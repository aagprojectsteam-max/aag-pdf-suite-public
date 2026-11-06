"""Portable contracts plus native-only Windows filesystem tests."""
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from pathlib import Path
import sys
import pytest

from book2pdf import platforms


def attempt_lock(path):
    try:
        with platforms.FileLock(path, blocking=False):
            return True
    except BlockingIOError:
        return False


def test_lock_across_spawned_processes(tmp_path):
    path = tmp_path / 'נעילה עם רווח.lock'
    with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context('spawn')) as pool:
        with platforms.FileLock(path):
            assert pool.submit(attempt_lock, path).result(timeout=30) is False
        assert pool.submit(attempt_lock, path).result(timeout=30) is True


def test_lock_rejects_hardlink(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'unchanged')
    alias = tmp_path / 'alias'
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match='hard link'):
        platforms.FileLock(alias)
    assert source.read_bytes() == b'unchanged'


def test_publish_unicode_no_clobber_and_replace(tmp_path):
    temp, target = tmp_path / 'זמני.tmp', tmp_path / 'ספר עם רווח.pdf'
    temp.write_bytes(b'new')
    target.write_bytes(b'old')
    with pytest.raises(FileExistsError):
        platforms.publish(temp, target)
    assert temp.read_bytes() == b'new' and target.read_bytes() == b'old'
    platforms.publish(temp, target, overwrite=True)
    assert target.read_bytes() == b'new' and not temp.exists()


def test_windows_case_keys(monkeypatch, tmp_path):
    # Contract simulation is not evidence of native Windows execution.
    monkeypatch.setattr(platforms, 'IS_WINDOWS', True)
    assert platforms.path_key(tmp_path / 'BOOK.pdf') == platforms.path_key(tmp_path / 'book.PDF')
    assert platforms.output_key(tmp_path / 'BOOK.pdf') == platforms.output_key(tmp_path / 'book.PDF')


def test_case_collision_plan(monkeypatch, tmp_path, pdf_bytes):
    from book2pdf.batch import run_batch
    from book2pdf.models import Options
    monkeypatch.setattr(platforms, 'IS_WINDOWS', True)
    sources = []
    for folder, name in [('first', 'Book.pdf'), ('second', 'BOOK.pdf')]:
        source = tmp_path / folder / name
        source.parent.mkdir()
        source.write_bytes(pdf_bytes)
        sources.append(source)
    events = []
    run_batch(sources, tmp_path / 'output', Options(preserve_tree=False), dry_run=True, on_event=events.append)
    rows = [event for event in events if event['type'] == 'dry_run']
    assert len(rows) == 2 and all(row['error'] for row in rows)


def test_windows_no_external_qpdf(monkeypatch):
    monkeypatch.setattr(platforms, 'IS_WINDOWS', True)
    monkeypatch.setattr(platforms.shutil, 'which', lambda _: pytest.fail('Windows must not depend on external qpdf'))
    assert platforms.qpdf_executable() is None


def test_validation_without_external_qpdf(monkeypatch, tmp_path, pdf_bytes):
    from book2pdf import validator
    monkeypatch.setattr(validator, 'qpdf_executable', lambda: None)
    monkeypatch.setattr(validator, 'IS_WINDOWS', True)
    source = tmp_path / 'document.pdf'
    source.write_bytes(pdf_bytes)
    count, warning = validator.validate(source, True)
    assert count == 3 and 'bundled libqpdf' in warning
    source.write_bytes(pdf_bytes[:len(pdf_bytes) // 2])
    with pytest.raises(validator.ValidationError):
        validator.validate(source, True)


def test_thermal_unsupported_does_not_probe_sys(monkeypatch):
    from book2pdf import thermal
    monkeypatch.setattr(thermal.sys, 'platform', 'win32')
    monkeypatch.setattr(Path, 'glob', lambda *_: pytest.fail('No /sys probes on Windows'))
    assert not thermal.supported() and thermal.temperature() is None


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows API required')
def test_windows_locked_target_is_preserved(tmp_path):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    temp, target = tmp_path / 'temp.pdf', tmp_path / 'locked.pdf'
    temp.write_bytes(b'new'); target.write_bytes(b'old')
    # A reader which did not grant FILE_SHARE_DELETE blocks atomic replacement.
    handle = create(str(target), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        with pytest.raises(PermissionError):
            platforms.publish(temp, target, overwrite=True)
        assert target.read_bytes() == b'old' and temp.read_bytes() == b'new'
    finally:
        close(handle)
    platforms.publish(temp, target, overwrite=True)
    assert target.read_bytes() == b'new'


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows path and file APIs required')
def test_windows_long_unicode_paths(tmp_path, pdf_bytes):
    from book2pdf.worker import convert
    from book2pdf.models import Options
    base = tmp_path
    while len(str(base)) < 280:
        base /= 'ספרים עם רווח ושם ארוך'
    base.mkdir(parents=True)
    source, target = base / 'ספר.pdf', base / 'ספר.repaired.pdf'
    source.write_bytes(pdf_bytes)
    result = convert(source, target, Options(), base / 'state.sqlite3')
    assert result.status == 'PASS_EXACT', result.error
    assert result.page_count == 3
    assert platforms.windows_long_path(target).startswith('\\\\?\\')
