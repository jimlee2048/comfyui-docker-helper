"""Replace root installation inputs after the locked checkout is available."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
from pathlib import Path
from textwrap import dedent

comfyui = Path(os.environ["COMFYUI_PATH"])
assert Path.cwd() == comfyui
node = comfyui / "custom_nodes/git-hook-probe"
assert node.is_dir()
assert (node / ".git").is_dir()
head = subprocess.run(
    ["git", "-C", str(node), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
assert head == "609f3afaa74b2f88ef9ce8d939626065e3247469"
try:
    importlib.metadata.distribution("pyfiglet")
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise RuntimeError("hook fixture drift: pyfiglet is already installed")

log = comfyui / "cdh-git-hook-probe.log"
assert log.read_text() == "pre-clone\n"
(node / "requirements.txt").write_text("pyfiglet==1.0.2\n", encoding="utf-8")
(node / "install.py").write_text(
    dedent("""\
        import importlib.metadata
        import json
        import os
        from pathlib import Path

        comfyui = Path(os.environ["COMFYUI_PATH"])
        node = comfyui / "custom_nodes/git-hook-probe"
        assert Path.cwd() == node
        version = importlib.metadata.version("pyfiglet")
        assert version == "1.0.2"
        log = comfyui / "cdh-git-hook-probe.log"
        assert log.read_text() == "pre-clone\\npre-install\\n"
        (node / "cdh-hook-install.json").write_text(
            json.dumps({"pyfiglet": version}), encoding="utf-8"
        )
        with log.open("a", encoding="utf-8") as output:
            output.write("install.py\\n")
        """),
    encoding="utf-8",
)
with log.open("a", encoding="utf-8") as output:
    output.write("pre-install\n")
