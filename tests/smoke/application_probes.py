"""Shell and Python probes shared by durable application image acceptance."""

COMFY_CLI_BRIDGE_PROBE = r"""
set -eu

test -x /opt/uv/tools/comfy-cli/bin/python
test ! -e "$COMFYUI_PATH/.venv"
test ! -L "$COMFYUI_PATH/.venv"
test ! -e "$COMFYUI_PATH/venv"
test ! -L "$COMFYUI_PATH/venv"

/opt/uv/tools/comfy-cli/bin/python -c '
import importlib.metadata as metadata
import json
import pathlib
import sys

plan = json.loads(pathlib.Path("/opt/cdh/build/build-plan.json").read_text())
tool = plan["toolchain"]["tool_store"]["comfy_cli"]
assert tool is not None
assert metadata.version("comfy-cli") == tool["version"]
assert pathlib.Path(sys.prefix) == pathlib.Path("/opt/uv/tools/comfy-cli")
manifest = json.loads(pathlib.Path("/opt/cdh/build/manifest.json").read_text())
evidence = manifest["toolchain"]["comfy_cli"]
assert evidence["direct"] == {
    "intended": tool["version"],
    "observed": tool["version"],
}
assert {item["name"]: item["version"] for item in evidence["inventory"]}[
    "comfy-cli"
] == tool["version"]
'
uv --no-config pip check \
  --python /opt/uv/tools/comfy-cli/bin/python \
  --no-python-downloads

for command in comfy comfy-cli comfycli; do
  public="/opt/uv/bin/$command"
  owned="/opt/uv/tools/comfy-cli/bin/$command"
  test -x "$public"
  test "$(readlink -f "$public")" = "$owned"
  "$public" --help >/dev/null
done

/opt/uv/bin/comfy --workspace="$COMFYUI_PATH" launch -- \
  --listen 127.0.0.1 --port 8199 --disable-auto-launch --cpu &
launcher="$!"
application_pid=""
cleanup() {
  trap - EXIT INT TERM
  for pid in "$application_pid" "$launcher"; do
    test -z "$pid" || kill "$pid" 2>/dev/null || true
  done

  cleanup_attempt=0
  while [ "$cleanup_attempt" -lt 10 ]; do
    alive=""
    for pid in "$application_pid" "$launcher"; do
      test -n "$pid" || continue
      if test -d "/proc/$pid" && \
        ! grep -q '^State:[[:space:]]*Z' "/proc/$pid/status"; then
        alive=1
      fi
    done
    test -n "$alive" || break
    cleanup_attempt=$((cleanup_attempt + 1))
    sleep 1
  done

  for pid in "$application_pid" "$launcher"; do
    test -z "$pid" || kill -KILL "$pid" 2>/dev/null || true
  done
  test -z "$application_pid" || wait "$application_pid" 2>/dev/null || true
  wait "$launcher" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

attempt=0
while [ "$attempt" -lt 180 ]; do
  for process in /proc/[0-9]*; do
    test -r "$process/cmdline" || continue
    first="$(tr '\000' '\n' < "$process/cmdline" | sed -n '1p')"
    test "$first" = "/opt/venv/bin/python" || continue
    tr '\000' '\n' < "$process/cmdline" | grep -Fxq -- main.py || continue
    test "$(readlink -f "$process/cwd")" = "$COMFYUI_PATH" || continue
    application_pid="${process##*/}"
    break
  done
  test -z "$application_pid" || break
  kill -0 "$launcher"
  attempt=$((attempt + 1))
  sleep 1
done

test -n "$application_pid"
test "$(tr '\000' '\n' < "/proc/$application_pid/cmdline" | sed -n '1p')" = \
  "/opt/venv/bin/python"
tr '\000' '\n' < "/proc/$application_pid/cmdline" | grep -Fxq -- main.py
test "$(readlink -f "/proc/$application_pid/cwd")" = "$COMFYUI_PATH"

attempt=0
until curl --fail --silent --show-error \
  http://127.0.0.1:8199/system_stats >/dev/null; do
  kill -0 "$application_pid"
  attempt=$((attempt + 1))
  test "$attempt" -lt 180
  sleep 1
done

test ! -e "$COMFYUI_PATH/.venv"
test ! -L "$COMFYUI_PATH/.venv"
test ! -e "$COMFYUI_PATH/venv"
test ! -L "$COMFYUI_PATH/venv"
"""

