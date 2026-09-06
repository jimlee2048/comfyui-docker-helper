"""Direct-Git custom-node content identity and placement contracts."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.build_plan_support import accepted_resolution, build_plan, final_config
from tests.container_installer_support import (
    application as _application,
)
from tests.container_installer_support import (
    custom_nodes_phase as _phase,
)
from tests.container_installer_support import (
    patch_phases as _patch_phases,
)

from comfyui_docker_helper.config.authored.models import FinalConfig
from comfyui_docker_helper.config.authored.validation.domains import (
    validate_final_config_domains,
)
from comfyui_docker_helper.config.authored.validation.semantics import (
    validate_final_config_semantics,
)
from comfyui_docker_helper.config.evidence.custom_nodes import custom_node_inventory
from comfyui_docker_helper.config.planning.build_plan import (
    GitNodePlan,
    HookPlan,
)
from comfyui_docker_helper.config.planning.canonical_lock import (
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    canonical_lock_from_entries,
    compute_request_digest,
)
from comfyui_docker_helper.config.planning.resolver import AcceptedCanonicalLock
from comfyui_docker_helper.container.build.custom_nodes import (
    git as git_installer,
)
from comfyui_docker_helper.container.build.custom_nodes import (
    orchestrator as custom_node_installer,
)
from comfyui_docker_helper.container.build.custom_nodes.contracts import (
    CustomNodeInstallError,
)
from comfyui_docker_helper.container.build.custom_nodes.git import (
    _prepare_git_node,
    _verify_git_provenance,
)
from comfyui_docker_helper.container.process.runners import ContainerRuntime, run_argv

_LOCAL_GIT_TIMEOUT_SECONDS = 30


def _git(repository: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ("git", "-C", repository, *arguments),
        check=True,
        capture_output=True,
        timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
    ).stdout


def _commit(repository: Path, message: str, content: str) -> str:
    repository.joinpath("content.txt").write_text(content)
    _git(repository, "add", "content.txt")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD").decode().strip()


def _repository(path: Path) -> tuple[str, str]:
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "cdh tests")
    first = _commit(path, "first", "first\n")
    second = _commit(path, "second", "second\n")
    return first, second


def _add_submodule(parent: Path, child: Path, relative: str, commit: str) -> None:
    _git(
        parent,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--",
        str(child),
        relative,
    )
    checkout = parent / relative
    _git(checkout, "checkout", "--detach", commit)
    _git(parent, "add", ".gitmodules", relative)
    _git(parent, "commit", "-m", f"add {relative}")


def _materialized_nested_checkout(tmp_path: Path) -> tuple[Path, GitNodePlan, str]:
    sources = tmp_path / "sources"
    sources.mkdir()
    leaf = sources / "leaf"
    leaf_first, leaf_second = _repository(leaf)
    middle = sources / "middle"
    _repository(middle)
    _add_submodule(middle, leaf, "nested/leaf", leaf_first)
    middle_commit = _git(middle, "rev-parse", "HEAD").decode().strip()
    root_source = sources / "root"
    _repository(root_source)
    _add_submodule(root_source, middle, "deps/middle", middle_commit)
    root_commit = _git(root_source, "rev-parse", "HEAD").decode().strip()

    custom_nodes = tmp_path / "custom_nodes"
    custom_nodes.mkdir()
    target = custom_nodes / "node"
    subprocess.run(
        ("git", "clone", "--no-checkout", "--", str(root_source), str(target)),
        check=True,
        capture_output=True,
        timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    _git(target, "checkout", "--detach", root_commit, "--")
    _git(
        target,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--checkout",
    )
    node = GitNodePlan(
        type="git",
        url="https://example.invalid/Raw/Node.git",
        commit=root_commit,
        target=str(target),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )
    return custom_nodes, node, leaf_second


def _verify(custom_nodes: Path, node: GitNodePlan) -> None:
    _verify_git_provenance(
        node,
        Path(node.target),
        custom_nodes,
        Path("/usr/bin/git"),
        os.environ,
    )


# Direct-Git identity binds committed recursive content and exact target placement.
def test_real_git_proof_uses_committed_recursive_gitlinks_not_dirty_index(
    tmp_path: Path,
) -> None:
    custom_nodes, node, leaf_second = _materialized_nested_checkout(tmp_path)
    target = Path(node.target)

    assert target.joinpath(".git").is_dir()
    assert target.joinpath("deps/middle/.git").is_file()
    assert target.joinpath("deps/middle/nested/leaf/.git").is_file()

    _verify(custom_nodes, node)
    _git(
        Path(node.target),
        "update-index",
        "--cacheinfo",
        f"160000,{leaf_second},deps/middle",
    )
    Path(node.target).joinpath("content.txt").write_text("trusted mutation\n")
    Path(node.target).joinpath("generated.txt").write_text("generated\n")
    Path(node.target).joinpath("deps/middle/content.txt").write_text(
        "nested trusted mutation\n"
    )

    _verify(custom_nodes, node)


def test_root_git_symlink_is_rejected(tmp_path: Path) -> None:
    custom_nodes, node, _leaf_second = _materialized_nested_checkout(tmp_path)
    target = Path(node.target)
    dot_git = target / ".git"
    moved = target / ".git-real"
    dot_git.rename(moved)
    dot_git.symlink_to(moved, target_is_directory=True)

    with pytest.raises(CustomNodeInstallError, match=r"\.git directory"):
        _verify(custom_nodes, node)


def test_linked_worktree_root_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _first, commit = _repository(source)
    custom_nodes = tmp_path / "custom_nodes"
    custom_nodes.mkdir()
    target = custom_nodes / "node"
    _git(source, "worktree", "add", "--detach", str(target), commit)
    node = GitNodePlan(
        type="git",
        url="https://example.invalid/node.git",
        commit=commit,
        target=str(target),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )

    assert target.joinpath(".git").is_file()
    with pytest.raises(CustomNodeInstallError, match=r"\.git directory"):
        _verify(custom_nodes, node)


def test_external_submodule_git_directory_is_rejected(tmp_path: Path) -> None:
    custom_nodes, node, _leaf_second = _materialized_nested_checkout(tmp_path)
    child = Path(node.target) / "deps/middle"
    git_file = child / ".git"
    source_repository = (
        _git(
            Path(node.target),
            "config",
            "-f",
            ".gitmodules",
            "--get",
            "submodule.deps/middle.url",
        )
        .decode()
        .strip()
    )
    external_git_directory = tmp_path / "external-middle-git"
    subprocess.run(
        ("git", "clone", "--bare", "--", source_repository, external_git_directory),
        check=True,
        capture_output=True,
        timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    subprocess.run(
        (
            "git",
            "--git-dir",
            external_git_directory,
            "config",
            "core.worktree",
            str(child),
        ),
        check=True,
        capture_output=True,
        timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    git_file.write_text(f"gitdir: {external_git_directory}\n")

    with pytest.raises(CustomNodeInstallError, match="escapes root Git management"):
        _verify(custom_nodes, node)


@pytest.mark.parametrize("mutation", ["attached-root", "wrong-nested", "uninitialized"])
def test_real_git_proof_rejects_root_or_recursive_materialization_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    custom_nodes, node, leaf_second = _materialized_nested_checkout(tmp_path)
    target = Path(node.target)
    if mutation == "attached-root":
        _git(target, "switch", "-c", "mutated")
    elif mutation == "wrong-nested":
        _git(target / "deps/middle/nested/leaf", "checkout", "--detach", leaf_second)
    else:
        _git(target, "submodule", "deinit", "-f", "--all")

    with pytest.raises(CustomNodeInstallError, match=r"detached|commit|submodule"):
        _verify(custom_nodes, node)


def test_repository_root_proof_rejects_parent_repository_discovery(
    tmp_path: Path,
) -> None:
    custom_nodes = tmp_path / "custom_nodes"
    commit, _ = _repository(custom_nodes)
    target = custom_nodes / "node"
    target.mkdir()
    node = GitNodePlan(
        type="git",
        url="ssh://git@example.invalid/node.git",
        commit=commit,
        target=str(target),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )

    with pytest.raises(CustomNodeInstallError, match="repository root"):
        _verify(custom_nodes, node)


def test_final_proof_rejects_a_different_valid_sibling_repository(
    tmp_path: Path,
) -> None:
    custom_nodes = tmp_path / "custom_nodes"
    custom_nodes.mkdir()
    sibling = custom_nodes / "sibling"
    _first, commit = _repository(sibling)
    node = GitNodePlan(
        type="git",
        url="https://example.invalid/node.git",
        commit=commit,
        target=str(custom_nodes / "expected"),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )

    with pytest.raises(CustomNodeInstallError, match="does not match BuildPlan"):
        _verify_git_provenance(
            node,
            sibling,
            custom_nodes,
            Path("/usr/bin/git"),
            os.environ,
        )


def test_direct_git_prepare_clones_into_final_target_and_retains_repository_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _first, commit = _repository(source)
    _application_phase, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    target = custom_nodes / "direct"
    # The clone fixture supplies a local source after BuildPlan admission.
    node = GitNodePlan.model_construct(
        type="git",
        url=str(source),
        commit=commit,
        target=str(target),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )
    _prepare_git_node(
        node,
        custom_nodes,
        Path("/usr/bin/git"),
        {**os.environ, "GIT_ALLOW_PROTOCOL": "file"},
    )

    assert _git(target, "rev-parse", "HEAD").decode().strip() == commit
    symbolic = subprocess.run(
        ("git", "-C", target, "symbolic-ref", "-q", "HEAD"),
        check=False,
        capture_output=True,
        timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    assert symbolic.returncode == 1
    assert target.joinpath(".git").exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_direct_git_prepare_never_replaces_an_occupied_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _application_phase, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    target = custom_nodes / "direct"
    if kind == "file":
        target.write_text("foreign")
    elif kind == "directory":
        target.mkdir()
    else:
        foreign = tmp_path / "missing-foreign-target"
        target.symlink_to(foreign, target_is_directory=True)
    node = GitNodePlan.model_construct(
        type="git",
        url=str(tmp_path / "source"),
        commit="a" * 40,
        target=str(target),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )
    monkeypatch.setattr(
        git_installer,
        "_run_git",
        lambda *_args, **_kwargs: pytest.fail("occupied target must stop before Git"),
    )

    with pytest.raises(CustomNodeInstallError, match="already exists"):
        _prepare_git_node(
            node,
            custom_nodes,
            Path("/usr/bin/git"),
            os.environ,
        )

    if kind == "file":
        assert target.read_text() == "foreign"
    elif kind == "directory":
        assert target.is_dir()
        assert not tuple(target.iterdir())
    else:
        assert target.is_symlink()
        assert target.readlink() == foreign


def test_direct_git_prepare_readmits_the_real_custom_nodes_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _application_phase, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    custom_nodes.rmdir()
    replacement = tmp_path / "replacement-custom-nodes"
    replacement.mkdir()
    custom_nodes.symlink_to(replacement, target_is_directory=True)
    node = GitNodePlan.model_construct(
        type="git",
        url=str(tmp_path / "source"),
        commit="a" * 40,
        target=str(custom_nodes / "direct"),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )
    monkeypatch.setattr(
        git_installer,
        "_run_git",
        lambda *_args, **_kwargs: pytest.fail("unsafe root must stop before Git"),
    )

    with pytest.raises(CustomNodeInstallError, match="must be one real directory"):
        _prepare_git_node(
            node,
            custom_nodes,
            Path("/usr/bin/git"),
            os.environ,
        )

    assert custom_nodes.is_symlink()
    assert not (replacement / "direct").exists()


def test_failed_direct_git_clone_leaves_created_target_for_failed_layer(
    tmp_path: Path,
) -> None:
    _application_phase, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    unrelated = custom_nodes / "unrelated"
    unrelated.mkdir()
    unrelated.joinpath("keep").write_text("keep")
    # The failure fixture supplies a missing local source after plan admission.
    node = GitNodePlan.model_construct(
        type="git",
        url=str(tmp_path / "missing-source"),
        commit="a" * 40,
        target=str(custom_nodes / "direct"),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )

    with pytest.raises(CustomNodeInstallError, match="clone failed"):
        _prepare_git_node(
            node,
            custom_nodes,
            Path("/usr/bin/git"),
            os.environ,
        )

    assert unrelated.joinpath("keep").read_text() == "keep"
    assert (custom_nodes / "direct").is_dir()


# Git failures keep stderr on the live build stream while returning concise cdh errors.
def test_failed_git_command_preserves_stderr_diagnostic(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    command = (
        sys.executable,
        "-c",
        (
            "import sys; "
            "print('captured stdout'); "
            "print('streamed Git diagnostic', file=sys.stderr); "
            "raise SystemExit(19)"
        ),
    )

    with pytest.raises(
        CustomNodeInstallError,
        match="Git diagnostic probe failed with exit code 19",
    ):
        git_installer._run_git(
            command,
            cwd=tmp_path,
            env=os.environ,
            description="Git diagnostic probe",
        )

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == "streamed Git diagnostic\n"


# Direct-Git acquisition passes the locked raw locator unchanged.
def test_direct_git_retrieval_receives_the_unchanged_declared_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator = "ssh://Git@Example.invalid:22/Org/Node.git"
    config_document = final_config().model_dump(mode="python")
    config_document["comfyui"]["custom_nodes"][1]["url"] = locator
    config = FinalConfig.model_validate(config_document)
    domains = validate_final_config_domains(config)
    assert (
        *domains.diagnostics,
        *validate_final_config_semantics(config, domains),
    ) == ()
    resolution = accepted_resolution()
    entries = [
        entry.model_copy(
            update={
                "url": locator,
                "request_digest": compute_request_digest(
                    DirectGitRequestIdentity(type="git", url=locator, ref="2" * 40)
                ),
            }
        )
        if isinstance(entry, DirectGitLockEntry)
        else entry
        for entry in resolution.lock.entries
    ]
    changed_resolution = AcceptedCanonicalLock(
        lock=canonical_lock_from_entries(entries),
        delta=resolution.delta,
        write_intent=resolution.write_intent,
        provider_calls=resolution.provider_calls,
        local_reads=resolution.local_reads,
    )
    plan = build_plan(config, changed_resolution)
    planned = plan.custom_nodes.nodes[1]
    assert isinstance(planned, GitNodePlan)
    assert config.comfyui.custom_nodes[1].url == locator
    locked = next(
        entry
        for entry in changed_resolution.lock.entries
        if isinstance(entry, DirectGitLockEntry)
    )
    assert locked.url == locator
    assert planned.url == locator

    _application_phase, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    node = planned.model_copy(update={"target": str(custom_nodes / "direct")})
    commands: list[tuple[str, ...]] = []

    def run_git(argv, **_kwargs) -> bytes:
        commands.append(tuple(os.fspath(item) for item in argv))
        return b""

    monkeypatch.setattr(
        "comfyui_docker_helper.container.build.custom_nodes.git._run_git", run_git
    )
    _prepare_git_node(
        node,
        custom_nodes,
        Path("/usr/bin/git"),
        os.environ,
    )

    assert commands[0][-2] == locator
    assert commands[0][-1] == os.fspath(custom_nodes / "direct")
    evidence = custom_node_inventory((node,)).nodes[0]
    assert evidence.type == "git" and evidence.url == locator


# Mutation after installation invalidates the committed identity before later work.
@pytest.mark.parametrize("hook_stage", ["pre_install_hooks", "post_install_hooks"])
@pytest.mark.parametrize("mutation", ["root", "nested", "future"])
def test_hook_identity_drift_stops_before_next_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    hook_stage: str,
) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    _fixture_nodes, prepared_node, leaf_second = _materialized_nested_checkout(
        fixture_root
    )
    prepared = Path(prepared_node.target)
    application, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    first_target = custom_nodes / "first"
    first = GitNodePlan(
        type="git",
        url="https://example.invalid/first.git",
        commit=prepared_node.commit,
        target=str(first_target),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(
            HookPlan(relative_path="mutate.py", digest=f"sha256:{'a' * 64}"),
        ),
    )
    second = GitNodePlan(
        type="git",
        url="https://example.invalid/second.git",
        commit=prepared_node.commit,
        target=str(custom_nodes / "second"),
        pre_clone_hooks=(),
        pre_install_hooks=(),
        post_install_hooks=(),
    )
    first = first.model_copy(
        update={
            "post_install_hooks": (),
            hook_stage: first.post_install_hooks,
        }
    )
    phase = _phase(runtime, (first, second))
    _patch_phases(monkeypatch, application, phase)
    installs: list[str] = []

    def install(node, *_args) -> Path:
        installs.append(Path(node.target).name)
        if node is not first:
            pytest.fail("second node must not install after Git drift")
        prepared.rename(first_target)
        return first_target

    def mutate(_hook, **_kwargs) -> None:
        if mutation == "root":
            _git(first_target, "switch", "-c", "mutated")
        elif mutation == "nested":
            _git(
                first_target / "deps/middle/nested/leaf",
                "checkout",
                "--detach",
                leaf_second,
            )
        else:
            Path(second.target).mkdir()

    monkeypatch.setattr(git_installer, "_prepare_git_node", install)
    monkeypatch.setattr(
        git_installer, "_install_git_root_surfaces", lambda *_args: None
    )
    monkeypatch.setattr(custom_node_installer, "run_hook", mutate)
    monkeypatch.setattr(
        git_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("final health must not run"),
    )

    with pytest.raises(
        CustomNodeInstallError, match=r"detached|commit|future Git target"
    ):
        custom_node_installer.install_custom_nodes(
            phase,
            application,
            runtime=runtime,
        )

    assert installs == ["first"]


@pytest.mark.parametrize(
    "surfaces", ["both", "requirements-only", "install-only", "none"]
)
def test_real_hooks_patch_install_inputs_after_recursive_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surfaces: str,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _, source_node, _ = _materialized_nested_checkout(fixture)
    source = fixture / "sources/root"
    (source / "requirements.txt").write_text("requests==2.30.0\n")
    if surfaces != "install-only":
        (source / "install.py").write_text(
            "raise RuntimeError('unpatched installer')\n"
        )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "installation inputs")
    commit = _git(source, "rev-parse", "HEAD").decode().strip()
    application, initial_runtime = _application(tmp_path)
    runtime = ContainerRuntime(
        workspace=initial_runtime.workspace,
        comfyui_path=initial_runtime.comfyui_path,
        virtual_env=Path(sys.prefix),
    )
    application = application.model_copy(
        update={"paths": application.paths.model_copy(update={"venv": str(sys.prefix)})}
    )
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    has_requirements = surfaces in {"both", "requirements-only"}
    has_install = surfaces in {"both", "install-only"}
    common = (
        "import os\nfrom pathlib import Path\n"
        "root = Path(os.environ['COMFYUI_PATH'])\n"
        "assert Path.cwd() == root\n"
        "target = root / 'custom_nodes/direct'\n"
        "trace = root / 'hook-trace'\n"
    )
    install_script = (
        "from pathlib import Path\n"
        "target = Path.cwd()\n"
        + (
            "assert (target / 'requirements-consumed').exists()\n"
            if has_requirements
            else ""
        )
        + "with (target.parent.parent / 'hook-trace').open('a') as out:\n"
        "    out.write('install.py\\n')\n"
        "(target / 'installed').write_text('patched installer')\n"
    )
    scripts = {
        "clone.py": common
        + "assert not target.exists()\ntrace.write_text('pre-clone\\n')\n",
        "patch.py": common + "import subprocess\n" + "head = subprocess.check_output(\n"
        "    ['git', '-C', str(target), 'rev-parse', 'HEAD']).decode().strip()\n"
        + f"assert head == {commit!r}\n"
        + "leaf = target / 'deps/middle/nested/leaf/content.txt'\n"
        "assert leaf.read_text() == 'first\\n'\n"
        + "assert (target / 'requirements.txt').read_text() == 'requests==2.30.0\\n'\n"
        + (
            "(target / 'requirements.txt').write_text('packaging==24.0\\n')\n"
            if has_requirements
            else "(target / 'requirements.txt').unlink()\n"
        )
        + (
            f"(target / 'install.py').write_text({install_script!r})\n"
            if has_install
            else "(target / 'install.py').unlink()\n"
        )
        + "with trace.open('a') as out:\n    out.write('pre-install\\n')\n",
        "post.py": common
        + (
            "assert (target / 'installed').read_text() == 'patched installer'\n"
            if has_install
            else ""
        )
        + "with trace.open('a') as out:\n    out.write('post-install\\n')\n",
    }

    def hook(name: str) -> HookPlan:
        content = scripts[name].encode()
        (hooks / name).write_bytes(content)
        return HookPlan(
            relative_path=name, digest=f"sha256:{hashlib.sha256(content).hexdigest()}"
        )

    node = source_node.model_copy(
        update={
            "target": str(runtime.comfyui_path / "custom_nodes/direct"),
            "commit": commit,
            "pre_clone_hooks": (hook("clone.py"),),
            "pre_install_hooks": (hook("patch.py"),),
            "post_install_hooks": (hook("post.py"),),
        }
    )
    phase = _phase(runtime, (node,))
    _patch_phases(monkeypatch, application, phase)
    installed_requirements: list[str] = []

    def install(argv, **kwargs):
        if "--requirements" in argv:
            # Offline seam observes the real post-hook installer input without
            # contacting an index; install.py still executes as a real child.
            content = Path(argv[argv.index("--requirements") + 1]).read_text()
            installed_requirements.append(content)
            Path(kwargs["cwd"], "requirements-consumed").write_text(content)
            return subprocess.CompletedProcess(argv, 0)
        return run_argv(argv, **kwargs)

    monkeypatch.setattr(git_installer, "run_argv", install)
    custom_node_installer.install_custom_nodes(
        phase,
        application,
        runtime=runtime,
        build_hooks_directory=hooks,
        environ={
            **os.environ,
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{source}.insteadOf",
            "GIT_CONFIG_VALUE_0": node.url,
        },
    )

    expected = ["pre-clone", "pre-install"]
    if has_install:
        expected.append("install.py")
    expected.append("post-install")
    assert (runtime.comfyui_path / "hook-trace").read_text().splitlines() == expected
    assert installed_requirements == (["packaging==24.0\n"] if has_requirements else [])
