import hashlib
import json
from pathlib import Path
import re
import threading

import pikepdf
import pytest

from book2pdf.batch import run_batch
from book2pdf.models import Options, Unsupported
from book2pdf.state import sha256
from book2pdf.validator import ValidationError, validate
from book2pdf.worker import convert


def conversion(book, tmp_path, **options):
    return convert(book, tmp_path / 'out.pdf', Options(**options), tmp_path / 'state.sqlite3')


@pytest.mark.parametrize('offset', [0, 1, 73, 2452, 8197])
def test_arbitrary_wrapper(tmp_path, pdf_bytes, offset):
    source = tmp_path / 'a.pdf'
    source.write_bytes(b'X' * offset + pdf_bytes)
    before = sha256(source)
    result = conversion(source, tmp_path)
    assert result.status == ('PASS_DECODED' if offset else 'PASS_EXACT'), result.error
    assert result.page_count == 3
    assert sha256(source) == before


def test_stream_preservation(book, tmp_path, pdf_bytes):
    result = conversion(book, tmp_path)
    assert result.status == 'PASS_EXACT', result.error
    import io
    with pikepdf.open(io.BytesIO(pdf_bytes)) as original, pikepdf.open(tmp_path / 'out.pdf') as output:
        for page_a, page_b in zip(original.pages, output.pages):
            assert page_a.Resources.XObject.Im.read_raw_bytes() == page_b.Resources.XObject.Im.read_raw_bytes()
            assert page_a.Contents.read_raw_bytes() == page_b.Contents.read_raw_bytes()


def test_headerless_intact_objects(tmp_path, pdf_bytes):
    source = tmp_path / 'headerless.pdf'
    first = re.search(rb'\d+ \d+ obj', pdf_bytes).start()
    source.write_bytes(b'Z' * 511 + b'!' * first + pdf_bytes[first:])
    assert conversion(source, tmp_path).status == 'PASS_REPAIRED'


def test_original_absolute_offsets(tmp_path, pdf_bytes):
    wrapper = b'PREFIX' * 31
    shifted = re.sub(rb'(?m)^(\d{10})( \d{5} n)', lambda m: f'{int(m[1]) + len(wrapper):010d}'.encode() + m[2], pdf_bytes)
    shifted = re.sub(rb'(startxref\s+)(\d+)', lambda m: m[1] + str(int(m[2]) + len(wrapper)).encode(), shifted)
    source = tmp_path / 'absolute.pdf'
    source.write_bytes(wrapper + shifted)
    assert conversion(source, tmp_path).status == 'PASS_REPAIRED'