GIT_PROOF_SOURCE = r"""
def require_real_directory(path):
    path = pathlib.Path(path)
    metadata = path.lstat()
    resolved = path.resolve(strict=True)
    assert not stat.S_ISLNK(metadata.st_mode)
    assert stat.S_ISDIR(metadata.st_mode)
    assert resolved == path
    return resolved

def git_output(repository, *arguments):
    return subprocess.run(
        ["git", "-C", repository, *arguments],
        check=True,
        capture_output=True,
    ).stdout

def absolute_git_path(repository, *arguments):
    output = git_output(repository, *arguments)
    assert output.endswith(b"\n")
    assert output.count(b"\n") == 1
    path = pathlib.Path(os.fsdecode(output[:-1]))
    assert path.is_absolute()
    return path

def git_directories(repository):
    actual = absolute_git_path(repository, "rev-parse", "--absolute-git-dir")
    common = absolute_git_path(
        repository,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    return actual, common

def verify_exact_repository_root(repository):
    top = git_output(
        repository,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
    )
    assert top == os.fsencode(repository) + b"\n"

def verify_exact_detached_head(repository, expected_commit):
    head = git_output(repository, "rev-parse", "--verify", "HEAD")
    assert head == expected_commit.encode("ascii") + b"\n"
    symbolic = subprocess.run(
        ["git", "-C", repository, "symbolic-ref", "-q", "HEAD"],
        check=False,
        capture_output=True,
    )
    assert symbolic.returncode == 1

def verify_root_git_directory(repository):
    dot_git = require_real_directory(repository / ".git")
    assert dot_git == repository / ".git"
    actual, common = git_directories(repository)
    assert actual == dot_git
    assert common == dot_git
    return dot_git

def require_contained_git_directory(path, root_git_directory):
    root_git_directory = require_real_directory(root_git_directory)
    assert path != root_git_directory
    relative = path.relative_to(root_git_directory)
    current = root_git_directory
    for part in relative.parts:
        current = current / part
        assert not stat.S_ISLNK(current.lstat().st_mode)
    return require_real_directory(path)

def verify_submodule_git_directory(repository, root_git_directory):
    dot_git = repository / ".git"
    metadata = dot_git.lstat()
    assert not stat.S_ISLNK(metadata.st_mode)
    assert stat.S_ISREG(metadata.st_mode)
    actual, common = git_directories(repository)
    assert actual == common
    require_contained_git_directory(actual, root_git_directory)

def verify_committed_gitlinks(
    repository,
    repository_root,
    custom_nodes_root,
    root_git_directory,
    seen,
):
    tree = subprocess.run(
        ["git", "-C", repository, "ls-tree", "-rz", "--full-tree", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata_fields, separator, raw_path = record.partition(b"\t")
        fields = metadata_fields.split(b" ", 2)
        assert separator == b"\t"
        assert len(fields) == 3
        mode, object_type, object_id = fields
        if mode != b"160000":
            continue
        assert object_type == b"commit"
        assert len(object_id) == 40
        assert all(character in b"0123456789abcdef" for character in object_id)
        path_text = os.fsdecode(raw_path)
        relative = pathlib.PurePosixPath(path_text)
        assert path_text
        assert not relative.is_absolute()
        assert relative.as_posix() == path_text
        assert all(part not in {"", ".", ".."} for part in relative.parts)
        child = repository.joinpath(*relative.parts)
        child = require_real_directory(child)
        assert child != repository
        assert child.is_relative_to(repository_root)
        assert child.is_relative_to(custom_nodes_root)
        assert child not in seen
        seen.add(child)
        verify_submodule_git_directory(child, root_git_directory)
        verify_exact_repository_root(child)
        verify_exact_detached_head(child, object_id.decode("ascii"))
        verify_committed_gitlinks(
            child,
            repository_root,
            custom_nodes_root,
            root_git_directory,
            seen,
        )

def prove_git_targets(root, nodes):
    root = require_real_directory(root)
    assert root.is_absolute()
    proven = []
    for node in nodes:
        if node["type"] != "git":
            continue
        target = pathlib.Path(node["target"])
        assert target.is_absolute()
        assert target.parent == root
        assert target.name not in {"", ".", ".."}
        assert re.fullmatch(r"[A-Za-z0-9._-]+", target.name) is not None
        assert target not in proven
        target = require_real_directory(target)
        verify_exact_repository_root(target)
        root_git_directory = verify_root_git_directory(target)
        verify_exact_detached_head(target, node["commit"])
        verify_committed_gitlinks(
            target,
            target,
            root,
            root_git_directory,
            {target},
        )
        proven.append(target)
    return frozenset(proven)
"""

REGISTRY_PROOF_SOURCE = r"""
def scan_registry_projects_after_git_proof(root, nodes):
    root = require_real_directory(root)
    excluded_git_targets = prove_git_targets(root, nodes)
    assert all(target.parent == root for target in excluded_git_targets)
    projects = {}
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child in excluded_git_targets:
            continue
        child_metadata = child.lstat()
        assert not stat.S_ISLNK(child_metadata.st_mode)
        if stat.S_ISREG(child_metadata.st_mode):
            continue
        assert stat.S_ISDIR(child_metadata.st_mode)
        resolved_child = require_real_directory(child)
        assert resolved_child.parent == root
        project_file = child / "pyproject.toml"
        try:
            project_metadata = project_file.lstat()
        except FileNotFoundError:
            continue
        assert not stat.S_ISLNK(project_metadata.st_mode)
        assert stat.S_ISREG(project_metadata.st_mode)
        resolved_project = project_file.resolve(strict=True)
        assert resolved_project.parent == resolved_child
        assert resolved_project.is_relative_to(root)
        project = tomllib.loads(project_file.read_bytes().decode("utf-8"))["project"]
        normalized = canonicalize_name(project["name"], validate=True)
        assert normalized not in projects
        projects[normalized] = Version(project["version"])
    expected_projects = {}
    for node in nodes:
        if node["type"] != "registry":
            continue
        normalized = canonicalize_name(node["id"], validate=True)
        assert normalized not in expected_projects
        expected_projects[normalized] = Version(node["version"])
    assert projects == expected_projects
    return projects
"""
