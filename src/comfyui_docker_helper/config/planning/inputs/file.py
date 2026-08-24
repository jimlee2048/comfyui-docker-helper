"""Host-local build-file identity request shared with lock reconciliation."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class LocalFileIdentityRequest:
    """Host-only locator paired with one stable image-relative target identity."""

    source_path: Path
    relative_target: PurePosixPath
