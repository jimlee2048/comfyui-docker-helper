"""Prove source preparation and patch the optional root requirements."""

import importlib.metadata
import os
import sys
from pathlib import Path

comfyui = Path(os.environ["COMFYUI_PATH"])
node = comfyui / "custom_nodes/local-probe"
assert Path.cwd() == comfyui
assert Path(sys.prefix) == Path("/opt/venv")
assert (node / "revision.txt").is_file()
assert not (node / ".git").exists()
assert not (node / "ignored").exists()
try:
    importlib.metadata.distribution("pyfiglet")
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise RuntimeError("local fixture drift: pyfiglet is already installed")
(node / "requirements.txt").write_text("pyfiglet==1.0.2\n")
(node / "stages.txt").write_text("pre-install\n")
