import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import pytest


@pytest.fixture
def pdf_bytes():
    """A real image-containing PDF with a classic xref, no renderer shortcut."""
    import pikepdf
    import io
    pdf = pikepdf.Pdf.new()
    for i in range(3):
        page = pdf.add_blank_page(page_size=(144, 144))
        image = pikepdf.Stream(pdf, bytes([25 + i * 50, 100, 220] * 16))
        image.Type = pikepdf.Name.XObject
        image.Subtype = pikepdf.Name.Image
        image.Width = 4
        image.Height = 4
        image.ColorSpace = pikepdf.Name.DeviceRGB
        image.BitsPerComponent = 8
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im=image))
        page.Contents = pikepdf.Stream(pdf, b'q 100 0 0 100 22 22 cm /Im Do Q\n')
    stream = io.BytesIO()
    pdf.save(stream, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    return stream.getvalue()


@pytest.fixture
def book(tmp_path, pdf_bytes):
    path = tmp_path / 'source.pdf'
    path.write_bytes(pdf_bytes)
    return path
