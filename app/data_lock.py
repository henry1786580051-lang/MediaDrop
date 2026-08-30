import os
import sys


class DataDirectoryLock:
    """Keep one backend in charge of a profile until its process exits."""

    def __init__(self, directory):
        self.path = os.path.join(directory, "server.lock")
        self.handle = None

    def __enter__(self):
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Another MediaDrop server is already using this data directory") from exc
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handle is not None:
            # Do not unlink the lock file: another process may already have it open.
            self.handle.close()
            self.handle = None
