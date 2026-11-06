"""Direct JPEG stream copying for a single image or explicitly numbered ZIP pages.

Orphan PDF images, thumbnails and unordered container members are not promoted
into pages. Numeric ZIP order is recorded as an assumption, never an exact PDF.
"""
import io
from pathlib import PurePosixPath
import re
import stat
import zipfile
import hashlib

from .evidence import EvidenceError
from .document import RecoveredDocument


def recover(data,target,family):
    entries=[];assumptions=[]
    if data.startswith(b'\xff\xd8\xff'):
        entries=[('single-original.jpeg',data)]
    elif data.startswith(b'PK\x03\x04'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members=archive.infolist()
            if not 1<=len(members)<=4096:return None
            indexed=[];total=0
            for member in members:
                name=PurePosixPath(member.filename)
                if (member.is_dir() or name.is_absolute() or '..' in name.parts or '\\' in member.filename
                    or len(name.parts)!=1 or member.flag_bits&1 or stat.S_ISLNK(member.external_attr>>16)):
                    return None
                match=re.fullmatch(r'(\d+)\.(?:jpe?g)',member.filename,re.IGNORECASE)
                if not match:return None
                total+=member.file_size
                if member.file_size>64*1024*1024 or total>256*1024*1024 or member.file_size>200*max(1,member.compress_size):
                    raise EvidenceError('Image archive expansion budget')
                indexed.append((int(match[1]),member))
            indexed.sort(key=lambda row:row[0])
            if [n for n,_ in indexed]!=list(range(1,len(indexed)+1)):return None
            entries=[(member.filename,archive.read(member)) for _,member in indexed]
            assumptions=['Page order follows the complete unique numeric ZIP member sequence starting at 1; original PDF page layout is unavailable.']
    else:return None
    from PIL import Image
    import pikepdf
    inventory=[]
    with pikepdf.Pdf.new() as pdf:
        for number,(name,raw) in enumerate(entries,1):
            with Image.open(io.BytesIO(raw)) as image:
                width,height=image.size;mode=image.mode
                if image.format!='JPEG' or mode not in ('L','RGB','CMYK') or not 0<width*height<=64_000_000:
                    raise EvidenceError('Unsupported image metadata or dimension limit')
                image.verify()
            page=pdf.add_blank_page(page_size=(width,height))
            stream=pikepdf.Stream(pdf,raw);stream.Type=pikepdf.Name.XObject;stream.Subtype=pikepdf.Name.Image
            stream.Filter=pikepdf.Name.DCTDecode;stream.Width=width;stream.Height=height;stream.BitsPerComponent=8
            stream.ColorSpace=pikepdf.Name({'L':'/DeviceGray','RGB':'/DeviceRGB','CMYK':'/DeviceCMYK'}[mode])
            # CMYK Adobe JPEG polarity requires transform metadata; avoid guessing.
            if mode=='CMYK':raise EvidenceError('CMYK JPEG polarity requires explicit source colour metadata')
            page.Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Scan=stream))
            page.Contents=pikepdf.Stream(pdf,f'q {width} 0 0 {height} 0 0 cm /Scan Do Q\n'.encode())
            inventory.append({'number':number,'member':name,'jpeg_sha256':hashlib.sha256(raw).hexdigest()})
        pdf.save(target,compress_streams=False,stream_decode_level=pikepdf.StreamDecodeLevel.none)
    from ..validator import validate
    count,warning=validate(target,True)
    with pikepdf.open(target,attempt_recovery=False) as pdf:
        actual=[hashlib.sha256(p.Resources.XObject.Scan.read_raw_bytes()).hexdigest() for p in pdf.pages]
    if actual!=[p['jpeg_sha256'] for p in inventory]:raise EvidenceError('Original JPEG stream changed')
    return RecoveredDocument(family,'IMAGE_LEVEL_RECOVERY',inventory,
        resources={'original_image_streams':len(entries),'preserved_image_streams':len(entries)},
        confidence='ORIGINAL_IMAGES_VALIDATED_LAYOUT_INFERRED',assumptions=assumptions+['PDF page size uses source pixel dimensions at 72 units/inch; original physical page size is unknown.'],
        warnings=[warning] if warning else [],provenance={'strategy':'evidence-pdf-v1','source_sha256':hashlib.sha256(data).hexdigest(),
            'synthesized_structures':['page tree','image placement','page dimensions','xref/trailer']},
        validation={'libqpdf':'PASS','MuPDF':'PASS','all_pages_rendered':count,'original_jpeg_streams_byte_identical':True}).to_dict()
