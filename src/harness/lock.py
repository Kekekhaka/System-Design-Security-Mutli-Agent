"""An exclusive OS file lock for harness runs.

POSIX uses ``flock``; Windows uses ``msvcrt.locking``, the platform equivalent.
There is no PID lockfile to go stale. Lock contention fails fast.
"""

from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


class LockError(RuntimeError):
    """Another harness run holds the lock."""


class ExclusiveLock:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        try:
            if fcntl is not None:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self._stream.close()
            self._stream = None
            raise LockError(
                f"Another harness run holds the lock {self.path}. Wait for it to finish."
            ) from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._stream is None:
            return False
        try:
            if fcntl is not None:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            else:
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._stream.close()
        self._stream = None
        return False

    @property
    def path_in_use(self) -> Path:
        return self.path
