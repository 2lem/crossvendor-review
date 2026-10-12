"""Subprocess helper: file-backed stdio, own process group, hard timeout."""

import os
import signal
import subprocess
import time


def _kill_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run(argv, cwd, stdin_path, out_path, err_path, timeout, env=None, grace=5.0):
    """Run argv; return (rc, timed_out, wall_seconds). rc is None when the program could not start.

    Output goes to files and the child runs in its own process group, which is killed on timeout and after exit,
    so a hung descendant cannot hold the caller's pipes.
    """
    start = time.monotonic()
    with open(stdin_path or os.devnull, "rb") as fin, open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
        try:
            child = subprocess.Popen(argv, cwd=cwd, stdin=fin, stdout=fout, stderr=ferr, env=env,
                                     start_new_session=True)
        except OSError as exc:
            ferr.write(("cannot start %s: %s\n" % (argv[0], exc)).encode("utf-8"))
            return None, False, time.monotonic() - start
        timed_out = False
        try:
            rc = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(child.pid, signal.SIGTERM)
            try:
                rc = child.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                _kill_group(child.pid, signal.SIGKILL)
                rc = child.wait()
    _kill_group(child.pid, signal.SIGKILL)
    return rc, timed_out, time.monotonic() - start


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""
