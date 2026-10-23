"""Process lifetime locks on Windows and Unix."""
from contextlib import contextmanager
import os


class ServiceAlreadyRunning(BlockingIOError):
    pass


@contextmanager
def service_lock(path):
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt

            if handle.seek(0, 2) == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ServiceAlreadyRunning('Talkback service is already starting') from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ServiceAlreadyRunning('Talkback service is already starting') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
