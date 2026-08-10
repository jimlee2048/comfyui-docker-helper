"""Linux real-FD coverage for the production runtime logging broker."""

from __future__ import annotations

import subprocess
import sys

_REAL_FD_BROKER = r"""
import os
import subprocess
import sys

from comfyui_docker_helper.container.runtime_logging import RuntimeLoggingBroker


def write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        remaining = remaining[written:]


broker = RuntimeLoggingBroker()
broker.start()
for pipe in broker._pipes:
    if any(
        os.get_inheritable(fd)
        for fd in (pipe.restore_fd, pipe.writer_fd, pipe.read_fd)
    ):
        os._exit(11)
if not os.get_inheritable(1) or not os.get_inheritable(2):
    os._exit(12)

stdout_prefix = b"out\x00\xfftail"
stderr_prefix = b"err\x80fragment"
payload = b"x" * (512 * 1024)
write_all(1, stdout_prefix)
write_all(2, stderr_prefix)
write_all(1, payload)

descendant_code = r'''
import errno
import os
import sys

for raw_fd in sys.argv[1:]:
    try:
        os.fstat(int(raw_fd))
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
    else:
        raise SystemExit(21)
sys.stdin.buffer.read(1)
os.write(1, b"late-out")
os.write(2, b"late-err")
'''
broker_fds = [
    str(fd)
    for pipe in broker._pipes
    for fd in (pipe.restore_fd, pipe.writer_fd, pipe.read_fd)
]
descendant = subprocess.Popen(
    [sys.executable, "-c", descendant_code, *broker_fds],
    stdin=subprocess.PIPE,
    close_fds=False,
)

# A generation boundary does not close the controller-lifetime broker or wait
# for pipe EOF while this descendant still owns redirected fd 1/2.
assert descendant.stdin is not None
descendant.stdin.write(b"x")
descendant.stdin.close()
if descendant.wait(timeout=5.0) != 0:
    os._exit(13)

broker.close()
if broker.failure() is not None:
    os._exit(14)
os._exit(0)
"""


_PRIMARY_FAILURE = r"""
import errno
import os

from comfyui_docker_helper.container.runtime_logging import RuntimeLoggingBroker


def fail_saved_writer(_fd, _data):
    raise OSError(errno.EBADF, "synthetic saved output failure")


broker = RuntimeLoggingBroker(writer=fail_saved_writer)
broker.start()
os.write(1, b"fatal")
if not broker.wait_for_failure(2.0):
    os._exit(21)
failure = broker.failure()
if failure is None or failure.stream != "stdout":
    os._exit(22)

# The failed primary writer remains actively drained/discarded, so this payload
# cannot fill the pipe and freeze the workload before the owner handles fatal.
payload = b"z" * (512 * 1024)
remaining = memoryview(payload)
while remaining:
    written = os.write(1, remaining)
    remaining = remaining[written:]
broker.close()
os._exit(0)
"""


_CLOSE_WITH_DESCENDANT = r"""
import subprocess
import sys

from comfyui_docker_helper.container.runtime_logging import RuntimeLoggingBroker


broker = RuntimeLoggingBroker()
broker.start()
descendant = subprocess.Popen(
    [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
    stdin=subprocess.PIPE,
    close_fds=False,
)

# The descendant retains redirected fd 1/2 while close must restore the owner
# and return without treating pipe EOF as a lifecycle boundary.
broker.close()
assert descendant.stdin is not None
descendant.stdin.write(b"x")
descendant.stdin.close()
if descendant.wait(timeout=5.0) != 0:
    raise SystemExit(31)
"""


_START_FAILURE = r"""
import errno
import os

from comfyui_docker_helper.container import runtime_logging
from comfyui_docker_helper.container.runtime_logging import (
    RuntimeLoggingBroker,
    RuntimeLoggingError,
)


original_pipe = runtime_logging.os.pipe
calls = 0


def fail_second_pipe():
    global calls
    calls += 1
    if calls == 2:
        raise OSError(errno.EMFILE, "synthetic pipe allocation failure")
    return original_pipe()


runtime_logging.os.pipe = fail_second_pipe
try:
    RuntimeLoggingBroker().start()
except RuntimeLoggingError:
    pass
else:
    raise SystemExit(41)
os.write(1, b"stdout-restored")
os.write(2, b"stderr-restored")
"""


_BUFFERED_CLOSE = r"""
import sys

from comfyui_docker_helper.container.runtime_logging import RuntimeLoggingBroker


broker = RuntimeLoggingBroker()
broker.start()
sys.stdout.write("buffered-stdout")
sys.stderr.write("buffered-stderr")
broker.close()
"""


def test_broker_preserves_raw_primary_output_and_descendant_inheritance() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _REAL_FD_BROKER],
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    assert result.returncode == 0
    assert result.stdout == b"out\x00\xfftail" + b"x" * (512 * 1024) + b"late-out"
    assert result.stderr == b"err\x80fragmentlate-err"


def test_primary_failure_is_observable_without_pipe_backpressure() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PRIMARY_FAILURE],
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr.startswith(b"cdh: Runtime stdout primary output failed:")


def test_broker_close_does_not_wait_for_descendant_pipe_eof() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CLOSE_WITH_DESCENDANT],
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


def test_broker_start_failure_restores_both_primary_streams() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _START_FAILURE],
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    assert result.returncode == 0
    assert result.stdout == b"stdout-restored"
    assert result.stderr == b"stderr-restored"


def test_broker_close_flushes_buffered_bytes_to_saved_primary_streams() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _BUFFERED_CLOSE],
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    assert result.returncode == 0
    assert result.stdout == b"buffered-stdout"
    assert result.stderr == b"buffered-stderr"
