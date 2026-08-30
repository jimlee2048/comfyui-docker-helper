"""Shared URL and download-target validation helpers."""

from typing import Literal
from urllib.parse import urlsplit

from comfyui_docker_helper.config.validation.values import has_control_characters

type DownloaderName = Literal["aria2", "httpx"]

DOWNLOADERS: frozenset[DownloaderName] = frozenset({"aria2", "httpx"})
TRANSFER_STAGING_DIRECTORY_NAME = ".cdh-staging"


def is_http_url(url: str) -> bool:
    """Return whether a URL is HTTP(S), host-qualified, and consumer-safe."""
    if has_control_characters(url):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(hostname)
        and "\\" not in parsed.netloc
        and not any(character.isspace() for character in parsed.netloc)
    )


def normalize_downloader_name(value: str) -> DownloaderName | None:
    """Return a supported internal downloader name, if one was provided."""
    if value in DOWNLOADERS:
        return value
    return None


def require_downloader_name(value: str) -> DownloaderName:
    """Return a supported downloader name after public validation has run."""
    downloader = normalize_downloader_name(value)
    if downloader is None:
        raise ValueError(f"unsupported downloader: {value}")
    return downloader


def is_reserved_file_target_component(value: str) -> bool:
    """Return whether one target component belongs to transfer staging."""
    return value == TRANSFER_STAGING_DIRECTORY_NAME
