"""Observe the successful root installer before the files overlay."""

import json
import os
import sys
from pathlib import Path

comfyui = Path(os.environ["COMFYUI_PATH"])
node = comfyui / "custom_nodes/local-probe"
assert Path.cwd() == comfyui
assert Path(sys.prefix) == Path("/opt/venv")
assert json.loads((node / "installed.json").read_text())["pyfiglet"] == "1.0.2"
assert (node / "requirements.txt").read_text() == "pyfiglet==1.0.2\n"
assert (node / "stages.txt").read_text() == "pre-install\ninstall.py\n"
with (node / "stages.txt").open("a") as output:
    output.write("post-install\n")
