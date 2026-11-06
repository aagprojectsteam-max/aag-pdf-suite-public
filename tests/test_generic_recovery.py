"""Generic evidence fixtures; no private book is needed for format coverage."""
import io
import json
import re
import zipfile
from pathlib import Path

import pikepdf
import pytest

from book2pdf.models import Options
from book2pdf.recovery.evidence import collect, scan, Ref
from book2pdf.recovery.engine import run
from book2pdf.worker import convert
from book2pdf.state import sha256


def fixture(data,folder):
    folder.mkdir();source=folder/'unknown.pdf';source.write_bytes(data)
    return source,run(source,folder)


def without_xref(data):
    return data[:data.index(b'xref\n')]


def packed(pdf_bytes):
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        buf=io.BytesIO();pdf.save(buf,object_stream_mode=pikepdf.ObjectStreamMode.generate)
        return buf.getvalue()


def test_scanner_ignores_object_markers_strings_and_payload():
    payload=b'999 0 obj\n<< /Type /Page >>\nendobj\n%PDF-1.7'
    data=b'1 0 obj\n<< /Length '+str(len(payload)).encode()+b' >>\nstream\n'+payload+b'\nendstream\nendobj\n2 0 obj\n(3 0 obj endobj)\nendobj'
    records,errors=scan(data)
    assert not errors and [r.ref.number for r in records]==[1,2]
    assert collect(data)[3]['observations']['headers']==[]


def test_missing_xref_trailer_and_header_rebuild(pdf_bytes,tmp_path):
    raw=without_xref(pdf_bytes);raw=raw[raw.index(b'1 0 obj'):]
    source,result=fixture(b'UNKNOWN\x00'+raw,tmp_path/'case')
    assert result['success'],result
    assert result['model']['recovery_level']=='STRUCTURAL_REPAIR'
    assert len(result['model']['pages'])==3
    assert 'cross-reference stream' in result['model']['provenance']['synthesized_structures']


def test_catalog_discovery_and_page_level_recovery(pdf_bytes,tmp_path):
    raw=without_xref(pdf_bytes);records,_,_,_=collect(raw)
    catalog=next(r for r in records if isinstance(r.value,dict) and r.value.get('/Type')=='/Catalog')
    raw=raw[:catalog.start]+raw[catalog.end:]
    source,result=fixture(raw,tmp_path/'case')
    assert result['success'],result
    assert result['model']['recovery_level']=='PAGE_LEVEL_RECOVERY'
    assert len(result['model']['pages'])==3


def test_count_and_parent_metadata_repaired_only_from_kids(pdf_bytes,tmp_path):
    raw=without_xref(pdf_bytes)
    raw=re.sub(rb'/Count\s+3',b'/Count 999',raw)
    raw=re.sub(rb'/Parent\s+\d+\s+0\s+R',b'/Parent 777 0 R',raw)
    _,result=fixture(raw,tmp_path/'case')
    assert result['success'],result
    assert len(result['model']['pages'])==3
    assert len(result['model']['provenance']['synthesized_structures'])>3


@pytest.mark.parametrize('broken',['cycle','orphan','missing_stream','encrypted'])
def test_contradictions_do_not_become_page_or_image_success(pdf_bytes,tmp_path,broken):
    raw=without_xref(pdf_bytes)
    if broken=='cycle':raw=re.sub(rb'/Kids\s*\[[^]]+\]',b'/Kids [2 0 R]',raw)
    if broken=='orphan':raw+=b'999 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj'
    if broken=='missing_stream':raw=raw.replace(b'/Length ',b'/Length 999999 %',1)
    if broken=='encrypted':
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            out=io.BytesIO();pdf.save(out,encryption=pikepdf.Encryption(owner='owner',user='reader'));raw=out.getvalue()
    _,result=fixture(raw,tmp_path/'case')
    assert not result['success'],result


def test_xref_stream_and_compressed_objects(pdf_bytes,tmp_path):
    raw=packed(pdf_bytes);records,expanded,tables,evidence=collect(raw)
    assert expanded and tables[0]['kind']=='stream'
    assert any(row[0]==2 for row in tables[0]['entries'].values())
    _,result=fixture(raw,tmp_path/'case')
    assert result['success'],result


def test_intact_incremental_hybrid_reference_chain(pdf_bytes,tmp_path):
    from book2pdf.provenance import _stamp
    path=tmp_path/'incremental.pdf';path.write_bytes(packed(pdf_bytes))
    _stamp(path,{'test':'incremental fixture'})
    # Classic final xref uses /Prev pointing to the original xref stream.
    raw=path.read_bytes();_,_,tables,_=collect(raw)
    assert {t['kind'] for t in tables}=={'classic','stream'}
    _,result=fixture(raw,tmp_path/'case')
    assert result['success'],result


