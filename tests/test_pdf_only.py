"""The public edition reads PDF files only."""
import importlib
import re

import pytest

from book2pdf.batch import discover, run_batch
from book2pdf.models import Options
from book2pdf.state import sha256
from book2pdf.worker import convert


def repair(source, tmp_path):
    return convert(source, tmp_path / 'out.pdf', Options(), tmp_path / 'state.sqlite3')


def test_valid_pdf_is_copied_byte_for_byte(book, tmp_path):
    result = repair(book, tmp_path)
    assert result.status == 'PASS_EXACT', result.error
    assert sha256(tmp_path / 'out.pdf') == sha256(book)


def test_damaged_pdf_is_rebuilt_and_source_kept(tmp_path, pdf_bytes):
    data = re.sub(rb'xref.*', b'', pdf_bytes, flags=re.S)
    source = tmp_path / 'damaged.pdf'
    source.write_bytes(data)
    result = repair(source, tmp_path)
    assert result.status == 'PASS_REPAIRED', result.error
    assert result.page_count == 3
    assert source.read_bytes() == data


def test_other_file_types_are_refused(tmp_path, pdf_bytes):
    source = tmp_path / 'source.book'
    source.write_bytes(pdf_bytes)
    result = repair(source, tmp_path)
    assert not result.status.startswith('PASS')
    assert not (tmp_path / 'out.pdf').exists()


def test_batch_scan_ignores_other_file_types_and_its_own_output(tmp_path, pdf_bytes):
    source = tmp_path / 'input'
    output = source / 'repaired'
    output.mkdir(parents=True)
    (source / 'a.pdf').write_bytes(pdf_bytes)
    (source / 'b.book').write_bytes(pdf_bytes)
    (output / 'old.pdf').write_bytes(pdf_bytes)
    found = [item[0] if isinstance(item, tuple) else item for item in discover([source], True, exclude=output)]
    assert [path.name for path in found] == ['a.pdf']


@pytest.mark.parametrize('name', ['envelope', 'bkf', 'container_decode', 'viewer'])
def test_removed_modules_stay_removed(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f'book2pdf.{name}')
