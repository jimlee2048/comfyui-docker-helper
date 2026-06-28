from __future__ import annotations

import os
from pathlib import Path

path = Path(os.environ["COMFYUI_PATH"]) / "cdh-smoke-hook-evidence.log"
with path.open("a", encoding="utf-8") as output:
    output.write(f"post.py cwd={Path.cwd()}\n")
    output.write(f"post.py WORKSPACE={os.environ['WORKSPACE']}\n")
    output.write(f"post.py COMFYUI_PATH={os.environ['COMFYUI_PATH']}\n")
    output.write(f"post.py VIRTUAL_ENV={os.environ['VIRTUAL_ENV']}\n")
