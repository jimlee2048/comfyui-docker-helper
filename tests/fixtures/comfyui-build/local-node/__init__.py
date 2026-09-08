"""Small CPU node whose registration exposes the captured source revision."""

from pathlib import Path


class CDHLocalProbe:
    @classmethod
    def INPUT_TYPES(cls):
        revision = (Path(__file__).parent / "revision.txt").read_text().strip()
        return {"required": {"revision": ([revision],)}}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    CATEGORY = "cdh/acceptance"

    def run(self, revision):
        return (revision,)


NODE_CLASS_MAPPINGS = {"CDHLocalProbe": CDHLocalProbe}
