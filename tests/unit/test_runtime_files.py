"""Tests for internal runtime file download planning."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

import httpx
import pytest

from comfyui_docker_helper.config import RuntimeConfigurationError, load_runtime_config
from comfyui_docker_helper.config.runtime_projection import RuntimeConfig
from comfyui_docker_helper.container.download_files import (
    DownloaderSettings,
    FileDownloadItem,
    HttpxDownloader,
    Logger,
)
from comfyui_docker_helper.container.runtime_files import (
    RuntimeFileDownloadError,
    RuntimeFilePlanError,
    build_runtime_file_plan,
    download_runtime_files,
    merge_runtime_file_items,
    process_runtime_file_downloads,
)


def _identities(error: RuntimeFilePlanError) -> list[tuple[tuple, str]]:
    return [(diagnostic.path, diagnostic.code) for diagnostic in error.diagnostics]


class FakeDownloadBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[FileDownloadItem, DownloaderSettings]] = []

    def download(
        self,
        item: FileDownloadItem,
        settings: DownloaderSettings,
    ) -> None:
        self.calls.append((item, settings))


class FakeManagedDownloadBackend(FakeDownloadBackend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakeManagedDownloadBackend:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.exited = True


class FakeAria2Factory:
    def __init__(self, backend: FakeManagedDownloadBackend) -> None:
        self.backend = backend
        self.calls: list[Logger] = []

    def __call__(self, *, log: Logger) -> FakeManagedDownloadBackend:
        self.calls.append(log)
        return self.backend


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


def test_runtime_file_download_selects_explicit_backend_before_default(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/httpx.bin",
                        "dir": "models",
                        "filename": "httpx.bin",
                        "downloader": "httpx",
                    },
                    {
                        "url": "https://example.com/default.bin",
                        "dir": "models",
                        "filename": "default.bin",
                    },
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    config = RuntimeConfig.model_validate({"cdh": {"default_downloader": "aria2"}})
    httpx_backend = FakeDownloadBackend()
    aria2_backend = FakeDownloadBackend()

    results = process_runtime_file_downloads(
        plan,
        config=config,
        backends={"httpx": httpx_backend, "aria2": aria2_backend},
        log=lambda message: None,
    )

    assert [result.backend for result in results] == ["httpx", "aria2"]
    assert [call[0].url for call in httpx_backend.calls] == [
        "https://example.com/httpx.bin"
    ]
    assert [call[0].url for call in aria2_backend.calls] == [
        "https://example.com/default.bin"
    ]
    assert httpx_backend.calls[0][0].target == (
        comfyui / "models" / ".httpx.bin.cdh-download"
    )
    assert aria2_backend.calls[0][0].target == (
        comfyui / "models" / ".default.bin.cdh-download"
    )
    assert httpx_backend.calls[0][0].target != results[0].item.target
    assert aria2_backend.calls[0][0].target != results[1].item.target


def test_runtime_file_download_uses_runtime_downloader_settings(
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
                        "dir": "models",
                        "filename": "a.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    config = RuntimeConfig.model_validate(
        {
            "cdh": {
                "default_downloader": "httpx",
                "downloader": {
                    "aria2": {
                        "rpc_port": 6811,
                        "split": 4,
                        "max_connection_per_server": 5,
                        "min_split_size": "2M",
                        "resume_download": False,
                    },
                    "httpx": {"timeout": 12.5, "retries": 1},
                },
            }
        }
    )
    backend = FakeDownloadBackend()

    process_runtime_file_downloads(
        plan,
        config=config,
        backends={"aria2": backend},
        log=lambda message: None,
    )

    settings = backend.calls[0][1]
    assert settings.default == "httpx"
    assert settings.aria2.rpc_port == 6811
    assert settings.aria2.split == 4
    assert settings.aria2.max_connection_per_server == 5
    assert settings.aria2.min_split_size == "2M"
    assert settings.aria2.resume_download is False
    assert settings.httpx.timeout == 12.5
    assert settings.httpx.retries == 1


def test_runtime_file_httpx_download_writes_staging_not_final_target(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/a.bin"
        return httpx.Response(200, content=b"runtime-bytes")

    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
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

    results = download_runtime_files(
        plan,
        config=RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}}),
        httpx_downloader=HttpxDownloader(
            transport=httpx.MockTransport(handler),
            sleep=lambda seconds: None,
        ),
        log=lambda message: None,
    )

    final_target = comfyui / "models" / "a.bin"
    staging_target = comfyui / "models" / ".a.bin.cdh-download"
    assert results[0].staging_target == staging_target
    assert staging_target.read_bytes() == b"runtime-bytes"
    assert not final_target.exists()


def test_runtime_file_aria2_factory_is_used_only_when_needed(
    tmp_path: Path,
) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    httpx_plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/httpx.bin",
                        "dir": "models",
                        "filename": "httpx.bin",
                        "downloader": "httpx",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    aria2_plan = build_runtime_file_plan(
        [
            {
                "files": [
                    {
                        "url": "https://example.com/aria2.bin",
                        "dir": "models",
                        "filename": "aria2.bin",
                        "downloader": "aria2",
                    }
                ]
            }
        ],
        comfyui_path=comfyui,
    )
    httpx_backend = FakeDownloadBackend()
    aria2_backend = FakeManagedDownloadBackend()
    factory = FakeAria2Factory(aria2_backend)
    config = RuntimeConfig.model_validate({"cdh": {"default_downloader": "httpx"}})

    download_runtime_files(
        httpx_plan,
        config=config,
        httpx_downloader=httpx_backend,
        aria2_downloader_factory=factory,
        log=lambda message: None,
    )
    assert factory.calls == []

    results = download_runtime_files(
        aria2_plan,
        config=config,
        httpx_downloader=httpx_backend,
        aria2_downloader_factory=factory,
        log=lambda message: None,
    )

    assert len(factory.calls) == 1
    assert aria2_backend.entered is True
    assert aria2_backend.exited is True
    assert aria2_backend.calls[0][0].target == (
        comfyui / "models" / ".aria2.bin.cdh-download"
    )
    assert results[0].staging_target == aria2_backend.calls[0][0].target


def test_runtime_file_download_reports_unavailable_backend(tmp_path: Path) -> None:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    plan = build_runtime_file_plan(
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

    with pytest.raises(RuntimeFileDownloadError) as error:
        process_runtime_file_downloads(
            plan,
            config=RuntimeConfig.model_validate(
                {"cdh": {"default_downloader": "aria2"}}
            ),
            backends={"httpx": FakeDownloadBackend()},
            log=lambda message: None,
        )

    assert [(item.path, item.code) for item in error.value.diagnostics] == [
        (("files", 0, "downloader"), "runtime_file.downloader_unavailable")
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
