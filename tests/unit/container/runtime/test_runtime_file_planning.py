"""Runtime file planning and identity tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from tests.unit.container.runtime.runtime_file_support import (
    checksum as _checksum,
)
from tests.unit.container.runtime.runtime_file_support import runtime_file as _file
from tests.unit.container.runtime.runtime_file_support import (
    runtime_file_plan as _plan,
)

from comfyui_docker_helper.container.runtime.files.models import (
    RuntimeFilePlanError,
)
from comfyui_docker_helper.container.runtime.files.planning import (
    runtime_file_identity_digest,
    runtime_file_staging_target,
    runtime_file_state_identity_digest,
)


def _identities(error: RuntimeFilePlanError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


def test_runtime_plan_projects_order_targets_modes_and_checksum(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    uppercase = f"sha256:{'AB' * 32}"

    plan = _plan(
        root,
        _file("a.bin", checksum=uppercase),
        _file("b.bin", downloader="aria2", mode="async"),
    )

    assert [item.relative_target for item in plan.items] == [
        "models/a.bin",
        "models/b.bin",
    ]
    assert [item.download_mode for item in plan.items] == ["sync", "async"]
    assert plan.items[0].checksum == uppercase.lower()
    assert plan.items[0].target == root / "models" / "a.bin"


def test_runtime_plan_supports_a_file_in_the_comfyui_root(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    item = _file("root.bin")
    item["target_dir"] = "./"

    planned = _plan(root, item).items[0]

    assert planned.directory == "."
    assert planned.relative_target == "root.bin"
    assert planned.target == root / "root.bin"


def test_runtime_plan_preserves_item_downloader_and_mode_selection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    plan = _plan(
        root,
        _file("implicit.bin"),
        _file("explicit.bin", downloader="httpx", mode="async"),
    )

    assert plan.items[0].downloader is None
    assert plan.items[0].download_mode == "sync"
    assert plan.items[1].downloader == "httpx"
    assert plan.items[1].download_mode == "async"


# Transfer identity follows the requested bytes and destination, while execution
# policies remain outside the resumable staging identity.
def test_runtime_transfer_identity_tracks_source_target_and_checksum_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ComfyUI"
    item = _plan(root, _file("a.bin", checksum=_checksum(b"one"))).items[0]
    baseline = runtime_file_identity_digest(item)

    assert runtime_file_identity_digest(replace(item, overwrite=True)) == baseline
    assert (
        runtime_file_identity_digest(replace(item, download_mode="async")) == baseline
    )
    assert (
        runtime_file_identity_digest(
            replace(item, url="https://example.test/other.bin")
        )
        != baseline
    )
    assert (
        runtime_file_identity_digest(
            replace(item, target=root / "models" / "other.bin")
        )
        != baseline
    )
    assert (
        runtime_file_identity_digest(replace(item, checksum=_checksum(b"two")))
        != baseline
    )


def test_runtime_state_identity_tracks_backend_and_overwrite_but_not_mode(
    tmp_path: Path,
) -> None:
    item = replace(
        _plan(tmp_path / "ComfyUI", _file("a.bin")).items[0],
        downloader="httpx",
    )
    baseline = runtime_file_state_identity_digest(item)

    assert (
        runtime_file_state_identity_digest(replace(item, download_mode="async"))
        == baseline
    )
    assert runtime_file_state_identity_digest(replace(item, overwrite=True)) != baseline
    assert (
        runtime_file_state_identity_digest(replace(item, downloader="aria2"))
        != baseline
    )
    assert runtime_file_identity_digest(
        replace(item, overwrite=True)
    ) == runtime_file_identity_digest(item)
    assert runtime_file_identity_digest(
        replace(item, downloader="aria2")
    ) == runtime_file_identity_digest(item)


def test_runtime_staging_uses_transfer_identity_digest(tmp_path: Path) -> None:
    item = _plan(tmp_path / "ComfyUI", _file("a.bin")).items[0]
    digest = runtime_file_identity_digest(item)

    assert runtime_file_staging_target(item) == (
        item.target.parent
        / ".cdh-staging"
        / f"cdh-{digest.removeprefix('sha256:')}.part"
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "target_dir",
            "../models",
            "runtime_file.parent_directory_segment",
        ),
        ("filename", "../a.bin", "runtime_file.invalid_filename"),
        ("url", "file:///tmp/a", "runtime_file.invalid_url"),
        ("checksum", "sha256:bad", "schema.value_error"),
        ("checksum", 123, "schema.string_type"),
        ("download_mode", "later", "schema.literal_error"),
    ],
)
def test_runtime_plan_rejects_invalid_file_fields(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    item = _file("a.bin")
    item[field] = value

    with pytest.raises(RuntimeFilePlanError) as captured:
        _plan(tmp_path / "ComfyUI", item)

    assert _identities(captured.value) == [(("files", 0, field), code)]
