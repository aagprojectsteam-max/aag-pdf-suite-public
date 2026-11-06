"""Small OS boundary shared by repair, validation and publishing."""
from contextlib import AbstractContextManager
import errno
import hashlib
import os
from pathlib import Path
import shutil
import sys
import time

IS_WINDOWS = sys.platform == 'win32'


def path_key(path):
    """Identity key for case-insensitive Windows output planning/locking."""
    value = str(Path(path).absolute())
    return value.casefold() if IS_WINDOWS else value


def output_key(path):
    # Keep existing Linux lock/staging names compatible with v1 journals.
    name = Path(path).name.casefold() if IS_WINDOWS else Path(path).name
    return hashlib.sha256(os.fsencode(name)).hexdigest()[:24]


class FileLock(AbstractContextManager):
    """Exclusive process lock; byte locking on Windows, flock on POSIX.

    Nonblocking acquire raises BlockingIOError uniformly. Closing the descriptor
    releases the lock, including on process death. Persistent lock files avoid
    an unlink/recreate race between cooperating writers.
    """
    def __init__(self, path, *, blocking=True):
        path = Path(path)
        if path.is_symlink():
            raise ValueError('Lock path must not be a symbolic link')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        self.file = os.fdopen(fd, 'r+b', buffering=0)
        try:
            if os.fstat(fd).st_nlink > 1:
                raise ValueError('Lock path must not be a hard link')
            if IS_WINDOWS:
                import msvcrt
                # Locking a byte beyond EOF is supported by Windows. Do not write
                # into an existing file just to acquire its advisory lock.
                while True:
                    try:
                        self.file.seek(0)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        if not blocking:
                            raise BlockingIOError(errno.EAGAIN, 'Lock already held', str(path)) from exc
                        time.sleep(.1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BaseException:
            self.file.close()
            raise

    def close(self):
        self.file.close()

    def __exit__(self, *args):
        self.close()


def sync_directory(path):
    if IS_WINDOWS:
        # Windows publication uses MoveFileExW WRITE_THROUGH instead. Opening a
        # directory with POSIX os.open/O_DIRECTORY is not supported there.
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def windows_long_path(path):
    """Extended Unicode Win32 path, including UNC; do not use for GUI labels."""
    value = str(Path(path).absolute())
    if value.startswith('\\\\?\\'):
        return value
    if value.startswith('\\\\'):
        return '\\\\?\\UNC\\' + value[2:]
    return '\\\\?\\' + value


def _windows_publish(temp, target, overwrite):
    import ctypes
    from ctypes import wintypes
    move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    # No COPY_ALLOWED: staging is on the same volume and must remain atomic.
    flags = 0x8 | (0x1 if overwrite else 0)  # WRITE_THROUGH / REPLACE_EXISTING
    if not move(windows_long_path(temp), windows_long_path(target), flags):
        error = ctypes.get_last_error()
        if error in (80, 183):
            raise FileExistsError(errno.EEXIST, 'Existing PDF is protected', str(target))
        if error in (5, 32, 33):
            raise PermissionError(errno.EACCES, 'PDF is open/locked or destination is not writable; close it and retry', str(target))
        raise ctypes.WinError(error)


def publish(temp, target, overwrite=False):
    """Publish a validated, closed staging file without a non-atomic fallback."""
    # Windows FlushFileBuffers needs a writable handle even after all writers
    # have closed; staging files are private and writable by this process.
    with Path(temp).open('r+b') as stream:
        os.fsync(stream.fileno())
    if IS_WINDOWS:
        _windows_publish(temp, target, overwrite)
    elif overwrite:
        os.replace(temp, target)
    else:
        os.link(temp, target)
        Path(temp).unlink()
    sync_directory(Path(target).parent)


def qpdf_executable():
    # Windows wheels bundle libqpdf in pikepdf. Avoid a second external binary and
    # frozen-process DLL search-path interactions. Strict libqpdf + MuPDF remain.
    return None if IS_WINDOWS else shutil.which('qpdf')


def setup_error(message):
    if IS_WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'AAG - PDF Suite', 0x10)
    else:
        import subprocess
        try:
            subprocess.run(['zenity', '--error', '--text=' + message], check=False)
        except OSError:
            print(message, file=sys.stderr)
