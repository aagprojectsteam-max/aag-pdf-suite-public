"""AAG - PDF Suite: pages, components check, review answers and a real run on a small sample."""
import json
import os
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from aag_pdf_suite import components
from aag_pdf_suite.components import Component, DEDUP, fix_command, missing
from aag_pdf_suite.gui.dedup_page import (DedupPage, Job, engine_args, load_choices, open_pairs, pick_folder,
                                          save_choices)
from aag_pdf_suite.gui.shell import SuiteWindow

SAMPLE = Path(os.environ.get('AAG_SUITE_SAMPLE', '/nonexistent'))


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)


def pair(a, b):
    return {'a': a, 'b': b, 'image_a': 'report_files/a.jpg', 'image_b': 'report_files/b.jpg', 'ppi_a': 150, 'ppi_b': 90,
            'cover_a': 0.9, 'cover_b': 0.8, 'inliers': 70, 'text': 0.4}


def test_open_pairs_skips_answered_and_removed():
    summary = {'review': [pair(1, 2), pair(3, 4), pair(5, 6)]}
    choices = {'remove': [3], 'not_duplicate': [[1, 2]]}
    assert [p['a'] for p in open_pairs(summary, choices)] == [5]


def test_engine_args_carry_saved_removals(tmp_path):
    save_choices(tmp_path, {'remove': [9, 4], 'not_duplicate': []})
    args = engine_args(Job(Path('/x/a.pdf'), tmp_path, ocr=False, deskew=True))
    assert args[args.index('--remove') + 1] == '4,9'
    assert '--no-ocr' in args and '--deskew' in args and '--managed' in args


def test_pick_folder_never_shares_a_folder(tmp_path):
    first, second = tmp_path / 'one' / 'a.pdf', tmp_path / 'two' / 'a.pdf'
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(b'x' * 10)
    root = tmp_path / 'out'
    assert pick_folder(first, root, set(), True, False) == root / 'a'
    assert pick_folder(second, root, {root / 'a'}, True, False) == root / 'a-2'
    work = root / 'a' / '.work'
    work.mkdir(parents=True)
    info = first.stat()
    (work / 'stamp.json').write_text(json.dumps({'size': info.st_size, 'mtime': int(info.st_mtime), 'ocr': True,
                                                 'deskew': False}))
    assert pick_folder(first, root, set(), True, False) == root / 'a'
    assert pick_folder(first, root, set(), True, True) == root / 'a-2'


def test_missing_and_fix_command():
    rows = [Component('a', 'A', '', (DEDUP,), True, 'pkg-a'), Component('b', 'B', '', (DEDUP,), False, 'pkg-b'),
            Component('c', 'C', '', (DEDUP,), True, 'pkg-c', ok=True)]
    assert [row.key for row in missing(rows, DEDUP)] == ['a']
    assert fix_command(rows) == 'sudo apt install pkg-a pkg-b'


def test_missing_tool_is_reported(monkeypatch):
    monkeypatch.setattr(components.shutil, 'which', lambda *_, **__: None)
    found = components.check_tool(components.TOOLS[0])
    assert not found.ok and found.apt == 'ocrmypdf'
    assert not any(row.ok for row in components.check_languages())


def test_blocked_dedup_cannot_start(qtbot, tmp_path, settings):
    page = DedupPage(settings)
    qtbot.addWidget(page)
    source = tmp_path / 'a.pdf'
    source.write_bytes(b'%PDF')
    page.add_paths([source, tmp_path / 'note.txt'])
    assert page.inputs.count() == 1
    page.output_edit.setText(str(tmp_path / 'out'))
    page.set_blocked('חסר')
    assert not page.start_button.isEnabled()
    page.set_blocked('')
    assert page.start_button.isEnabled()
    page.output_edit.setText('')
    assert not page.start_button.isEnabled() and 'יעד' in page.reason.text()


