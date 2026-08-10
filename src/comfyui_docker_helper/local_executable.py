"""Process-local identity request for one admitted executable input."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class LocalExecutableIdentityRequest:
    """Bridge shared reconciliation intent to host-owned file acquisition."""

    root: Path
    relative_path: PurePosixPath
    identity_path: PurePosixPath | None = None

    @property
    def canonical_path(self) -> PurePosixPath:
        """Return the stable lock identity independently from the source path."""
        return self.identity_path or self.relative_path
