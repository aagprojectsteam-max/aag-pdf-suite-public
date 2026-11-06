# Architecture

Two packages make up the app:

- `book2pdf/`: the PDF repair engine and its page. The name is historic and kept so the engine's lock, state and report names stay stable.
- `aag_pdf_suite/`: the app shell, the OCR and duplicates page, the components check and the duplicate engine.

The window and the `aag-pdf-repair` command both call `batch.run_batch`, which builds a disk-backed SQLite work plan, so source bytes are never preloaded for a whole folder. The coordinator runs a bounded number of worker processes, one source per process.

| Module | Responsibility |
| --- | --- |
| `book2pdf/batch.py` | Streaming discovery of `.pdf` files, collision planning, process scheduling, safe cancellation, progress and final counts |
| `book2pdf/worker.py` | Per-output lock, source identity and hash checks, strict copy or rebuild, staging cleanup, atomic publication, durable success record |
| `book2pdf/repair.py` | Chunked byte copy with ordered patches to proven structural fields |
| `book2pdf/recovery/evidence.py` | Typed, stream-aware observations of objects, cross-reference data and coordinates |
| `book2pdf/recovery/engine.py` | Bounded recovery hypotheses in a disposable child process, rebuilding only justified reference and page metadata |
| `book2pdf/recovery/images.py` | Direct copy of explicitly framed and ordered JPEG scans |
| `book2pdf/recovery/document.py` | The serializable recovered-document model used by reports and the result cache |
| `book2pdf/strategies/containers.py` | Bounded unwrapping of standard whole-file compression and single-member ZIP |
| `book2pdf/validator.py`, `render_diagnostics.py` | Strict libqpdf parsing, page-tree checks, the qpdf command when available, MuPDF parsing and rendering |
| `book2pdf/provenance.py` | Provenance record embedded in rebuilt PDFs |
| `book2pdf/platforms.py` | File locks, atomic publication and external validator discovery |
| `book2pdf/state.py` | SQLite records of validated results and streaming SHA-256 |
| `book2pdf/report.py` | Exclusive JSONL and readable log creation, per-result flush, final summary |
| `book2pdf/thermal.py` | Read-only temperature sampling, pausing new work when the machine is hot |
| `book2pdf/gui/` | The repair page: Hebrew RTL widgets, drag and drop, settings, worker lifecycle |
| `aag_pdf_suite/gui/` | Shell window, OCR and duplicates page, components page |
| `aag_pdf_suite/dedup/engine.py` | OCR through OCRmyPDF, page comparison with OpenCV, resumable runs |
| `aag_pdf_suite/components.py` | Read-only detection of libraries, tools and OCR languages |

## Publication order

Source fingerprint, output lock, provenance and overwrite decision, strict check or recovery, staging, validation, source recheck, atomic publication, durable record.

There is no cross-filesystem transaction between PDF publication and SQLite. A crash in that gap leaves the output untrusted for resume, so it is reported instead of being silently accepted.

## Exact copy and rebuild

A PDF that passes the strict checks is copied byte for byte. The engine never asks a library to silently reconstruct missing content: automatic recovery is disabled in the strict parser.

A rebuild keeps the surviving object and stream bytes and writes new structure around them. It is accepted only when one consistent reading of the file exists and the result passes every validator on every page. Cycles, orphan pages, missing content streams, ambiguous roots and surviving security dictionaries are refused.

## Dependency choices

pikepdf and libqpdf give strict parsing and object inspection. PyMuPDF gives an independent parser and renderer. The qpdf command adds a separately invoked check when installed. OpenCV compares page images in the duplicate engine.

## Operating limits

Publication has been tested on local Linux filesystems only. The code still contains a few Windows branches inherited from earlier work. They are not tested and Windows is not supported.
