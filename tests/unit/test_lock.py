"""Tests for lockfile models, TOML I/O, and lock input digests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from comfyui_docker_helper.config import (
    Config,
    GitCustomNodeConfig,
    GitLockedCustomNode,
    LockDomainError,
    LockedComfyUI,
    Lockfile,
    LockManifest,
    RegistryCustomNodeConfig,
    RegistryLockedCustomNode,
    compute_lock_input_digest,
    dump_lockfile_toml,
    load_lockfile,
    parse_lockfile_toml,
    validate_lock_domain_unique,
    write_lockfile,
)


def make_config() -> Config:
    """Return a fresh minimal structurally valid configuration."""
    return Config.model_validate(
        {
            "compute_platform": {
                "type": "cuda",
                "cuda": {"version": "12.9.2"},
            },
            "pytorch": {"version": "2.10"},
            "comfyui": {"version": "latest"},
        }
    )


def make_lockfile() -> Lockfile:
    """Return a representative resolved lockfile."""
    return Lockfile(
        schema_version=1,
        manifest=LockManifest(
            lock_input_digest="sha256:"
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        ),
        comfyui=LockedComfyUI(
            repo="https://github.com/comfyanonymous/ComfyUI.git",
            version="0.26.0",
            commit="0123456789abcdef0123456789abcdef01234567",
            cli_version="1.5.0",
        ),
        custom_nodes=[
            RegistryLockedCustomNode(
                type="registry",
                id="comfyui-custom-scripts",
                version="1.2.3",
            ),
            GitLockedCustomNode(
                type="git",
                url="https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git",
                commit="abcdefabcdefabcdefabcdefabcdefabcdefabcd",
            ),
        ],
    )


def test_lockfile_round_trips_through_deterministic_toml(tmp_path: Path) -> None:
    """Round-trip the public lockfile shape through bytes and path helpers."""
    lockfile = make_lockfile()
    document = dump_lockfile_toml(lockfile)
    path = tmp_path / "config.lock.toml"

    write_lockfile(path, lockfile)

    assert path.read_text(encoding="utf-8") == document
    assert parse_lockfile_toml(document) == lockfile
    assert load_lockfile(path) == lockfile


def test_nightly_lockfile_omits_stable_comfyui_version() -> None:
    """Allow nightly replay locks to omit the optional stable release version."""
    lockfile = make_lockfile()
    lockfile.comfyui.version = None

    comfyui_section = (
        dump_lockfile_toml(lockfile)
        .split("[comfyui]\n", maxsplit=1)[1]
        .split("[[custom_nodes]]\n", maxsplit=1)[0]
    )
    assert not any(
        line.startswith("version =") for line in comfyui_section.splitlines()
    )
    assert parse_lockfile_toml(dump_lockfile_toml(lockfile)) == lockfile


def test_lockfile_serialization_is_deterministic() -> None:
    """Emit stable TOML field order and array-table shape."""
    assert dump_lockfile_toml(make_lockfile()) == (
        "schema_version = 1\n"
        "\n"
        "[manifest]\n"
        'lock_input_digest = "sha256:'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"\n'
        "\n"
        "[comfyui]\n"
        'repo = "https://github.com/comfyanonymous/ComfyUI.git"\n'
        'commit = "0123456789abcdef0123456789abcdef01234567"\n'
        'version = "0.26.0"\n'
        'cli_version = "1.5.0"\n'
        "\n"
        "[[custom_nodes]]\n"
        'type = "registry"\n'
        'id = "comfyui-custom-scripts"\n'
        'version = "1.2.3"\n'
        "\n"
        "[[custom_nodes]]\n"
        'type = "git"\n'
        'url = "https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git"\n'
        'commit = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"\n'
    )


@pytest.mark.parametrize(
    "document",
    [
        """
[manifest]
lock_input_digest = "sha256:abc"

[comfyui]
repo = "https://example.com/ComfyUI.git"
commit = "0123456789abcdef0123456789abcdef01234567"
cli_version = "1.5.0"
""",
        """
schema_version = 2

[manifest]
lock_input_digest = "sha256:abc"

[comfyui]
repo = "https://example.com/ComfyUI.git"
commit = "0123456789abcdef0123456789abcdef01234567"
cli_version = "1.5.0"
""",
        """
schema_version = 1

[manifest]

[comfyui]
repo = "https://example.com/ComfyUI.git"
commit = "0123456789abcdef0123456789abcdef01234567"
cli_version = "1.5.0"
""",
        """
schema_version = 1
unknown = true

[manifest]
lock_input_digest = "sha256:abc"

[comfyui]
repo = "https://example.com/ComfyUI.git"
commit = "0123456789abcdef0123456789abcdef01234567"
cli_version = "1.5.0"
""",
        """
