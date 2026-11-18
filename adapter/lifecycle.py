"""Linux job ownership, including cooperative children that leave their group.

This is lifecycle cleanup, not a sandbox. Children must retain the unique job
marker. Never select processes by executable name, parent PID alone or GPU use.
"""
import asyncio
import errno
import os
from pathlib import Path
import signal
import sys
import time

JOB_MARKER = "ISAAC_SCENE_JOB_ID"


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] in {"Z", "X"} else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


class JobProcesses:
    def __init__(self, job_id):
        if not sys.platform.startswith("linux"):
            raise RuntimeError("job ownership requires Linux /proc")
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("invalid job identity")
        self.marker = f"{JOB_MARKER}={job_id}".encode()
        self.known = {}
        self.observed = {}

    def snapshot(self):
        live = {pid: start for pid, start in self.known.items() if identity(pid) == start}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit() or int(path.name) == os.getpid():
                continue
            try:
                if path.stat().st_uid != os.geteuid():
                    continue
                pid = int(path.name)
                start = identity(pid)
                if start is None or self.marker not in (path / "environ").read_bytes().split(b"\0"):
                    continue
                if identity(pid) != start:
                    continue
                live[pid] = start
                # No full argv or environment: SDK helpers may include user IDs.
                self.observed[pid] = {"pid": pid, "start": start,
                    "executable": Path(os.readlink(path / "exe")).name}
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        self.known = live
        return live

    @staticmethod
    def send(pid, start, signum):
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                fd = os.pidfd_open(pid)
                try:
                    if identity(pid) == start:
                        signal.pidfd_send_signal(fd, signum)
                finally:
                    os.close(fd)
                return
            except ProcessLookupError:
                return
            except OSError as exc:
                if exc.errno not in {errno.ENOSYS, errno.EOPNOTSUPP}:
                    raise
        # Older Conda Python lacks pidfd. Recheck birth time; unlike pidfd this
        # narrows but cannot completely close the PID reuse window.
        try:
            if identity(pid) == start:
                os.kill(pid, signum)
        except ProcessLookupError:
            pass

    async def terminate(self, grace_s=2.0):
        """Require two empty scans; keep discovering children during shutdown."""
        for signum in (signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + grace_s
            while True:
                live = self.snapshot()
                if not live:
                    await asyncio.sleep(.05)
                    if not self.snapshot():
                        return {}
                for pid, start in live.items():
                    self.send(pid, start, signum)
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(.05)
        return self.snapshot()
