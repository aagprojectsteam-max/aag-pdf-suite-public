# AAG - PDF Suite

A Hebrew, right-to-left desktop app for Linux that repairs damaged PDF files, adds a text layer to scanned PDFs and removes repeated pages. Your source files are never changed. Results go to a new folder.

## What it does

The app has three pages:

- **תיקון PDF**: checks PDF files one by one or a whole folder. A healthy file is copied byte for byte. A damaged file is rebuilt from what survives in it, only when the result passes independent checks. Nothing is invented: if a page cannot be proven, the file is refused and the report says why.
- **OCR וכפילויות**: adds a text layer to a scanned PDF and removes pages that repeat, including the same page photographed at a different angle or zoom. Doubtful pairs are shown side by side, and you decide.
- **בדיקת רכיבים**: a read-only check of every library and tool the app needs. It shows the install command for anything missing. It never installs anything.

The app does not read proprietary or encrypted formats and does not bypass any copy protection. A password-protected PDF stays refused.

## Install

You need Linux with Python 3.11 or newer. The app was developed and tested on Ubuntu with GNOME.

```sh
./install.sh
```

The installer creates its own environment under `~/.local/share/aag-pdf-suite`, adds a launcher to `~/.local/bin` and an item to the application menu. Then open **AAG - PDF Suite** from the menu.

The OCR page also needs system tools. On Ubuntu:

```sh
sudo apt install ocrmypdf tesseract-ocr tesseract-ocr-heb tesseract-ocr-eng ghostscript poppler-utils
```

`qpdf`, `unpaper` and `pngquant` are optional. The components page shows the state of each one.

To remove the app, run `./uninstall.sh`. It deletes only the files the installer created. Your PDF files, results and settings stay.

## Command line

The installer puts only the window launcher on your `PATH`. The command-line tools stay in the app's own environment, so point at it first:

```sh
APP=~/.local/share/aag-pdf-suite/venv/bin
```

Repair:

```sh
$APP/aag-pdf-repair damaged.pdf
$APP/aag-pdf-repair damaged.pdf -o repaired.pdf
$APP/aag-pdf-repair --input /path/to/pdfs --output /path/to/repaired --recursive
$APP/aag-pdf-repair --input /path/to/pdfs --output /path/to/repaired --recovery exact --dry-run
```

Without `-o`, one file becomes `<name>.repaired.pdf` beside the source, and a folder goes to `repaired` beside it. Existing results are skipped when their stored hashes still match. `--overwrite` replaces them, `--validate-all-pages` renders every page instead of a sample, and `--report run.jsonl` chooses the report file.

OCR and duplicate removal without the window:

```sh
$APP/python -m aag_pdf_suite.dedup.engine book.pdf -o out
$APP/python -m aag_pdf_suite.dedup.engine book.pdf --no-ocr --keep 12,40-42 --remove 7,9
```

## How a repair is accepted

Before a file is published it must pass a strict parser with automatic repair turned off, a full walk of the page tree, the qpdf check when qpdf is installed, and rendering by MuPDF. A rebuilt file is rendered page by page. Work happens in a hidden temporary file in the destination folder, and the result appears only after every check passes and the source is proven unchanged.

Outcomes are reported per file. See [SUPPORTED_FORMATS.md](SUPPORTED_FORMATS.md) for what each one means and [ARCHITECTURE.md](ARCHITECTURE.md) for the design.

Each run writes a JSONL report and a readable log. `.book2pdf-state.sqlite3` in the destination folder remembers validated results so a stopped run can resume, and small `.book2pdf-*.lock` files stay there on purpose.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

## License

AGPL-3.0-or-later, see [LICENSE](LICENSE). PyMuPDF is used under the AGPL, so anyone who distributes this app or offers it over a network must follow those terms. See [THIRD_PARTY.md](THIRD_PARTY.md).
