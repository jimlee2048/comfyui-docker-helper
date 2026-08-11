"""Native Windows coverage for the Docker/Buildx host adapter."""

import csv
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfyui_docker_helper.host import cli as host_cli
from comfyui_docker_helper.host.buildx import (
    FileSecretBinding,
    build_image_with_buildx,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows")


def test_windows_buildx_preserves_unicode_spaces_and_csv_comma(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = tmp_path / "build context 构建"
    context.mkdir()
    source = tmp_path / "secret files" / "凭据,primary.txt"
    source.parent.mkdir()
    source.write_text("not-forwarded-by-the-mock")
    assert context.drive
    assert source.drive
    calls: list[dict[str, object]] = []

    def build(received_context: Path, **kwargs: object):
        calls.append({"context": received_context, **kwargs})
        yield "progress"

    monkeypatch.setattr(
        "comfyui_docker_helper.host.buildx.DockerClient",
        lambda: SimpleNamespace(buildx=SimpleNamespace(build=build)),
    )

    build_image_with_buildx(
        image_tags=("image:test",),
        context_dir=context,
        forward_default_ssh=True,
        file_secret_bindings=(FileSecretBinding("credential", source),),
        log=lambda message: None,
    )

    assert calls[0]["context"] == context.resolve()
    assert calls[0]["ssh"] == "default"
    rendered_secret = calls[0]["secrets"]
    assert isinstance(rendered_secret, list)
    assert next(csv.reader(rendered_secret)) == [
        "type=file",
        "id=credential",
        f"src={source}",
    ]


def test_windows_known_hosts_discovery_stays_in_the_user_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_profile = tmp_path / "User Profile 用户"
    known_hosts = user_profile / ".ssh" / "known_hosts"
    known_hosts.parent.mkdir(parents=True)
    known_hosts.write_text("host-key")
    monkeypatch.setenv("USERPROFILE", str(user_profile))

    bindings = host_cli._collect_default_known_hosts_bindings()

    assert bindings == (
        FileSecretBinding(
            secret_id="cdh-ssh-known-hosts-user",
            source=known_hosts,
        ),
    )