schema_version = 1

[manifest]
lock_input_digest = "sha256:abc"

[comfyui]
repo = "https://example.com/ComfyUI.git"
commit = "0123456789abcdef0123456789abcdef01234567"
cli_version = "1.5.0"

[[custom_nodes]]
type = "registry"
id = "node"
""",
        """
schema_version = 1

[manifest]
lock_input_digest = "sha256:abc"

[comfyui]
repo = "https://example.com/ComfyUI.git"
commit = "0123456789abcdef0123456789abcdef01234567"
cli_version = "1.5.0"

[[custom_nodes]]
type = "git"
url = "https://example.com/node.git"
""",
    ],
)
def test_invalid_lockfiles_are_rejected(document: str) -> None:
    """Reject missing required fields, unsupported schema, extras, and bad entries."""
    with pytest.raises(ValidationError):
        parse_lockfile_toml(document)


def test_lockfile_rejects_duplicate_registry_ids() -> None:
    """Reject lockfiles that cannot map registry entries back by unique ID."""
    data = make_lockfile().model_dump(mode="json")
    data["custom_nodes"] = [
        {"type": "registry", "id": "same", "version": "1.0.0"},
        {"type": "registry", "id": "same", "version": "2.0.0"},
    ]

    with pytest.raises(ValidationError, match="duplicate registry custom-node id"):
        Lockfile.model_validate(data)


def test_lockfile_rejects_duplicate_git_urls() -> None:
    """Reject lockfiles that cannot map Git entries back by unique URL."""
    data = make_lockfile().model_dump(mode="json")
    data["custom_nodes"] = [
        {
            "type": "git",
            "url": "https://example.com/node.git",
            "commit": "a" * 40,
        },
        {
            "type": "git",
            "url": "https://example.com/node.git",
            "commit": "b" * 40,
        },
    ]

    with pytest.raises(ValidationError, match="duplicate git custom-node url"):
        Lockfile.model_validate(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: setattr(config.compute_platform.cuda, "version", "13.0.2"),
        lambda config: setattr(config.compute_platform.cuda, "image_flavor", "devel"),
        lambda config: setattr(
            config.compute_platform.cuda,
            "image_distro",
            "ubuntu22.04",
        ),
        lambda config: setattr(config.system, "extra_packages", ["ffmpeg"]),
        lambda config: setattr(config.system, "env", {"HF_HOME": "/cache"}),
        lambda config: setattr(config.cdh.downloader.aria2, "split", 8),
        lambda config: setattr(config.cdh, "default_downloader", "httpx"),
        lambda config: setattr(config.python, "version", "3.13"),
        lambda config: setattr(config.python, "index_url", "https://mirror/simple"),
        lambda config: setattr(config.python, "extra_packages", ["httpx"]),
        lambda config: setattr(config.pytorch, "version", "2.11"),
        lambda config: setattr(
            config.pytorch,
            "index_base_url",
            "https://mirror/pytorch",
        ),
        lambda config: setattr(config.pytorch, "extra_packages", ["torchvision"]),
        lambda config: setattr(config.build, "tags", ["example:dev"]),
        lambda config: setattr(config.build, "output", "push"),
        lambda config: setattr(config.comfyui, "install_manager", False),
        lambda config: setattr(config.comfyui, "launch_args", ["--cpu"]),
        lambda config: setattr(
            config,
            "files",
            [
                {
                    "url": "https://example.com/model.bin",
                    "dir": "models",
                    "filename": "model.bin",
                }
            ],
        ),
    ],
)
def test_lock_input_digest_ignores_non_lock_config_changes(mutate) -> None:
    """Keep non-lock build, package, downloader, file, and runtime fields out."""
    base = make_config()
    changed = make_config()

    mutate(changed)

    assert compute_lock_input_digest(changed) == compute_lock_input_digest(base)


def test_lock_input_digest_ignores_custom_node_install_details() -> None:
    """Keep hooks and Git target directories out of the lock input digest."""
    base = make_config()
    base.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "registry-node", "version": "1.0.0"}
        ),
        GitCustomNodeConfig.model_validate(
            {
                "type": "git",
                "url": "https://example.com/git-node.git",
                "ref": "main",
            }
        ),
    ]
    changed = make_config()
    changed.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {
                "type": "registry",
                "id": "registry-node",
                "version": "1.0.0",
                "pre_install_scripts": ["prepare.sh"],
                "post_install_scripts": ["finish.py"],
            }
        ),
        GitCustomNodeConfig.model_validate(
            {
                "type": "git",
                "url": "https://example.com/git-node.git",
                "ref": "main",
                "target_dir": "custom-target",
                "pre_install_scripts": ["prepare.sh"],
                "post_install_scripts": ["finish.py"],
            }
        ),
    ]

    assert compute_lock_input_digest(changed) == compute_lock_input_digest(base)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: setattr(config.comfyui, "version", "nightly"),
        lambda config: setattr(config.comfyui, "cli_version", "1.5.0"),
        lambda config: setattr(
            config.comfyui,
            "custom_nodes",
            [
                RegistryCustomNodeConfig.model_validate(
                    {"type": "registry", "id": "node", "version": "1.0.0"}
                )
            ],
        ),
        lambda config: setattr(
            config.comfyui,
            "custom_nodes",
            [
                GitCustomNodeConfig.model_validate(
                    {"type": "git", "url": "https://example.com/node.git"}
                )
            ],
        ),
        lambda config: setattr(
            config.comfyui,
            "custom_nodes",
            [
                GitCustomNodeConfig.model_validate(
                    {
                        "type": "git",
                        "url": "https://example.com/node.git",
                        "ref": "v1.0.0",
                    }
                )
            ],
        ),
    ],
)
def test_lock_input_digest_changes_for_lock_selectors(mutate) -> None:
    """Change the digest when a lock-relevant selector changes."""
    base = make_config()
    changed = make_config()

    mutate(changed)

    assert compute_lock_input_digest(changed) != compute_lock_input_digest(base)


def test_lock_input_digest_normalizes_omitted_and_latest_registry_version() -> None:
    """Treat omitted and latest registry selectors as the same moving selector."""
    omitted = make_config()
    omitted.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "node"})
    ]
    latest = make_config()
    latest.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "node", "version": "latest"}
        )
    ]

    assert compute_lock_input_digest(omitted) == compute_lock_input_digest(latest)


def test_lock_input_digest_normalizes_supported_version_selectors() -> None:
    """Use the same selector normalization as config validation."""
    plain = make_config()
    plain.comfyui.version = "1.2.3"
    plain.comfyui.cli_version = "2.0rc1"
    plain.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "node", "version": "1.2.3"}
        )
    ]
    equivalent = make_config()
    equivalent.comfyui.version = "v1.2.3"
    equivalent.comfyui.cli_version = "v2.0RC1"
    equivalent.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "node", "version": "v1.2.3"}
        )
    ]

    assert compute_lock_input_digest(plain) == compute_lock_input_digest(equivalent)


def test_lock_input_digest_sorts_custom_nodes_by_mapping_key() -> None:
    """Avoid digest changes from declaration order once lock-domain keys are unique."""
    first = make_config()
    first.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "b", "version": "1.0.0"}
        ),
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/b.git", "ref": "main"}
        ),
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "a", "version": "2.0.0"}
        ),
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/a.git", "ref": "v1"}
        ),
    ]
    second = make_config()
    second.comfyui.custom_nodes = [
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/a.git", "ref": "v1"}
        ),
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "a", "version": "2.0.0"}
        ),
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/b.git", "ref": "main"}
        ),
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "b", "version": "1.0.0"}
        ),
    ]

    assert compute_lock_input_digest(first) == compute_lock_input_digest(second)


def test_lock_domain_helper_rejects_duplicate_registry_ids() -> None:
    """Expose deterministic duplicate ID rejection for lock-domain callers."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "same"}),
        RegistryCustomNodeConfig.model_validate(
            {"type": "registry", "id": "same", "version": "1.0.0"}
        ),
    ]

    with pytest.raises(
        LockDomainError,
        match=r"comfyui\.custom_nodes\[1\]\.id: same",
    ):
        validate_lock_domain_unique(config)


def test_lock_domain_helper_rejects_duplicate_git_urls() -> None:
    """Expose deterministic duplicate URL rejection for lock-domain callers."""
    config = make_config()
    config.comfyui.custom_nodes = [
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/node.git", "ref": "one"}
        ),
        GitCustomNodeConfig.model_validate(
            {"type": "git", "url": "https://example.com/node.git", "ref": "two"}
        ),
    ]

    with pytest.raises(
        LockDomainError,
        match=r"comfyui\.custom_nodes\[1\]\.url: https://example\.com/node\.git",
    ):
        validate_lock_domain_unique(config)


def test_lock_input_digest_rejects_duplicates_before_hashing() -> None:
    """Fail deterministically instead of hashing an ambiguous minimal lock domain."""
    config = make_config()
    config.comfyui.custom_nodes = [
        RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "same"}),
        RegistryCustomNodeConfig.model_validate({"type": "registry", "id": "same"}),
    ]

    with pytest.raises(LockDomainError):
        compute_lock_input_digest(config)
