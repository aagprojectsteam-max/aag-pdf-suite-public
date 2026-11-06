"""Audited, bounded standard-container adapters. Never extract archive paths."""
import bz2
import io
import lzma
import stat
import zipfile
import zlib

LIMIT = 32 * 1024 * 1024
REGISTRY = (
    ('zip-single-document', b'PK\x03\x04', 'zip'),
    ('gzip-document', b'\x1f\x8b\x08', 'gzip'),
    ('xz-document', b'\xfd7zXZ\x00', 'xz'),
    ('bzip2-document', b'BZh', 'bzip2'),
    ('zlib-document', b'\x78', 'zlib'),
)


def decode(kind, payload):
    """Return bytes only for one complete bounded member, without trailing data."""
    if kind == 'zip':
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) > 64:
                raise ValueError('Archive member limit exceeded')
            if any(m.flag_bits & 1 for m in members):
                raise PermissionError('ZIP encryption flag requires an authorized key/decoder')
            files = [m for m in members if not m.is_dir()]
            if len(files) != 1:
                raise ValueError('Archive is not a single unambiguous document')
            member = files[0]
            name = member.filename.replace('\\', '/')
            if name.startswith('/') or ':' in name or '..' in name.split('/') or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError('Unsafe archive member path or symlink')
            if not name.lower().endswith('.pdf'):
                raise ValueError('Archive member is not a declared PDF document')
            if member.file_size > LIMIT or member.file_size > max(1,member.compress_size)*100:
                raise ValueError('Archive size/ratio bound exceeded')
            # Check EOCD terminates the payload; no polyglot/trailing segments.
            end = payload.rfind(b'PK\x05\x06')
            if end < 0 or end + 22 + int.from_bytes(payload[end+20:end+22], 'little') != len(payload):
                raise ValueError('Archive has unexplained trailing bytes')
            with archive.open(member) as stream:
                out = stream.read(LIMIT+1)
            if len(out) != member.file_size or len(out) > LIMIT:
                raise ValueError('Archive output length mismatch/limit')
            return out
    if kind in ('gzip', 'zlib'):
        decoder = zlib.decompressobj(31 if kind == 'gzip' else 15)
    elif kind == 'bzip2':
        decoder = bz2.BZ2Decompressor()
    elif kind == 'xz':
        decoder = lzma.LZMADecompressor(memlimit=128*1024*1024)
    else:
        raise ValueError('Unregistered codec')
    out = decoder.decompress(payload, LIMIT+1)
    if len(out) > LIMIT or not decoder.eof or decoder.unused_data:
        raise ValueError('Incomplete, oversized, concatenated or trailing-data member')
    return out
