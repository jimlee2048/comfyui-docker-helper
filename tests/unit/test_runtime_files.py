"""Tests for internal runtime file download planning."""

from pathlib import Path

import pytest

from comfyui_docker_helper.config import RuntimeConfigurationError, load_runtime_config
from comfyui_docker_helper.container.runtime_files import (
    RuntimeFilePlanError,
    build_runtime_file_plan,
    merge_runtime_file_items,
)


def _identities(error: RuntimeFilePlanError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


def test_runtime_file_plan_derives_targets_keys_and_first_seen_order(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()

    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models/checkpoints",
                        "filename": "a.bin",
                    },
                    {
                        "url": "https://example.com/b.bin",
                        "dir": "models/loras",
                        "filename": "b.bin",
                        "download_mode": "sync",
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    assert [item.relative_target for item in plan.items] == [
        "models/checkpoints/a.bin",
        "models/loras/b.bin",
    ]
    assert [item.target for item in plan.items] == [
        comfyui / "models" / "checkpoints" / "a.bin",
        comfyui / "models" / "loras" / "b.bin",
    ]
    assert [item.download_mode for item in plan.items] == ["sync", "sync"]
    assert [item.action for item in plan.items] == ["download", "download"]


def test_runtime_file_same_key_merge_and_reset_behavior() -> None:
    merged = merge_runtime_file_items(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": False,
                    },
                    {
                        "url": "https://example.com/b.bin",
                        "dir": "models",
                        "filename": "b.bin",
                    },
                ]
            },
            {
                "files": [
                    {
                        "url": "https://example.com/a2.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": True,
                    },
                    {
                        "url": "https://example.com/c.bin",
                        "dir": "models",
                        "filename": "c.bin",
                    },
                ]
            },
        ]
    )

    assert [
        (item["filename"], item["url"], item.get("overwrite")) for item in merged
    ] == [
        ("a.bin", "https://example.com/a2.bin", True),
        ("b.bin", "https://example.com/b.bin", None),
        ("c.bin", "https://example.com/c.bin", None),
    ]

    assert merge_runtime_file_items(
        [
            {"files": list(merged)},
            {"files": []},
            {
                "files": [
                    {
                        "url": "https://example.com/d.bin",
                        "dir": "models",
                        "filename": "d.bin",
                    }
                ]
            },
        ]
    ) == (
        {
            "url": "https://example.com/d.bin",
            "dir": "models",
            "filename": "d.bin",
        },
    )


def test_runtime_file_merge_accepts_full_runtime_layer_documents(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()

    plan = build_runtime_file_plan(
        [
            {
                "comfyui": {"listen": "127.0.0.1", "port": 8190},
                "cdh": {"default_downloader": "httpx"},
                "files": [
                    {
                        "url": "https://example.com/a.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ],
            },
            {
                "comfyui": {"extra_args": ["--cpu"]},
                "cdh": {"default_download_mode": "sync"},
                "files": [
                    {
                        "url": "https://example.com/a2.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": True,
                    },
                    {
                        "url": "https://example.com/b.bin",
                        "dir": "models",
                        "filename": "b.bin",
                    },
                ],
            },
        ],
        comfyui_path=comfyui,
    )

    assert [
        (item.relative_target, item.url, item.overwrite) for item in plan.items
    ] == [
        ("models/a.bin", "https://example.com/a2.bin", True),
        ("models/b.bin", "https://example.com/b.bin", False),
    ]


def test_runtime_file_merge_ignores_full_runtime_keys_but_validates_files() -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        merge_runtime_file_items(
            [
                {
                    "comfyui": {"listen": "127.0.0.1"},
                    "cdh": {"default_downloader": "httpx"},
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                            "download_mode": "async",
                        }
                    ],
                }
            ]
        )

    assert _identities(error.value) == [
        (("files", 0, "download_mode"), "schema.literal_error")
    ]


@pytest.mark.parametrize(
    ("directory", "code"),
    [
        ("/models", "runtime_file.absolute_directory"),
        ("", "runtime_file.empty_directory_segment"),
        ("models//checkpoints", "runtime_file.empty_directory_segment"),
        ("models/.", "runtime_file.current_directory_segment"),
        ("models/../checkpoints", "runtime_file.parent_directory_segment"),
        ("models/", "runtime_file.trailing_slash"),
    ],
)
def test_runtime_file_plan_rejects_unsafe_directories(
    tmp_path: Path,
    directory: str,
    code: str,
) -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": directory,
                            "filename": "a.bin",
                        }
                    ]
                }
            ],
            comfyui_path=tmp_path / "ComfyUI",
        )

    assert _identities(error.value) == [(("files", 0, "dir"), code)]