@pytest.mark.parametrize('mutation', ['random', 'false_header', 'truncated'])
def test_reject_unsafe(tmp_path, pdf_bytes, mutation):
    source = tmp_path / 'bad.pdf'
    variants = {
        'random': b'not a pdf' * 50,
        'false_header': b'random %PDF-1.4 fake obj xref trailer startxref 42 %%EOF',
        'truncated': pdf_bytes[:len(pdf_bytes) // 2],
        'no_eof': pdf_bytes.replace(b'%%EOF', b''),
        'broken_xref': re.sub(rb'\d{10} 00000 n', b'9999999999 00000 n', pdf_bytes, count=1),
        'damaged_object': pdf_bytes.replace(b'1 0 obj', b'XXXXXXX', 1),
        'concatenated': pdf_bytes + pdf_bytes,
    }
    source.write_bytes(variants[mutation])
    result = conversion(source, tmp_path)
    assert result.status == 'UNSUPPORTED_AFTER_ANALYSIS', result
    assert not (tmp_path / 'out.pdf').exists()


@pytest.mark.parametrize('mutation',['broken_xref','damaged_catalog_header'])
def test_surviving_content_requires_validated_generic_repair(tmp_path,pdf_bytes,mutation):
    import io
    import pikepdf
    variants={
        'broken_xref':re.sub(rb'\d{10} 00000 n',b'9999999999 00000 n',pdf_bytes,count=1),
        'damaged_catalog_header':pdf_bytes.replace(b'1 0 obj',b'XXXXXXX',1),
    }
    source=tmp_path/'recoverable.pdf';source.write_bytes(variants[mutation])
    result=conversion(source,tmp_path)
    assert result.status=='PASS_REPAIRED',result.error
    assert result.page_count==3 and result.validation_mode=='all_pages'
    assert result.reconstructed_objects
    with pikepdf.open(io.BytesIO(pdf_bytes)) as before,pikepdf.open(tmp_path/'out.pdf',attempt_recovery=False) as after:
        for page_a,page_b in zip(before.pages,after.pages):
            assert page_a.Resources.XObject.Im.read_raw_bytes()==page_b.Resources.XObject.Im.read_raw_bytes()
            assert page_a.Contents.read_raw_bytes()==page_b.Contents.read_raw_bytes()
    assert source.read_bytes()==variants[mutation]


def test_damaged_startxref_repaired(book, tmp_path):
    book.write_bytes(re.sub(rb'(startxref\s+)\d+', rb'\g<1>5', book.read_bytes()))
    assert conversion(book, tmp_path).status == 'PASS_REPAIRED'


def test_resume_and_changed_source(book, tmp_path):
    first = conversion(book, tmp_path)
    assert first.status == 'PASS_EXACT', first.error
    assert conversion(book, tmp_path).status == 'SKIPPED_EXISTING_VALID'
    book.write_bytes(b'NEW' + book.read_bytes())
    assert conversion(book, tmp_path).status == 'FAIL_REPAIR'
    assert conversion(book, tmp_path, overwrite=True).status == 'PASS_DECODED'


def test_resume_requires_output_hash(book, tmp_path):
    assert conversion(book, tmp_path).status == 'PASS_EXACT'
    output = tmp_path / 'out.pdf'
    output.write_bytes(output.read_bytes().replace(b'%PDF', b'%BAD', 1))
    assert conversion(book, tmp_path).status == 'FAIL_REPAIR'


def test_existing_untracked_protected(book, tmp_path):
    (tmp_path / 'out.pdf').write_bytes(b'valuable existing output')
    assert conversion(book, tmp_path).status == 'FAIL_REPAIR'
    assert (tmp_path / 'out.pdf').read_bytes() == b'valuable existing output'
    assert conversion(book, tmp_path, overwrite=True).status == 'PASS_EXACT'


def test_strict_upgrades_require_validation(book, tmp_path):
    assert conversion(book, tmp_path).status == 'PASS_EXACT'
    assert conversion(book, tmp_path, validate_all_pages=True).status == 'FAIL_REPAIR'
    assert conversion(book, tmp_path, validate_all_pages=True, overwrite=True).status == 'PASS_EXACT'


def test_failed_validation_does_not_replace(book, tmp_path, monkeypatch):
    output = tmp_path / 'out.pdf'
    output.write_bytes(b'original')
    def fail(*args):
        raise ValidationError('deliberate failure')
    monkeypatch.setattr('book2pdf.worker.validate', fail)
    result = conversion(book, tmp_path, overwrite=True)
    assert result.status == 'FAIL_VALIDATION'
    assert output.read_bytes() == b'original'
    assert not list(tmp_path.glob('*.tmp.pdf'))


def test_interrupted_staging_removed(book, tmp_path):
    key = hashlib.sha256(b'out.pdf').hexdigest()[:24]
    stale = tmp_path / f'.book2pdf-{key}-aborted.tmp.pdf'
    stale.write_bytes(b'incomplete')
    assert conversion(book, tmp_path).status == 'PASS_EXACT'
    assert not stale.exists()


def test_hardlink_source_protection(book, tmp_path):
    output = tmp_path / 'out.pdf'
    output.hardlink_to(book)
    before = book.read_bytes()
    assert conversion(book, tmp_path, overwrite=True).status == 'FAIL_REPAIR'
    assert book.read_bytes() == before


def test_symlink_source_protection(book, tmp_path):
    output = tmp_path / 'out.pdf'
    try:
        output.symlink_to(book)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Windows symbolic links require Developer Mode or privilege')
        raise
    assert conversion(book, tmp_path, overwrite=True).status == 'FAIL_REPAIR'


def test_recursive_unicode_parallel_reports(tmp_path, pdf_bytes):
    source = tmp_path / 'ספרים עם רווח'
    for sub, name in [('א', 'ספר (1)'), ('ב', "ספר '2'")]:
        folder = source / sub
        folder.mkdir(parents=True)
        (folder / (name + '.pdf')).write_bytes(b'prefix' + pdf_bytes)
    (source / 'bad.pdf').write_bytes(b'bad')
    output = tmp_path / 'יעד'
    summary = run_batch([source], output, Options(recursive=True, jobs=2))
    assert (summary['PASS'], summary['UNSUPPORTED'], summary['TOTAL']) == (2, 1, 3)
    assert len(list(output.rglob('*.pdf'))) == 2
    records = [json.loads(x) for x in Path(summary['REPORT']).read_text().splitlines()]
    assert len(records) == 4
    assert records[-1]['type'] == 'summary'
    summary2 = run_batch([source], output, Options(recursive=True))
    assert summary2['SKIPPED'] == 2


def test_collision_never_picks_winner(tmp_path, pdf_bytes):
    sources = []
    for name in ('one', 'two'):
        folder = tmp_path / name
        folder.mkdir()
        book = folder / 'same.pdf'
        book.write_bytes(pdf_bytes)
        sources.append(book)
    result = run_batch(sources, tmp_path / 'output', Options(overwrite=True))
    assert result['FAILED'] == 2 and result['PASS'] == 0
    assert not list((tmp_path / 'output').glob('*.pdf'))


def test_cancel_finishes_safe_units(tmp_path, pdf_bytes):
    source = tmp_path / 'input'
    source.mkdir()
    for i in range(8):
        (source / f'{i}.pdf').write_bytes(pdf_bytes)
    cancel = threading.Event()
    def event(data):
        if data['type'] == 'started':
            cancel.set()
    result = run_batch([source], tmp_path / 'output', Options(jobs=1), cancel=cancel, on_event=event)
    assert result['PASS'] == 1 and result['CANCELLED'] == 7
    assert len(list((tmp_path / 'output').glob('*.pdf'))) == 1


def test_dry_run_no_output(book, tmp_path):
    output = tmp_path / 'uncreated'
    result = run_batch([book], output, dry_run=True)
    assert result['DRY_RUN'] and not output.exists()


def test_xref_stream_strategy(tmp_path):
    import io
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page.Contents = pikepdf.Stream(pdf, b"q Q\n")
    data = io.BytesIO()
    pdf.save(data, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    source = tmp_path / 'stream.pdf'
    source.write_bytes(data.getvalue())
    result = conversion(source, tmp_path)
    assert result.status == 'PASS_EXACT', result.error
    assert result.conversion_strategy == 'validated-pdf-copy'


def test_thermal_hysteresis(monkeypatch):
    from book2pdf.thermal import ThermalGate
    readings = iter([90, 80, 74, None])
    monkeypatch.setattr('book2pdf.thermal.temperature', lambda: next(readings))
    gate = ThermalGate(Options(thermal_pause=True))
    assert [gate.allows_start() for _ in range(4)] == [False, False, True, True]


def test_output_creation_race_never_clobbers(book,tmp_path,monkeypatch):
    from book2pdf.platforms import publish
    def race(source,target,overwrite=False):
        Path(target).write_bytes(b'other writer')
        return publish(source,target,overwrite)
    monkeypatch.setattr('book2pdf.worker.publish',race)
    assert conversion(book,tmp_path).status=='FAIL_REPAIR'
    assert (tmp_path/'out.pdf').read_bytes()==b'other writer'
    assert not list(tmp_path.glob('*.tmp.pdf'))


def test_source_changed_during_conversion(book,tmp_path,monkeypatch):
    from book2pdf.validator import validate as real_validate
    def changing(*args,**kwargs):
        checked=real_validate(*args,**kwargs)
        book.write_bytes(b'changed externally'+book.read_bytes())
        return checked
    monkeypatch.setattr('book2pdf.worker.validate',changing)
    result=conversion(book,tmp_path)
    assert result.status=='FAIL_REPAIR'
    assert 'Source changed' in result.error
    assert not (tmp_path/'out.pdf').exists()


def test_state_cannot_alias_source(book,tmp_path):
    before=book.read_bytes()
    state=tmp_path/'state.sqlite3'
    state.hardlink_to(book)
    assert conversion(book,tmp_path).status=='FAIL_REPAIR'
    assert book.read_bytes()==before


def test_state_symlink_cannot_alias_source(book,tmp_path):
    before=book.read_bytes()
    state=tmp_path/'state.sqlite3'
    try:
        state.symlink_to(book)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Windows symbolic links require Developer Mode or privilege')
        raise
    assert conversion(book,tmp_path).status=='FAIL_REPAIR'
    assert book.read_bytes()==before


def crash_one_worker(source,target,options,state):
    import os
    if source.stem=='000-crash':
        os._exit(7)
    return convert(source,target,options,state)


def test_native_worker_crash_does_not_stop_remaining(tmp_path,pdf_bytes,monkeypatch):
    source=tmp_path/'input';source.mkdir()
    (source/'000-crash.pdf').write_bytes(pdf_bytes)
    (source/'001-good.pdf').write_bytes(pdf_bytes)
    monkeypatch.setattr('book2pdf.batch.convert',crash_one_worker)
    result=run_batch([source],tmp_path/'output',Options(jobs=1))
    assert result['FAILED']==1 and result['PASS']==1,result
    assert (tmp_path/'output/001-good.pdf').exists()


def test_missing_eof_marker_or_appended_copy_is_kept_as_is(tmp_path, pdf_bytes):
    for number, data in enumerate((pdf_bytes.replace(b'%%EOF', b''), pdf_bytes + pdf_bytes)):
        source = tmp_path / f'{number}.pdf'
        source.write_bytes(data)
        output = tmp_path / f'{number}.out.pdf'
        result = convert(source, output, Options(), tmp_path / 'state.sqlite3')
        assert result.status == 'PASS_EXACT', result.error
        assert output.read_bytes() == data
