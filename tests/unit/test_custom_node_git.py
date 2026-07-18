"""Direct-Git custom-node content identity and placement contracts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from tests.unit.test_build_plan import accepted_resolution, build_plan, final_config
from tests.unit.test_custom_node_installer import _application, _patch_phases, _phase

from comfyui_docker_helper.config.build_plan import (
    GitNodePlan,
    HookPlan,
)
from comfyui_docker_helper.config.canonical_lock import (
    CanonicalLock,
    DirectGitLockEntry,
    DirectGitRequestIdentity,
    compute_request_digest,
)
from comfyui_docker_helper.config.canonical_resolver import AcceptedCanonicalLock
from comfyui_docker_helper.config.custom_node_inventory import (
    custom_node_inventory,
    dump_custom_node_inventory,
)
from comfyui_docker_helper.config.final_models import FinalConfig
from comfyui_docker_helper.config.final_validation import (
    validate_final_config_domains,
    validate_final_config_semantics,
)
from comfyui_docker_helper.container import custom_node_installer
from comfyui_docker_helper.container.custom_node_installer import (
    CustomNodeInstallError,
    _capture_owned_stage,
    _install_git_node,
    _rename_noreplace,
    _verify_git_provenance,
)


def _git(repository: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ("git", "-C", repository, *arguments),
        check=True,
        capture_output=True,
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
        pre_install=(),
        post_install=(),
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


# Direct-Git identity binds committed recursive content and safe atomic placement.
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
        pre_install=(),
        post_install=(),
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
        pre_install=(),
        post_install=(),
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
        pre_install=(),
        post_install=(),
    )

    with pytest.raises(CustomNodeInstallError, match="does not match BuildPlan"):
        _verify_git_provenance(
            node,
            sibling,
            custom_nodes,
            Path("/usr/bin/git"),
            os.environ,
        )


# Clone staging is atomic, owns cleanup, and receives the declared locator unchanged.
def test_atomic_placement_never_replaces_an_existing_target(tmp_path: Path) -> None:
    source = tmp_path / ".stage"
    source.mkdir()
    source.joinpath("source").write_text("source")
    target = tmp_path / "target"
    target.mkdir()
    target.joinpath("existing").write_text("existing")

    identity = _capture_owned_stage(source, tmp_path)
    with pytest.raises(CustomNodeInstallError, match="already exists"):
        _rename_noreplace(source, target, identity)

    assert source.joinpath("source").read_text() == "source"
    assert target.joinpath("existing").read_text() == "existing"


def test_direct_git_install_stages_places_and_retains_repository_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _first, commit = _repository(source)
    application, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    target = custom_nodes / "direct"
    # The clone fixture supplies a local source after BuildPlan admission.
    node = GitNodePlan.model_construct(
        type="git",
        url=str(source),
        commit=commit,
        target=str(target),
        pre_install=(),
        post_install=(),
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.container.custom_node_installer._install_git_root_surfaces",
        lambda *_args: None,
    )

    _install_git_node(
        node,
        custom_nodes,
        application,
        runtime,
        Path("/usr/bin/git"),
        Path("/usr/local/bin/uv"),
        tmp_path / "constraints.txt",
        {**os.environ, "GIT_ALLOW_PROTOCOL": "file"},
        {},
    )

    assert _git(target, "rev-parse", "HEAD").decode().strip() == commit
    symbolic = subprocess.run(
        ("git", "-C", target, "symbolic-ref", "-q", "HEAD"),
        check=False,
        capture_output=True,
    )
    assert symbolic.returncode == 1
    assert target.joinpath(".git").exists()
    assert not tuple(custom_nodes.glob(".direct.cdh-stage-*"))


def test_failed_direct_git_clone_cleans_only_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
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
        pre_install=(),
        post_install=(),
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.container.custom_node_installer._install_git_root_surfaces",
        lambda *_args: None,
    )

    with pytest.raises(CustomNodeInstallError, match="clone failed"):
        _install_git_node(
            node,
            custom_nodes,
            application,
            runtime,
            Path("/usr/bin/git"),
            Path("/usr/local/bin/uv"),
            tmp_path / "constraints.txt",
            os.environ,
            {},
        )

    assert unrelated.joinpath("keep").read_text() == "keep"
    assert not (custom_nodes / "direct").exists()
    assert not tuple(custom_nodes.glob(".direct.cdh-stage-*"))


def test_stage_replacement_race_fails_without_removing_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    node = GitNodePlan(
        type="git",
        url="https://example.invalid/node.git",
        commit="a" * 40,
        target=str(custom_nodes / "direct"),
        pre_install=(),
        post_install=(),
    )
    replacement: Path | None = None

    def replace_stage(argv, **_kwargs) -> bytes:
        nonlocal replacement
        stage = Path(argv[-1])
        original = stage.with_name(f"{stage.name}.original")
        stage.rename(original)
        stage.mkdir()
        stage.joinpath("replacement").write_text("keep")
        replacement = stage
        return b""

    monkeypatch.setattr(
        "comfyui_docker_helper.container.custom_node_installer._run_git",
        replace_stage,
    )

    with pytest.raises(CustomNodeInstallError, match="stage identity"):
        _install_git_node(
            node,
            custom_nodes,
            application,
            runtime,
            Path("/usr/bin/git"),
            Path("/usr/local/bin/uv"),
            tmp_path / "constraints.txt",
            os.environ,
            {},
        )

    assert replacement is not None
    assert replacement.joinpath("replacement").read_text() == "keep"


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
        lock=CanonicalLock(schema_version=1, entries=entries),
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

    application, runtime = _application(tmp_path)
    custom_nodes = runtime.comfyui_path / "custom_nodes"
    node = planned.model_copy(update={"target": str(custom_nodes / "direct")})
    commands: list[tuple[str, ...]] = []

    def run_git(argv, **_kwargs) -> bytes:
        commands.append(tuple(os.fspath(item) for item in argv))
        return b""

    monkeypatch.setattr(
        "comfyui_docker_helper.container.custom_node_installer._run_git", run_git
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.container.custom_node_installer._verify_git_provenance",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "comfyui_docker_helper.container.custom_node_installer._install_git_root_surfaces",
        lambda *_args: None,
    )

    _install_git_node(
        node,
        custom_nodes,
        application,
        runtime,
        Path("/usr/bin/git"),
        Path("/usr/local/bin/uv"),
        tmp_path / "constraints.txt",
        os.environ,
        {},
    )

    assert commands[0][-2] == locator
    assert f'"url":"{locator}"'.encode() in dump_custom_node_inventory(
        custom_node_inventory((node,))
    )


# Mutation after installation invalidates the committed identity before later work.
@pytest.mark.parametrize("mutation", ["root", "nested"])
def test_post_hook_head_drift_stops_before_next_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
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
        pre_install=(),
        post_install=(
            HookPlan(relative_path="mutate.py", digest=f"sha256:{'a' * 64}"),
        ),
    )
    second = GitNodePlan(
        type="git",
        url="https://example.invalid/second.git",
        commit=prepared_node.commit,
        target=str(custom_nodes / "second"),
        pre_install=(),
        post_install=(),
    )
    phase = _phase(runtime, (first, second))
    _patch_phases(monkeypatch, application, phase)
    installs: list[str] = []

    def install(node, *_args) -> None:
        installs.append(Path(node.target).name)
        if node is not first:
            pytest.fail("second node must not install after Git drift")
        prepared.rename(first_target)

    def mutate(_hook, **_kwargs) -> None:
        if mutation == "root":
            _git(first_target, "switch", "-c", "mutated")
        else:
            _git(
                first_target / "deps/middle/nested/leaf",
                "checkout",
                "--detach",
                leaf_second,
            )

    monkeypatch.setattr(custom_node_installer, "_install_git_node", install)
    monkeypatch.setattr(custom_node_installer, "run_hook", mutate)
    monkeypatch.setattr(
        custom_node_installer,
        "run_argv",
        lambda *_args, **_kwargs: pytest.fail("final health must not run"),
    )

    with pytest.raises(CustomNodeInstallError, match=r"detached|commit"):
        custom_node_installer.install_custom_nodes(
            phase,
            application,
            runtime=runtime,
        )

    assert installs == ["first"]
