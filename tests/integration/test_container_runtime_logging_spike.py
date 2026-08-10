"""Development-only real-FD spike for the future runtime log broker."""

from __future__ import annotations

import errno
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable


def _write_all(
    fd: int,
    data: bytes,
    *,
    writer: Callable[[int, bytes | memoryview], int] = os.write,
) -> None:
    remaining = memoryview(data)
    while remaining:
        try:
            written = writer(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("saved output descriptor made no write progress")
        remaining = remaining[written:]


def test_fd_tee_write_all_retries_interruption_and_partial_writes() -> None:
    writes: list[bytes] = []
    attempts = 0

    def writer(fd: int, data: bytes | memoryview) -> int:
        nonlocal attempts
        assert fd == 42
        attempts += 1
        if attempts == 1:
            raise InterruptedError
        chunk = bytes(data[:2])
        writes.append(chunk)
        return len(chunk)

    _write_all(42, b"abcdef", writer=writer)

    assert attempts == 4
    assert b"".join(writes) == b"abcdef"


def test_fd_tee_primary_failure_is_observable() -> None:
    read_fd, write_fd = os.pipe()
    failed_saved_fd = os.dup(write_fd)
    os.close(failed_saved_fd)
    failures: queue.Queue[OSError] = queue.Queue(maxsize=1)

    def drain() -> None:
        try:
            chunk = os.read(read_fd, 1024)
            _write_all(failed_saved_fd, chunk)
        except OSError as error:
            failures.put_nowait(error)
        finally:
            os.close(read_fd)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        os.write(write_fd, b"fatal")
    finally:
        os.close(write_fd)
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert failures.get_nowait().errno == errno.EBADF


_REAL_FD_SPIKE = r"""
import os
import queue
import subprocess
import sys
import threading


def write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("write made no progress")
        remaining = remaining[written:]


sys.stdout.flush()
sys.stderr.flush()
saved_stdout = os.dup(1)
saved_stderr = os.dup(2)
stdout_read, stdout_write = os.pipe()
stderr_read, stderr_write = os.pipe()
for fd in (saved_stdout, saved_stderr, stdout_read, stderr_read):
    os.set_inheritable(fd, False)
os.dup2(stdout_write, 1, inheritable=True)
os.dup2(stderr_write, 2, inheritable=True)
os.close(stdout_write)
os.close(stderr_write)

observed = {"stdout": [], "stderr": []}
slow_follower = queue.Queue(maxsize=1)
slow_follower.put_nowait(b"already-full")
failures = queue.Queue(maxsize=2)


def drain(stream, read_fd, saved_fd):
    try:
        while True:
            chunk = os.read(read_fd, 8192)
            if not chunk:
                break
            write_all(saved_fd, chunk)
            observed[stream].append(chunk)
            try:
                slow_follower.put_nowait((stream, chunk))
            except queue.Full:
                pass
    except BaseException as error:
        failures.put_nowait(error)
    finally:
        os.close(read_fd)
        os.close(saved_fd)


threads = (
    threading.Thread(
        target=drain,
        args=("stdout", stdout_read, saved_stdout),
        daemon=True,
    ),
    threading.Thread(
        target=drain,
        args=("stderr", stderr_read, saved_stderr),
        daemon=True,
    ),
)
for thread in threads:
    thread.start()

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
        raise SystemExit(11)
sys.stdin.buffer.read(1)
os.write(1, b"late-out")
os.write(2, b"late-err")
'''
descendant = subprocess.Popen(
    [
        sys.executable,
        "-c",
        descendant_code,
        str(saved_stdout),
        str(saved_stderr),
        str(stdout_read),
        str(stderr_read),
    ],
    stdin=subprocess.PIPE,
    close_fds=False,
)

# A generation boundary must not join the controller-lifetime drains or wait for
# EOF: the descendant still owns fd 1/2 here. Releasing it afterwards proves its
# late bytes continue through the same primary output path.
assert descendant.stdin is not None
descendant.stdin.write(b"x")
descendant.stdin.close()
if descendant.wait(timeout=5.0) != 0:
    os._exit(12)

os.close(1)
os.close(2)
for thread in threads:
    thread.join(timeout=5.0)
if any(thread.is_alive() for thread in threads) or not failures.empty():
    os._exit(13)
if b"".join(observed["stdout"]) != stdout_prefix + payload + b"late-out":
    os._exit(14)
if b"".join(observed["stderr"]) != stderr_prefix + b"late-err":
    os._exit(15)
os._exit(0)
"""


def test_controller_lifetime_fd_tee_preserves_primary_output_without_eof_wait() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _REAL_FD_SPIKE],
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    assert result.returncode == 0
    assert result.stdout == b"out\x00\xfftail" + b"x" * (512 * 1024) + b"late-out"
    assert result.stderr == b"err\x80fragmentlate-err"