def test_multiple_valid_embedded_documents_are_not_selected(pdf_bytes,tmp_path):
    from book2pdf.recovery.evidence import EvidenceError
    with pytest.raises(EvidenceError,match='Multiple validated embedded'):
        fixture(pdf_bytes+b'\nWRAPPER\n'+pdf_bytes,tmp_path/'case')


def jpeg(value):
    from PIL import Image
    out=io.BytesIO();Image.new('RGB',(48,64),(value,60,100)).save(out,format='JPEG');return out.getvalue()


def test_ordered_image_archive_stream_copy(tmp_path):
    first,last=jpeg(30),jpeg(90);buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as archive:
        archive.writestr('2.jpg',last);archive.writestr('1.jpg',first)
    _,result=fixture(buf.getvalue(),tmp_path/'case')
    assert result['success'],result
    assert result['model']['recovery_level']=='IMAGE_LEVEL_RECOVERY'
    assert result['model']['assumptions']
    with pikepdf.open(tmp_path/'case'/result['file']) as pdf:
        assert [p.Resources.XObject.Scan.read_raw_bytes() for p in pdf.pages]==[first,last]


@pytest.mark.parametrize('names',[['cover.jpg','page.jpg'],['1.jpg','3.jpg'],['../1.jpg']])
def test_unordered_or_unsafe_images_are_not_ordered_by_guess(tmp_path,names):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as archive:
        for name in names:archive.writestr(name,jpeg(50))
    _,result=fixture(buf.getvalue(),tmp_path/'case')
    assert not result['success']


def test_nested_compression_and_depth_budget(pdf_bytes,tmp_path):
    import gzip,zlib
    raw=gzip.compress(zlib.compress(pdf_bytes))
    _,result=fixture(raw,tmp_path/'nested')
    assert result['success'] and result['model']['recovery_level']=='DECODED'
    assert len(result['model']['provenance']['container_layers'])==2
    _,too_deep=fixture(gzip.compress(raw),tmp_path/'too-deep')
    assert not too_deep['success']


def test_true_hybrid_xrefstm(pdf_bytes,tmp_path):
    from book2pdf.recovery.engine import serialize
    raw=packed(pdf_bytes);_,_,tables,_=collect(raw);table=tables[0]
    final=len(raw);rows=b'xref\n0 1\n0000000000 65535 f \n'
    for number,(kind,offset,generation) in table['entries'].items():
        if kind==1:rows+=f'{number} 1\n{offset:010d} {generation:05d} n \n'.encode()
    trailer={'/Size':table['trailer']['/Size'],'/Root':table['trailer']['/Root'],'/XRefStm':table['offset']}
    hybrid=raw+rows+b'trailer\n'+serialize(trailer)+f'\nstartxref\n{final}\n%%EOF\n'.encode()
    _,result=fixture(hybrid,tmp_path/'hybrid')
    assert result['success'],result
    assert len(result['model']['pages'])==3


def test_damaged_pdf_extension_uses_same_generic_engine(pdf_bytes,tmp_path):
    source=tmp_path/'damaged.pdf';source.write_bytes(without_xref(pdf_bytes))
    result=convert(source,tmp_path/'recovered.pdf',Options(),tmp_path/'state.sqlite3')
    assert result.status=='PASS_REPAIRED',result.error
    assert result.page_count==3


def test_surviving_security_dictionary_blocks_missing_xref_recovery(pdf_bytes,tmp_path):
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        out=io.BytesIO();pdf.save(out,encryption=pikepdf.Encryption(owner='owner',user='reader'))
    raw=without_xref(out.getvalue())
    _,result=fixture(raw,tmp_path/'encrypted')
    assert not result['success']
    assert any('security handler' in str(row) for row in result['attempts'])


def test_indirect_stream_length_and_embedded_marker():
    payload=b'123 0 obj\nnull\nendobj'
    raw=b'1 0 obj\n<< /Length 2 0 R >>\nstream\n'+payload+b'\nendstream\nendobj\n2 0 obj\n'+str(len(payload)).encode()+b'\nendobj'
    records,errors=scan(raw)
    assert not errors and [r.ref.number for r in records]==[1,2]
    damaged=raw.replace(str(len(payload)).encode()+b'\nendobj',b'0\nendobj')
    assert scan(damaged)[1]


def test_report_accepts_structured_and_earlier_descriptive_repair_records(tmp_path):
    from book2pdf.models import Result
    from book2pdf.report import Report
    result=Result('source.pdf','output.pdf',status='PASS_REPAIRED',reconstructed_objects=[
        'cross-reference stream',{'object':'page tree','replacement':'source Kids order','uncertainty':'recorded'}])
    path=tmp_path/'report.jsonl';report=Report(path)
    try:report.write(result)
    finally:report.close()
    assert json.loads(path.read_text())['reconstructed_objects']==result.reconstructed_objects
    assert 'cross-reference stream' in path.with_suffix('.jsonl.log').read_text()
