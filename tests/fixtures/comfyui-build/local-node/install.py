"""Record root installation after the pre-install patch takes effect."""

import importlib.metadata
import json
import os
import sys
from pathlib import Path

node = Path(__file__).parent
comfyui = Path(os.environ["COMFYUI_PATH"])
assert Path.cwd() == node
assert Path(sys.prefix) == Path("/opt/venv")
assert (node / "stages.txt").read_text() == "pre-install\n"
version = importlib.metadata.version("pyfiglet")
assert version == "1.0.2"
(node / "installed.json").write_text(
    json.dumps(
        {"pyfiglet": version, "revision": (node / "revision.txt").read_text().strip()}
    ),
    encoding="utf-8",
)
with (node / "stages.txt").open("a") as output:
    output.write("install.py\n")
assert node == comfyui / "custom_nodes/local-probe"
