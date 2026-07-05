"""Subprocess choke-point (T-04, DESIGN §4.4, P5).

EVERY external process goes through here so no returncode is ever swallowed
(relay's `capture_output=True` bugs turned failed spawns into silent hangs).
`run` raises on non-zero unless the caller explicitly whitelists a code.
`spawn_detached` launches a background worker whose stdout+stderr go to a log
file — no PTY, no tmux (DESIGN P7); V1 swaps a Windows impl behind this
signature (ROADMAP W1).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class BosunProcError(RuntimeError):
    def __init__(self, cmd: Sequence[str], rc: int, out: str, err: str) -> None:
        self.cmd = list(cmd)
        self.rc = rc
        self.out = out
        self.err = err
        super().__init__(f"command failed (rc={rc}): {' '.join(self.cmd)}\n{err.strip()}")


@dataclass(frozen=True)
class ProcResult:
    rc: int
    out: str
    err: str


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    ok_rc: Sequence[int] = (0,),
) -> ProcResult:
    """Run a command to completion, capturing output. Raises unless rc in ok_rc."""
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode not in ok_rc:
        raise BosunProcError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return ProcResult(proc.returncode, proc.stdout, proc.stderr)


def spawn_detached(
    cmd: Sequence[str],
    *,
    log_path: Path,
    pid_path: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Launch a detached background process; stdout+stderr -> log_path.

    Returns the child pid, which is also written to pid_path BEFORE returning
    so liveness detection never races the spawn (DESIGN §2.3). POSIX impl:
    start_new_session detaches from the controlling terminal. No PTY (P7).
    """
    if sys.platform == "win32":  # pragma: no cover - implemented in ROADMAP W1
        raise NotImplementedError("Windows detach lands in T-17 (ROADMAP W1)")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "ab", buffering=0)  # noqa: SIM115 - handed to child, closed below
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        logf.close()
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def is_alive(pid: int) -> bool:
    """True if the process exists (POSIX signal-0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
