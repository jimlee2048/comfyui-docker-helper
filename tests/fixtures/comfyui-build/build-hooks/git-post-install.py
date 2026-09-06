"""Observe the dependency and installation marker on the successful path."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path

comfyui = Path(os.environ["COMFYUI_PATH"])
assert Path.cwd() == comfyui
node = comfyui / "custom_nodes/git-hook-probe"
assert importlib.metadata.version("pyfiglet") == "1.0.2"
assert json.loads((node / "cdh-hook-install.json").read_text()) == {"pyfiglet": "1.0.2"}
log = comfyui / "cdh-git-hook-probe.log"
assert log.read_text() == "pre-clone\npre-install\ninstall.py\n"
with log.open("a", encoding="utf-8") as output:
    output.write("post-install\n")
