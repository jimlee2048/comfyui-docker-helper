"""BuildPlan strict admission and authority validation contracts."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError
from tests.build_plan_support import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    accepted_resolution,
    build_plan,
    final_config,
)

from comfyui_docker_helper.config.authored.models import FinalConfig
from comfyui_docker_helper.config.authored.validation.domains import (
    validate_final_config_domains,
)
from comfyui_docker_helper.config.authored.validation.semantics import (
    validate_final_config_semantics,
)
from comfyui_docker_helper.config.planning.build_plan import (
    BuildPlan,
    ExactPackagePlan,
    UvToolPlan,
    dump_build_plan_json,
    parse_build_plan_json,
)
from comfyui_docker_helper.config.planning.canonical_lock import (
    CanonicalLock,
)
from comfyui_docker_helper.config.planning.resolver import AcceptedCanonicalLock

_VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "first@example"
)


def test_build_plan_parser_rejects_registry_without_manager_or_unique_identity() -> (
    None
):
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["application"]["comfyui"]["manager"] = None
    document["custom_nodes"]["install_manager"] = False

    with pytest.raises(ValidationError, match="Registry nodes require Manager"):
        parse_build_plan_json(json.dumps(document))

    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    duplicate = dict(document["custom_nodes"]["nodes"][0])
    duplicate["id"] = "Registry_Node"
    document["custom_nodes"]["nodes"] = (
        *document["custom_nodes"]["nodes"],
        duplicate,
    )

    with pytest.raises(ValidationError, match="identities must be unique"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_enforces_complete_hook_tree_identity() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["custom_nodes"]["nodes"][0]["pre_install_hooks"] = (
        {"relative_path": "hooks/install.txt", "digest": DIGEST_A},
    )
    with pytest.raises(ValidationError, match=r"must end in \.sh or \.py"):
        parse_build_plan_json(json.dumps(document))

    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["custom_nodes"]["nodes"][0]["pre_install_hooks"] = (
        {"relative_path": "hooks/install.py", "digest": DIGEST_A},
    )
    document["custom_nodes"]["nodes"][1]["post_install_hooks"] = (
        {"relative_path": "hooks/install.py", "digest": DIGEST_B},
    )
    with pytest.raises(ValidationError, match="conflicting digests"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_accepts_reused_build_hook_and_separate_tree_path() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    hook = {"relative_path": "pre-start.d/shared.py", "digest": DIGEST_A}
    document["custom_nodes"]["nodes"][0]["pre_install_hooks"] = (hook,)
    document["custom_nodes"]["nodes"][1]["post_install_hooks"] = (hook,)
    document["runtime"]["hooks"] = (hook,)

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.custom_nodes.nodes[0].pre_install_hooks[0].digest == DIGEST_A
    assert parsed.runtime.hooks[0].relative_path == "pre-start.d/shared.py"


def test_build_plan_rejects_file_target_ancestor_overlap_across_file_kinds() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    root = "/workspace/ComfyUI"
    local_relative_target = "models/checkpoints/local.bin"
    tree_relative_target = "models/checkpoints"
    http = dict(document["files"]["files"][0])
    http["target"] = f"{root}/models/checkpoints/remote.bin"
    local = {
        "type": "local",
        "kind": "file",
        "target": f"{root}/{local_relative_target}",
        "relative_target": local_relative_target,
        "context_path": (
            "build/files/"
            + hashlib.sha256(local_relative_target.encode("utf-8")).hexdigest()
        ),
        "verification": "unverified-local",
        "digest": None,
    }
    tree = {
        "type": "local",
        "kind": "tree",
        "target": f"{root}/{tree_relative_target}",
        "relative_target": tree_relative_target,
        "context_path": (
            "build/trees/"
            + hashlib.sha256(tree_relative_target.encode("utf-8")).hexdigest()
        ),
        "root_mode": "0755",
        "verification": "unverified-local",
        "members": (),
        "tree_digest": None,
    }
    document["files"]["files"] = (http, local, tree)

    with pytest.raises(ValidationError, match="file targets must not overlap"):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    "relative_path",
    ["unknown.d/hook.sh", "pre-start.d/nested/hook.sh"],
)
def test_build_plan_parser_rejects_invalid_runtime_hook_identity(
    relative_path: str,
) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["runtime"]["hooks"] = (
        {"relative_path": relative_path, "digest": DIGEST_A},
    )

    with pytest.raises(ValidationError, match="phase directory and filename"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_duplicate_runtime_hook_identity() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    hook = {"relative_path": "stop.d/cleanup.sh", "digest": DIGEST_A}
    document["runtime"]["hooks"] = (hook, hook)

    with pytest.raises(ValidationError, match="runtime hook identities must be unique"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_binds_registry_user_directory_to_comfyui() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["custom_nodes"]["user_directory"] = "/workspace/other/user"

    with pytest.raises(ValidationError, match="user directory does not match"):
        BuildPlan.model_validate(document)


def test_build_plan_constructor_rejects_core_channel_mismatch() -> None:
    resolution = accepted_resolution()
    data = resolution.lock.model_dump(mode="python")
    data["python"]["package_groups"]["pytorch"]["packages"][0]["version"] = (
        "2.12.1+cu129"
    )
    changed = AcceptedCanonicalLock(
        lock=CanonicalLock.model_validate(data),
        delta=(),
        write_intent=False,
        provider_calls=(),
        local_reads=(),
    )

    with pytest.raises(ValueError, match="canonical package does not satisfy"):
        build_plan(final_config(), changed)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "Torch", "normalized distribution name"),
        ("name", "torch.core", "normalized distribution name"),
        ("extras", ("Image_Preview",), "sorted, unique, and normalized"),
        ("extras", ("image", "image"), "sorted, unique, and normalized"),
        ("extras", ("preview", "image"), "sorted, unique, and normalized"),
    ],
)
def test_exact_package_plan_rejects_noncanonical_pep503_identity(
    field: str, value: str | tuple[str, ...], message: str
) -> None:
    document = {
        "name": "torch",
        "extras": (),
        "version": "2.12.1+cu130",
        "direct_reference": None,
        "environment": "application",
    }
    document[field] = value

    with pytest.raises(ValidationError, match=message):
        ExactPackagePlan.model_validate(document)


@pytest.mark.parametrize("version", ["1.0rc1", "1.0.dev1", "1.0+cu130"])
def test_user_package_and_tool_plans_accept_canonical_pep440_versions(
    version: str,
) -> None:
    package = ExactPackagePlan(
        name="demo",
        extras=(),
        version=version,
        direct_reference=None,
        environment="application",
    )
    tool = UvToolPlan(
        name="demo-tool",
        extras=(),
        version=version,
        direct_reference=None,
        environment="uv-tool:demo-tool",
    )

    assert package.version == version
    assert tool.version == version


@pytest.mark.parametrize(
    ("model", "document"),
    [
        (
            ExactPackagePlan,
            {
                "name": "demo",
                "extras": (),
                "version": "1.0",
                "direct_reference": "file:///tmp/demo.whl",
                "environment": "application",
            },
        ),
        (
            UvToolPlan,
            {
                "name": "demo-tool",
                "extras": (),
                "version": "1.0",
                "direct_reference": "https://user@example.test/demo.whl",
                "environment": "uv-tool:demo-tool",
            },
        ),
    ],
)
def test_user_package_plans_reject_unadmitted_direct_sources(
    model: type[ExactPackagePlan] | type[UvToolPlan],
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="admitted package source"):
        model.model_validate(document)


def test_build_plan_rejects_protected_pytorch_direct_source() -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    document["application"]["pytorch"]["packages"][0]["direct_reference"] = (
        "https://example.test/torch.whl"
    )

    with pytest.raises(ValidationError, match="protected PyTorch packages"):
        BuildPlan.model_validate(document)


def test_build_plan_admission_rejects_reserved_staging_final_leaf() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    document["files"]["files"][0]["target"] = "/workspace/ComfyUI/models/.cdh-staging"

    with pytest.raises(ValidationError, match="reserved staging path component"):
        BuildPlan.model_validate(document)


def test_build_plan_parser_rejects_forged_release_pip_authority() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["pip_version"] = "99.0.0"
    document["toolchain"]["python"]["pip_version"] = "99.0.0"

    with pytest.raises(ValidationError, match="pip version does not match"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_equal_workspace_and_comfyui_paths() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["paths"]["workspace"] = document["application"]["paths"][
        "comfyui"
    ]

    with pytest.raises(ValidationError, match="paths must be different"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_accepts_alternate_absolute_workspace_layout() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["paths"]["workspace"] = "/srv/work area"

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.application.paths.workspace == "/srv/work area"


@pytest.mark.parametrize(
    ("index", "value"),
    [(0, "/usr/bin/python3"), (1, "/tmp/other-main.py")],
)
def test_build_plan_parser_binds_runtime_launch_identity_to_application(
    index: int,
    value: str,
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["runtime"]["launch_command"][index] = value

    with pytest.raises(ValidationError, match="must match the application"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_forged_python_catalog_binding() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"]["catalog_descriptor_digest"] = DIGEST_C

    with pytest.raises(ValidationError, match="catalog is not bound"):
        parse_build_plan_json(json.dumps(document))


@pytest.mark.parametrize("catalog_key", ["..", "../python", "python/key", "key\\name"])
def test_build_plan_parser_rejects_unsafe_python_catalog_key(
    catalog_key: str,
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"]["catalog_key"] = catalog_key

    with pytest.raises(ValidationError, match="safe path component"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_preserves_safe_opaque_python_catalog_key() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"]["catalog_key"] = "alternate.catalog+key"

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.toolchain.python.catalog_key == "alternate.catalog+key"


@pytest.mark.parametrize(
    ("tag", "resolved_version"),
    [("debian-slim", "0.11.29"), ("0.11.29-debian-slim", "0.11.29")],
)
def test_build_plan_parser_accepts_locked_uv_image_selector(
    tag: str, resolved_version: str
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    uv_image = document["toolchain"]["uv_image"]
    uv_image["tag"] = tag
    uv_image["resolved_version"] = resolved_version

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.toolchain.uv_image.tag == tag
    assert parsed.toolchain.uv_image.resolved_version == resolved_version


def test_build_plan_parser_rejects_exact_uv_image_version_mismatch() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    uv_image = document["toolchain"]["uv_image"]
    uv_image["tag"] = "0.11.29-debian-slim"
    uv_image["resolved_version"] = "0.11.30"

    with pytest.raises(ValidationError, match="does not match its exact tag"):
        parse_build_plan_json(json.dumps(document))


@pytest.mark.parametrize("version", ["3.11.9", "3.15.0"])
def test_build_plan_parser_rejects_python_outside_package_support(
    version: str,
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["python"]["version"] = version

    with pytest.raises(ValidationError, match=r">=3\.12,<3\.15"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_accepts_unlisted_python_patch_inside_support() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    version = "3.13.15"
    document["toolchain"]["python"]["version"] = version
    document["application"]["pytorch"]["python_version"] = version
    document["application"]["python_extras"]["python_version"] = version
    document["application"]["comfyui"]["requirements"]["python_version"] = version

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.toolchain.python.version == version


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", "99.0.0", "cdh version does not match"),
        ("wheel_digest", "invalid", "digest must be sha256"),
    ],
)
def test_build_plan_parser_rejects_forged_cdh_wheel_identity(
    field: str, value: str, message: str
) -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["toolchain"]["tool_store"]["cdh"][field] = value

    with pytest.raises(ValidationError, match=message):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_forged_core_channel() -> None:
    plan = build_plan(final_config(), accepted_resolution())
    document = plan.model_dump(mode="python")
    torch = next(
        package
        for package in document["application"]["pytorch"]["packages"]
        if package["name"] == "torch"
    )
    torch["version"] = "2.12.1+cu129"

    with pytest.raises(ValidationError, match="does not match the group channel"):
        BuildPlan.model_validate(document)


def test_build_plan_parser_rejects_protected_projection_member_without_result() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    packages = document["application"]["pytorch"]["packages"]
    document["application"]["pytorch"]["packages"] = [
        package for package in packages if package["name"] != "torchaudio"
    ]

    with pytest.raises(ValidationError, match="missing exact PyTorch results"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_rejects_cohesively_shrunk_protected_policy() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    requirements = document["application"]["comfyui"]["requirements"]
    requirements["protected_names"] = ["torch"]
    requirements["protected"] = [
        item for item in requirements["protected"] if item["package"] == "torch"
    ]
    document["application"]["pytorch"]["packages"] = [
        package
        for package in document["application"]["pytorch"]["packages"]
        if package["name"] == "torch"
    ]

    with pytest.raises(ValidationError, match="do not match the backend adapter"):
        parse_build_plan_json(json.dumps(document))


def test_build_plan_parser_allows_adapter_member_absent_from_upstream_and_config() -> (
    None
):
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    requirements = document["application"]["comfyui"]["requirements"]
    requirements["protected"] = [
        item for item in requirements["protected"] if item["package"] != "torchaudio"
    ]
    document["application"]["pytorch"]["packages"] = [
        package
        for package in document["application"]["pytorch"]["packages"]
        if package["name"] != "torchaudio"
    ]

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.application.comfyui.requirements.protected_names == (
        "torch",
        "torchaudio",
        "torchvision",
    )


def test_build_plan_parser_allows_arbitrary_exact_pytorch_extra() -> None:
    document = json.loads(
        dump_build_plan_json(build_plan(final_config(), accepted_resolution()))
    )
    document["application"]["pytorch"]["packages"].append(
        {
            "name": "xformers",
            "extras": (),
            "version": "0.0.35+cu130",
            "direct_reference": None,
            "environment": "application",
        }
    )

    parsed = parse_build_plan_json(json.dumps(document))

    assert parsed.application.pytorch.packages[-1].name == "xformers"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("group-channel", "index must end"),
        ("group-index", "index must end"),
        ("group-python-index", "generic dependencies"),
        ("toolchain-channel", "target does not match"),
        ("toolchain-python", "target does not match"),
        ("duplicate-package", "packages must be unique"),
        ("case-variant-duplicate", "normalized distribution name"),
        ("missing-torch", "packages must be unique"),
        ("invalid-setuptools", "Invalid specifier"),
    ],
)
def test_build_plan_parser_rejects_cross_field_authority_forgery(
    mutation: str, message: str
) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    pytorch = document["application"]["pytorch"]
    if mutation == "group-channel":
        pytorch["channel"] = "cu129"
    elif mutation == "group-index":
        pytorch["pytorch_index_url"] = "https://download.pytorch.org/whl/cu129"
    elif mutation == "group-python-index":
        pytorch["python_index_url"] = "https://index.example.test/simple"
    elif mutation == "toolchain-channel":
        document["toolchain"]["pytorch_channel"] = "cu129"
    elif mutation == "toolchain-python":
        document["toolchain"]["python"]["version"] = "3.12.13"
    elif mutation == "duplicate-package":
        pytorch["packages"] = (pytorch["packages"][0], pytorch["packages"][0])
    elif mutation == "case-variant-duplicate":
        duplicate = dict(pytorch["packages"][0])
        duplicate["name"] = "Torch"
        pytorch["packages"] = (*pytorch["packages"], duplicate)
    elif mutation == "missing-torch":
        pytorch["packages"] = (pytorch["packages"][1],)
    else:
        pytorch["setuptools_specifier"] = "latest"

    with pytest.raises(ValidationError, match=message):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("oci-repository", "canonical OCI repository"),
        ("oci-digest", "digest must be sha256"),
        ("application-path", "canonical absolute POSIX path"),
        ("comfyui-repository", "canonical Git source URL"),
        ("registry-id", "argv-safe Registry ID"),
        ("git-target", "canonical absolute POSIX path"),
        ("file-url", "canonical HTTP"),
        ("launch-executable", "canonical absolute POSIX path"),
        ("shutdown-timeout", "must be a finite positive number or -1"),
        ("plan-digest", "digest must be sha256"),
    ],
)
def test_build_plan_parser_rejects_execution_sensitive_scalar_forgery(
    mutation: str, message: str
) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    if mutation == "oci-repository":
        document["toolchain"]["cuda_image"]["repository"] = "Invalid/Repository"
    elif mutation == "oci-digest":
        document["toolchain"]["uv_image"]["descriptor_digest"] = "bad"
    elif mutation == "application-path":
        document["application"]["paths"]["workspace"] = "relative"
    elif mutation == "comfyui-repository":
        document["application"]["comfyui"]["repository"] = "file:///tmp/source"
    elif mutation == "registry-id":
        document["custom_nodes"]["nodes"][0]["id"] = "-unsafe"
    elif mutation == "git-target":
        document["custom_nodes"]["nodes"][1]["target"] = "../escape"
    elif mutation == "file-url":
        document["files"]["files"][0]["url"] = "file:///tmp/model"
    elif mutation == "launch-executable":
        command = document["runtime"]["launch_command"]
        document["runtime"]["launch_command"] = ("python", *command[1:])
    elif mutation == "shutdown-timeout":
        document["runtime"]["shutdown_timeout"] = "8"
    else:
        document["image_config_digest"] = "bad"

    with pytest.raises(ValidationError, match=message):
        BuildPlan.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("cuda-repository", "cuda-base image repository does not match"),
        ("uv-repository", "uv-tool image repository does not match"),
        ("cuda-tag-grammar", "CUDA image tag must match"),
        ("cuda-derived-channel", "do not match toolchain"),
        ("comfyui-repository", "official ledger"),
        ("comfyui-release-floor", "below the supported floor"),
        ("git-target-sibling", "exact child of ComfyUI custom_nodes"),
        ("git-target-nested", "exact child of ComfyUI custom_nodes"),
        ("duplicate-git-target", "Git node targets must be unique"),
        ("file-target-outside", "strict descendants of ComfyUI"),
        ("duplicate-file-target", "file targets must be unique"),
        ("apt-option", "canonical package identity"),
        ("aria2-option", "canonical aria2 argument"),
        ("ssh-password-control", "must not contain control"),
        ("ssh-public-key", "canonical and unique"),
        ("ssh-public-key-duplicate", "canonical and unique"),
        ("launch-whitespace", "canonical argv values"),
    ],
)
def test_build_plan_rejects_syntactic_but_semantic_authority_forgery(
    mutation: str,
    message: str,
) -> None:
    document = build_plan(final_config(), accepted_resolution()).model_dump(
        mode="python"
    )
    if mutation == "cuda-repository":
        document["toolchain"]["cuda_image"]["repository"] = "ghcr.io/attacker/cuda"
    elif mutation == "uv-repository":
        document["toolchain"]["uv_image"]["repository"] = "ghcr.io/astral-sh/uv"
    elif mutation == "cuda-tag-grammar":
        document["toolchain"]["cuda_image"]["tag"] = "13.0.3-cudnn-devel-ubuntu20.04"
    elif mutation == "cuda-derived-channel":
        document["toolchain"]["pytorch_channel"] = "cu999"
        document["application"]["pytorch"]["channel"] = "cu999"
        document["application"]["pytorch"]["pytorch_index_url"] = (
            "https://download.pytorch.org/whl/cu999"
        )
        for package in document["application"]["pytorch"]["packages"]:
            if package["name"] in {"torch", "torchvision"}:
                package["version"] = package["version"].replace("+cu130", "+cu999")
    elif mutation == "comfyui-repository":
        document["application"]["comfyui"]["repository"] = (
            "https://github.com/attacker/ComfyUI.git"
        )
    elif mutation == "comfyui-release-floor":
        document["application"]["comfyui"]["formal_release"] = "0.10.0"
    elif mutation == "git-target-sibling":
        document["custom_nodes"]["nodes"][1]["target"] = (
            "/workspace/ComfyUI/plugins/direct-node"
        )
    elif mutation == "git-target-nested":
        document["custom_nodes"]["nodes"][1]["target"] = (
            "/workspace/ComfyUI/custom_nodes/nested/direct-node"
        )
    elif mutation == "duplicate-git-target":
        duplicate = dict(document["custom_nodes"]["nodes"][1])
        duplicate["url"] = "https://example.test/other.git"
        document["custom_nodes"]["nodes"] = (
            *document["custom_nodes"]["nodes"],
            duplicate,
        )
    elif mutation == "file-target-outside":
        document["files"]["files"][0]["target"] = "/workspace/model.safetensors"
    elif mutation == "duplicate-file-target":
        document["files"]["files"] = (
            document["files"]["files"][0],
            document["files"]["files"][0],
        )
    elif mutation == "apt-option":
        document["application"]["os_packages"] = (
            *document["application"]["os_packages"],
            "--allow-unauthenticated",
        )
    elif mutation == "aria2-option":
        document["files"]["downloader"]["aria2"]["min_split_size"] = "--quiet"
    elif mutation == "ssh-password-control":
        document["runtime"]["ssh"]["password"] = "secret\ncommand"
    elif mutation == "ssh-public-key":
        document["runtime"]["ssh"]["pub_keys"] = ("ssh-ed25519 AAAA invalid",)
    elif mutation == "ssh-public-key-duplicate":
        document["runtime"]["ssh"]["pub_keys"] = (
            _VALID_SSH_KEY,
            _VALID_SSH_KEY.rsplit(" ", 1)[0] + " second@example",
        )
    else:
        command = document["runtime"]["launch_command"]
        document["runtime"]["launch_command"] = (*command, "   ")

    with pytest.raises(ValidationError, match=message):
        BuildPlan.model_validate(document)


def test_user_cannot_duplicate_cdh_owned_launch_argument() -> None:
    document = final_config().model_dump(mode="python")
    document["comfyui"]["extra_args"] = ["--disable-auto-launch"]
    config = FinalConfig.model_validate(document)

    domains = validate_final_config_domains(config)
    diagnostics = (
        *domains.diagnostics,
        *validate_final_config_semantics(config, domains),
    )

    assert [item.code for item in diagnostics] == ["comfyui.controlled_extra_arg"]
