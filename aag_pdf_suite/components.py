"""Checks that everything the two tools need is present. Read-only: it never installs anything."""
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

CONVERT = 'תיקון PDF'
DEDUP = 'OCR וכפילויות'
LOW_SPACE_GIB = 5


@dataclass
class Component:
    key: str
    title: str
    purpose: str
    features: tuple
    required: bool
    apt: str = ''
    ok: bool = False
    version: str = ''
    note: str = ''


# key, title, purpose, features, required, code that prints the version
LIBRARIES = (
    ('pyside6', 'PySide6', 'חלון התוכנה', (CONVERT, DEDUP), True,
     'import PySide6; from PySide6 import QtWidgets; print(PySide6.__version__)'),
    ('pikepdf', 'pikepdf', 'קריאה וכתיבה של PDF', (CONVERT, DEDUP), True,
     'import pikepdf; pikepdf.Pdf.new().close(); print(pikepdf.__version__)'),
    ('pymupdf', 'PyMuPDF', 'קריאת עמודים ובדיקת תצוגה', (CONVERT,), True,
     'import pymupdf; pymupdf.open().close(); print(pymupdf.__version__)'),
    ('pillow', 'Pillow', 'עיבוד תמונות עמוד', (CONVERT,), True, 'import PIL; print(PIL.__version__)'),
    ('numpy', 'NumPy', 'חישובי השוואה בין עמודים', (DEDUP,), True, 'import numpy; print(numpy.__version__)'),
    ('opencv', 'OpenCV', 'זיהוי עמודים זהים בזווית או בזום אחרים', (DEDUP,), True,
     'import cv2; cv2.SIFT_create(); print(cv2.__version__)'),
)

# key, title, purpose, features, required, apt package, version arguments
TOOLS = (
    ('ocrmypdf', 'OCRmyPDF', 'הוספת שכבת טקסט לעמודים סרוקים', (DEDUP,), True, 'ocrmypdf', ('--version',)),
    ('tesseract', 'Tesseract', 'זיהוי האותיות', (DEDUP,), True, 'tesseract-ocr', ('--version',)),
    ('gs', 'Ghostscript', 'הכנת עמודים ל-OCR', (DEDUP,), True, 'ghostscript', ('--version',)),
    ('pdftoppm', 'pdftoppm', 'הפיכת עמודים לתמונות', (DEDUP,), True, 'poppler-utils', ('-v',)),
    ('pdftotext', 'pdftotext', 'קריאת הטקסט שזוהה', (DEDUP,), True, 'poppler-utils', ('-v',)),
    ('pdfimages', 'pdfimages', 'מדידת איכות הסריקה', (DEDUP,), True, 'poppler-utils', ('-v',)),
    ('qpdf', 'qpdf', 'בדיקת תקינות של PDF', (CONVERT,), False, 'qpdf', ('--version',)),
    ('pdfinfo', 'pdfinfo', 'בדיקת תקינות של PDF', (CONVERT,), False, 'poppler-utils', ('-v',)),
    ('unpaper', 'unpaper', 'ניקוי עמודים לפני OCR', (DEDUP,), False, 'unpaper', ('--version',)),
    ('pngquant', 'pngquant', 'הקטנת קבצים', (DEDUP,), False, 'pngquant', ('--version',)),
)

LANGUAGES = (('heb', 'עברית ל-OCR', 'tesseract-ocr-heb'), ('eng', 'אנגלית ל-OCR', 'tesseract-ocr-eng'))


def short_version(text):
    match = re.search(r'\d+(?:\.\d+)+', text)
    return match.group(0) if match else ''


def check_library(item):
    key, title, purpose, features, required, code = item
    found = Component(key, title, purpose, features, required)
    try:
        run = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        found.note = str(exc)
        return found
    found.ok = run.returncode == 0
    if found.ok:
        found.version = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else ''
    else:
        found.note = (run.stderr.strip().splitlines() or [''])[-1]
    return found


def check_tool(item):
    key, title, purpose, features, required, apt, version_args = item
    found = Component(key, title, purpose, features, required, apt)
    path = shutil.which(key)
    if not path:
        return found
    if version_args is None:
        found.ok = True
        return found
    try:
        run = subprocess.run([path, *version_args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        found.note = str(exc)
        return found
    found.ok = run.returncode == 0
    found.version = short_version(run.stdout + run.stderr)
    if not found.ok:
        found.note = (run.stderr.strip().splitlines() or [''])[-1]
    return found


def check_languages():
    rows = [Component(code, title, 'קריאת הטקסט בעמודים', (DEDUP,), True, apt) for code, title, apt in LANGUAGES]
    tesseract = shutil.which('tesseract')
    if not tesseract:
        return rows
    try:
        run = subprocess.run([tesseract, '--list-langs'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return rows
    known = set((run.stdout + run.stderr).split())
    for row in rows:
        row.ok = row.key in known
    return rows


def check_scratch():
    """OCR needs /var/tmp: Ubuntu lets Ghostscript write scratch files only in temp folders, and /tmp is in RAM here."""
    found = Component('scratch', 'תיקיית עבודה זמנית', 'קבצים זמניים של OCR', (DEDUP,), True)
    try:
        with tempfile.NamedTemporaryFile(dir='/var/tmp'):
            pass
        free = shutil.disk_usage('/var/tmp').free / 2**30
    except OSError as exc:
        found.note = str(exc)
        return found
    found.ok = True
    found.version = f'{free:.0f} GiB פנויים'
    if free < LOW_SPACE_GIB:
        found.note = 'נשאר מעט מקום. ספר גדול עלול להיעצר באמצע.'
    return found


def check_all():
    with ThreadPoolExecutor(8) as pool:
        libraries = pool.map(check_library, LIBRARIES)
        tools = pool.map(check_tool, TOOLS)
        languages = pool.submit(check_languages)
        scratch = pool.submit(check_scratch)
        return [*libraries, *tools, *languages.result(), scratch.result()]


def missing(rows, feature):
    return [row for row in rows if feature in row.features and row.required and not row.ok]


def fix_command(rows):
    """The one command that installs what is missing. It is shown to the owner and never run from here."""
    packages = sorted({row.apt for row in rows if row.apt and not row.ok})
    return f"sudo apt install {' '.join(packages)}" if packages else ''


def broken_libraries(rows):
    return [row for row in rows if not row.ok and not row.apt and row.key != 'scratch']