def test_review_saves_answers_and_reruns_only_on_removal(qtbot, tmp_path, settings):
    page = DedupPage(settings)
    qtbot.addWidget(page)
    job = Job(tmp_path / 'a.pdf', tmp_path, True, False, 'done', {'review': [pair(1, 2), pair(3, 4)], 'kept': 1})
    page.jobs = [job]
    page.table.setRowCount(1)
    page.refresh_table()
    page.table.selectRow(0)
    started = []
    page.run_queue = lambda: started.append(True)
    page.open_review()
    assert page.stack.currentIndex() == 1 and page.review.heading.text() == 'זוג 1 מתוך 2'
    page.review.keep_button.click()
    assert page.review.heading.text() == 'זוג 2 מתוך 2'
    assert load_choices(tmp_path)['not_duplicate'] == [[1, 2]]
    page.review.apply_button.click()
    assert not started and page.stack.currentIndex() == 0
    page.open_review()
    assert page.review.heading.text() == 'זוג 1 מתוך 1'
    page.review.remove_b.click()
    assert load_choices(tmp_path) == {'remove': [4], 'not_duplicate': [[1, 2]]}
    page.review.apply_button.click()
    assert started and page.queue == [job]


def test_window_has_three_pages_and_blocks_on_missing_parts(qtbot, settings):
    window = SuiteWindow(settings=settings)
    qtbot.addWidget(window)
    window.show()
    assert window.pages.count() == 3
    assert window.converter.findChild(type(window.health), 'title').text() == 'תיקון PDF'
    assert window.converter.isVisible() and window.converter.parent() is not None
    assert window.converter.width() >= 700
    window.on_checked([Component('ocrmypdf', 'OCRmyPDF', '', (DEDUP,), True, 'ocrmypdf')])
    assert 'OCRmyPDF' in window.dedup.blocked and '🟥' in window.health.text()
    qtbot.waitUntil(lambda: window.components.thread is None, timeout=30000)


def test_converted_pdfs_reach_the_dedup_page(qtbot, tmp_path, settings):
    window = SuiteWindow(settings=settings)
    qtbot.addWidget(window)
    pdf = tmp_path / 'book.pdf'
    pdf.write_bytes(b'%PDF')
    summary = {'TOTAL': 1, 'PASS': 1, 'SKIPPED': 0, 'FAILED': 0, 'UNSUPPORTED': 0, 'CANCELLED': 0}
    result = {'source_path': 'a.pdf', 'output_path': str(pdf), 'status': 'PASS_EXACT', 'error': '', 'warning': ''}
    window.converter.on_event({'type': 'plan', 'total': 1})
    window.converter.on_event({'type': 'result', 'result': result, 'completed': 1, 'summary': summary})
    assert window.dedup.converted == [str(pdf)]
    window.dedup.converted_button.click()
    assert window.dedup.paths() == [pdf]
    qtbot.waitUntil(lambda: window.components.thread is None, timeout=30000)


def test_idle_window_closes_on_first_click(qtbot, settings):
    window = SuiteWindow(settings=settings)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.components.thread is None, timeout=30000)
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=3000)


@pytest.mark.skipif(not SAMPLE.exists(), reason='needs AAG_SUITE_SAMPLE')
def test_real_run_then_stop_and_resume(qtbot, tmp_path, settings):
    page = DedupPage(settings)
    qtbot.addWidget(page)
    page.set_blocked('')
    source = tmp_path / 'book.pdf'
    shutil.copy(SAMPLE, source)
    page.add_paths([source])
    page.output_edit.setText(str(tmp_path / 'out'))
    page.start()
    qtbot.waitUntil(lambda: 'OCR' in page.stage.text(), timeout=60000)
    page.stop()
    qtbot.waitUntil(lambda: not page.busy(), timeout=30000)
    assert page.jobs[0].state == 'stopped'
    assert not (page.jobs[0].out / 'summary.json').exists()
    page.start()
    qtbot.waitUntil(lambda: not page.busy(), timeout=280000)
    job = page.jobs[0]
    assert job.state == 'done', job.tail
    assert job.summary['kept'] + job.summary['removed'] == job.summary['pages'] == 12
    assert (job.out / 'book.clean.pdf').exists() and page.report_button.isEnabled()
