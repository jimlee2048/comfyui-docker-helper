from __future__ import annotations

import os
from pathlib import Path

path = Path(os.environ["COMFYUI_PATH"]) / "cdh-smoke-hook-events.log"
with path.open("a", encoding="utf-8") as output:
    output.write(f"pre.py cwd={Path.cwd()}\n")
    output.write(f"pre.py WORKSPACE={os.environ['WORKSPACE']}\n")
    output.write(f"pre.py COMFYUI_PATH={os.environ['COMFYUI_PATH']}\n")
    output.write(f"pre.py VIRTUAL_ENV={os.environ['VIRTUAL_ENV']}\n")