@pytest.mark.parametrize(
    "filename", ["", ".", "..", "nested/name.bin", "bad\\name.bin"]
)
def test_runtime_file_plan_rejects_unsafe_filenames(
    tmp_path: Path,
    filename: str,
) -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": filename,
                        }
                    ]
                }
            ],
            comfyui_path=tmp_path / "ComfyUI",
        )

    assert _identities(error.value) == [
        (("files", 0, "filename"), "runtime_file.invalid_filename")
    ]


def test_runtime_file_plan_records_existing_regular_target_behavior(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    target = comfyui / "models" / "a.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/skip.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": False,
                    },
                    {
                        "url": "https://example.com/overwrite.bin",
                        "dir": "models",
                        "filename": "a.bin",
                        "overwrite": True,
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    assert [item.action for item in plan.items] == [
        "overwrite_existing",
    ]
    assert plan.items[0].overwrite is True


def test_runtime_file_plan_existing_regular_target_without_overwrite_skips(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    target = comfyui / "models" / "a.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/skip.bin",
                        "dir": "models",
                        "filename": "a.bin",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )

    assert plan.items[0].action == "skip_existing"


def test_runtime_file_plan_rejects_non_regular_existing_target(tmp_path: Path) -> None:
    comfyui = tmp_path / "ComfyUI"
    target = comfyui / "models" / "a.bin"
    target.mkdir(parents=True)

    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                        }
                    ]
                }
            ],
            comfyui_path=comfyui,
        )

    assert _identities(error.value) == [
        (("files", 0, "target"), "runtime_file.non_regular_target")
    ]


def test_runtime_file_plan_rejects_symlink_escape(tmp_path: Path) -> None:
    comfyui = tmp_path / "ComfyUI"
    outside = tmp_path / "outside"
    comfyui.mkdir()
    outside.mkdir()
    (comfyui / "models").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                        }
                    ]
                }
            ],
            comfyui_path=comfyui,
        )

    assert _identities(error.value) == [
        (("files", 0, "target"), "runtime_file.symlink_escape")
    ]


def test_runtime_file_plan_rejects_unsupported_download_mode(tmp_path: Path) -> None:
    with pytest.raises(RuntimeFilePlanError) as error:
        build_runtime_file_plan(
            [
                {
                    "files": [
                        {
                            "url": "https://example.com/a.bin",
                            "dir": "models",
                            "filename": "a.bin",
                            "download_mode": "async",
                        }
                    ]
                }
            ],
            comfyui_path=tmp_path / "ComfyUI",
        )

    assert _identities(error.value) == [
        (("files", 0, "download_mode"), "schema.literal_error")
    ]


def test_entrypoint_runtime_config_still_rejects_files(tmp_path: Path) -> None:
    mounted = tmp_path / "runtime.toml"
    mounted.write_text(
        """
[[files]]
url = "https://example.com/a.bin"
dir = "models"
filename = "a.bin"
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        load_runtime_config(
            baked_config_path=tmp_path / "missing-baked.toml",
            mounted_config_path=mounted,
            environ={},
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files",), "runtime.files_unsupported")
    ]
