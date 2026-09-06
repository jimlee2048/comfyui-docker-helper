"""Observe the target before cdh prepares the Git source."""

from __future__ import annotations

import os
from pathlib import Path

comfyui = Path(os.environ["COMFYUI_PATH"])
assert Path.cwd() == comfyui
assert comfyui.parent == Path(os.environ["WORKSPACE"])
assert not os.path.lexists(comfyui / "custom_nodes/git-hook-probe")
with (comfyui / "cdh-git-hook-probe.log").open("x", encoding="utf-8") as output:
    output.write("pre-clone\n")
