"""One source per process, validated staging, exclusive output lock, atomic publish."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile
import time

from .models import Options, Result, Unsupported
from .repair import Detection, reconstruct
from .state import State, sha256, pdf_provenance
from .validator import ValidationError, validate
from .platforms import FileLock, output_key, publish

REPAIRED_CLASSES = ('STRUCTURAL_PDF_RECOVERY', 'PAGE_LEVEL_RECOVERY', 'IMAGE_LEVEL_RECOVERY')
EXTRACTED_CLASSES = ('DECODED_PDF_CONTENT', 'DECODED_IMAGE_CONTENT')


def initialize_process():
    import signal
    # Terminal Ctrl+C reaches the whole process group. The coordinator handles it
    # and lets active safe units finish; children must not raise KeyboardInterrupt.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _cache_recovery_compatible(requested, previous):
    from . import __version__
    if previous.get('engine_version') != __version__:
        return False
    klass = previous.get('recovery_class', 'STRICT_EXACT_RECOVERY')
    from .recovery.document import GENERIC_CLASSES
    if klass in GENERIC_CLASSES:
        model = previous.get('recovered_document', {})
        from .recovery.engine import STRATEGY_VERSION
        return (model.get('engine_version') == __version__
                and model.get('provenance', {}).get('strategy') == STRATEGY_VERSION
                and model.get('assumptions', []) == previous.get('assumptions', [])
                and (requested != 'exact' or klass == 'DECODED_PDF_CONTENT'))
    return klass == 'STRICT_EXACT_RECOVERY'


def _strict_detection(source):
    """A PDF that passes the strict checks is copied byte for byte."""
    import pikepdf
    from .validator import check_tree
    try:
        with pikepdf.open(source, attempt_recovery=False) as pdf:
            check_tree(pdf)
            if pdf.check_pdf_syntax():
                raise ValueError('PDF syntax repair required')
        with source.open('rb') as stream:
            header = stream.read(8)
        if not header.startswith(b'%PDF-'):
            raise ValueError('Missing PDF header')
    except (pikepdf.PdfError, ValueError) as exc:
        raise Unsupported(str(exc)) from exc
    return Detection('validated-pdf-copy', header[5:].decode('ascii'), 0, 0, 0, source.stat().st_size)


def _apply_recovery(result, generic):
    model = generic['model']
    source = Path(generic['_path'])
    with source.open('rb') as stream:
        version = stream.read(8)[5:].decode('ascii')
    detection = Detection('universal-' + generic['selected_hypothesis']['kind'], version,
                          model['provenance'].get('origin', 0), model['provenance'].get('original_startxref'), 0,
                          source.stat().st_size)
    result.recovered_document = model
    result.recovery_class = {'DECODED': 'DECODED_PDF_CONTENT', 'EXACT': 'STRICT_EXACT_RECOVERY',
                             'STRUCTURAL_REPAIR': 'STRUCTURAL_PDF_RECOVERY', 'PAGE_LEVEL_RECOVERY': 'PAGE_LEVEL_RECOVERY',
                             'IMAGE_LEVEL_RECOVERY': 'IMAGE_LEVEL_RECOVERY'}[model['recovery_level']]
    result.fidelity = model['confidence']
    result.assumptions = model['assumptions']
    result.reconstructed_objects = [{
        'object': 'document structure', 'replacement': description,
        'uncertainty': '; '.join(model.get('assumptions', [])) or 'Constrained by surviving source objects and validated page graph',
    } for description in model['provenance'].get('synthesized_structures', [])]
    result.missing_data_synthesized = bool(result.reconstructed_objects)
    result.preservation = {**model['resources'], 'strategy': generic['strategy'],
                           'transformations': model['provenance'].get('transformations', []),
                           'streams': model['provenance'].get('preserved_streams', [])}
    result.validation_mode = 'all_pages'
    result.forensic = {k: v for k, v in generic.items() if not k.startswith('_') and k != 'model'}
    if result.assumptions:
        result.warning = ' '.join(result.assumptions)
    return source, detection


def convert(source: Path, output: Path, options: Options, state_path: Path, expected_source_hash=None, *, progress=None) -> Result:
    start = time.monotonic()
    result = Result(str(source), str(output), timestamp=datetime.now(timezone.utc).isoformat(),
                    validation_mode="all_pages" if options.validate_all_pages else "representative")
    tmp = None
    state = None
    generic = None
    working_source = source
    try:
        import pikepdf
        import pymupdf  # noqa: F401 - a missing renderer must fail here, before any output work.
        if source.suffix.lower() != '.pdf' or not source.is_file():
            result.status = "FAIL_INVALID_SOURCE"
            raise ValueError("Source must be a regular .pdf file")
        if output.suffix.lower() != ".pdf":
            raise ValueError("Output must use the .pdf extension")
        if source.resolve() == output.resolve() or (output.exists() and os.path.samefile(source, output)):
            raise ValueError("Source and output must be different files")
        source_stat = source.stat()
        result.source_size = source_stat.st_size
        result.source_mtime = source_stat.st_mtime_ns
        result.source_hash = sha256(source)
        if expected_source_hash and result.source_hash != expected_source_hash:
            raise ValueError('Source changed since opening; reopen the file before saving')
        prior = pdf_provenance(source, result.source_hash)
        from .provenance import embedded_provenance
        try:
            embedded = embedded_provenance(source)
        except pikepdf.PdfError:
            embedded = None  # Damaged PDF proceeds to bounded reconstruction.
        if embedded:
            prior = {**(prior or {}), **embedded}
        if prior and prior.get('recovery_class') in (*REPAIRED_CLASSES, *EXTRACTED_CLASSES):
            for name in ('recovery_class', 'reconstructed_objects', 'preservation', 'warning', 'fidelity',
                         'missing_data_synthesized', 'assumptions', 'affected_pages', 'recovered_document'):
                if name in prior:
                    setattr(result, name, prior[name])
            result.validation_mode = 'all_pages'
        output.parent.mkdir(parents=True, exist_ok=True)
        # Directory-relative lock remains present so processes cannot lock different
        # inodes after an unlink race. File contains no source content.
        key = output_key(output)
        lock_path = output.parent / f".book2pdf-{key}.lock"
        with FileLock(lock_path):
            if output.is_symlink():
                raise ValueError("Refusing a symbolic-link output")
            # Recover only this output's private staging files while holding its lock.
            prefix = f".book2pdf-{key}-"
            for stale in output.parent.glob(prefix + "*.tmp.pdf"):
                if stale.is_file() and not stale.is_symlink():
                    stale.unlink()
            state = State(state_path)
            previous = state.lookup(source, output)
            if options.skip_existing and not options.overwrite and output.is_file() and previous:
                matches = (previous["source_hash"] == result.source_hash
                           and previous["source_size"] == result.source_size
                           and previous["source_mtime"] == result.source_mtime
                           and previous["output_size"] == output.stat().st_size
                           and previous["output_hash"] == sha256(output)
                           and _cache_recovery_compatible(options.recovery, previous)
                           and (not options.validate_all_pages or previous["validation_mode"] == "all_pages"))
                if matches:
                    for name in ("page_count", "output_size", "output_hash", "detected_pdf_version",
                                 "detected_wrapper_offset", "detected_startxref", "conversion_strategy",
                                 "recovery_class", "reconstructed_objects", "preservation", "validation_mode", "warning",
                                 "fidelity", "missing_data_synthesized", "assumptions", "affected_pages",
                                 "dimensions", "recovered_document", "engine_version"):
                        if name in previous:
                            setattr(result, name, previous[name])
                    result.validation_status = "PASS_CACHED_SHA256"
                    result.status = "SKIPPED_EXISTING_VALID"
                    return result
            if output.exists() and not options.overwrite:
                raise FileExistsError("Output exists without matching validated provenance; choose overwrite or another destination")
            try:
                detection = _strict_detection(source)
            except Unsupported as initial:
                from .recovery.engine import attempt
                generic = attempt(source, output.parent)
                if not generic.get('success'):
                    result.status = 'UNSUPPORTED_AFTER_ANALYSIS'
                    result.error = f"{initial}; {generic.get('next_step') or generic.get('error') or 'no safe repair found'}"
                    result.forensic = {k: v for k, v in generic.items() if not k.startswith('_')}
                    return result
                if options.recovery == 'exact' and generic['model']['recovery_level'] not in ('EXACT', 'DECODED'):
                    result.status = 'NEW_VARIANT_DETECTED'
                    result.error = 'Validated structural recovery is available; exact mode forbids synthesis'
                    return result
                working_source, detection = _apply_recovery(result, generic)
            result.detected_pdf_version = detection.version
            result.detected_wrapper_offset = detection.wrapper_offset
            result.detected_startxref = detection.startxref
            result.conversion_strategy = detection.strategy
            fd, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp.pdf", dir=output.parent)
            os.close(fd)
            tmp = Path(name)
            if detection.strategy == 'validated-pdf-copy':
                shutil.copyfile(working_source, tmp)
            else:
                reconstruct(working_source, tmp, detection)
            result.conversion_seconds = time.monotonic() - start
            validation_start = time.monotonic()
            validation_options = {'progress': progress} if progress else {}
            result.page_count, validation_warning = validate(tmp, result.validation_mode == 'all_pages', **validation_options)
            result.warning = (result.warning + ' ' + validation_warning).strip()
            result.validation_seconds = time.monotonic() - validation_start
            from .recovery.document import GENERIC_CLASSES, from_result
            if result.recovery_class in GENERIC_CLASSES:
                from .provenance import stamp_recovered
                stamp_recovered(tmp, result)
                result.page_count, _ = validate(tmp, True, **validation_options)
            current = source.stat()
            if ((current.st_size, current.st_mtime_ns) != (source_stat.st_size, source_stat.st_mtime_ns)
                    or sha256(source) != result.source_hash):
                raise ValueError("Source changed during repair; refusing output")
            result.output_size = tmp.stat().st_size
            result.output_hash = sha256(tmp)
            publish(tmp, output, options.overwrite)
            tmp = None
            result.validation_status = "PASS"
            result.status = ('PASS_REPAIRED' if result.recovery_class in REPAIRED_CLASSES
                             else 'PASS_DECODED' if working_source != source or result.recovery_class in EXTRACTED_CLASSES
                             else 'PASS_EXACT')
            result.elapsed_seconds = time.monotonic() - start
            result.dimensions = {'FORMAT_IDENTIFIED': result.conversion_strategy if working_source != source else 'PDF',
                                 'DOCUMENT_PARSEABLE': True, 'PAGES_ENUMERABLE': result.page_count,
                                 'PAGES_RENDERED': result.page_count if result.validation_mode == 'all_pages' else 'representative',
                                 'SURVIVING_BYTES_PRESERVED': True, 'MISSING_DATA_SYNTHESIZED': result.missing_data_synthesized,
                                 'ASSUMPTIONS': result.assumptions, 'AFFECTED_PAGES': result.affected_pages,
                                 'FIDELITY_TO_AUTHORIZED_ORIGINAL': 'NOT_PROVEN', 'SOURCE_UNCHANGED': True}
            result.recovered_document = from_result(result)
            state.save(result)
    except Unsupported as exc:
        result.error = str(exc)
        result.status = 'FAIL_VALIDATION'
    except ModuleNotFoundError as exc:
        result.status = 'DEPENDENCY_MISSING'
        result.error = str(exc)
    except ImportError as exc:
        result.status = 'RUNTIME_LOAD_FAILED'
        result.error = str(exc)
    except ValidationError as exc:
        result.status = "FAIL_VALIDATION"
        result.validation_status = "FAIL"
        result.error = str(exc)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        if result.status in ("PASS_EXACT", "PASS_REPAIRED", "PASS_DECODED"):
            result.status = "FAIL_REPAIR"
            result.warning = "Validated PDF published, but durable state commit failed; file is protected on restart"
    finally:
        if generic and generic.get('_scratch'):
            shutil.rmtree(generic['_scratch'], ignore_errors=True)
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        if state is not None:
            state.close()
        result.elapsed_seconds = round(time.monotonic() - start, 4)
    return result
