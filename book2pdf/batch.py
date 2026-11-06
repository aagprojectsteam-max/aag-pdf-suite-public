"""Disk-backed enumeration plus a bounded process pool, shared by CLI and GUI."""
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from concurrent.futures.process import BrokenProcessPool
from contextlib import ExitStack
from datetime import datetime, timezone
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import uuid

from .models import Options, Result
from .report import Report
from .state import State
from .thermal import ThermalGate
from .worker import convert, initialize_process
from .platforms import path_key


def discover(inputs, recursive, exclude=None):
    for raw in inputs:
        path = Path(raw).expanduser().absolute()
        if path.is_dir():
            for root, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not Path(root, d).is_symlink() and Path(root, d) != exclude)
                for name in sorted(files):
                    item = Path(root, name)
                    if item.suffix.lower() == '.pdf':
                        yield item, item.relative_to(path), path
                if not recursive:
                    break
        else:
            yield path, Path(path.name), path.parent


def run_batch(inputs, output: Path, options=Options(), *, report_path=None, cancel=None, on_event=None,
              explicit_file=False, dry_run=False):
    cancel = cancel or threading.Event()
    emit = on_event or (lambda event: None)
    output = Path(output).expanduser().absolute()
    base = output.parent if explicit_file else output
    if explicit_file and (len(inputs) != 1 or Path(inputs[0]).is_dir()):
        raise ValueError('A PDF destination requires exactly one source file')
    # Dry-run uses the OS temporary directory and does not create output/state/reports.
    if not dry_run:
        base.mkdir(parents=True, exist_ok=True)
    summary = dict(TOTAL=0, PASS=0, PASS_EXACT=0, PASS_REPAIRED=0, SKIPPED=0, FAILED=0, UNSUPPORTED=0, CANCELLED=0)
    with tempfile.TemporaryDirectory(prefix='book2pdf-plan-') as spool:
        db = sqlite3.connect(Path(spool) / 'plan.sqlite3')
        db.execute('CREATE TABLE jobs (source TEXT PRIMARY KEY, output TEXT, error TEXT DEFAULT "", destination_key TEXT)')
        db.execute('CREATE INDEX destinations ON jobs(destination_key)')
        try:
            for source, relative, root in discover(inputs, options.recursive, base):
                if cancel.is_set():
                    break
                destination = output if explicit_file else base / (relative if options.preserve_tree else Path(source.name)).with_suffix('.pdf')
                canonical = source.resolve()
                target = destination.absolute()
                collision = db.execute('SELECT source FROM jobs WHERE destination_key=? AND source!=?', (path_key(target), str(canonical))).fetchone()
                error = 'Multiple sources map to the same output; choose separate destinations' if collision else ''
                if error:
                    db.execute('UPDATE jobs SET error=? WHERE destination_key=?', (error, path_key(target)))
                db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?)', (str(canonical), str(target), error, path_key(target)))
                summary['TOTAL'] = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] if summary['TOTAL'] == 0 else summary['TOTAL'] + 1
                if summary['TOTAL'] % 100 == 0:
                    emit({'type': 'scanning', 'count': summary['TOTAL']})
            db.commit()
            summary['TOTAL'] = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            summary['SCAN_CANCELLED'] = cancel.is_set()
            emit({'type': 'plan', 'total': summary['TOTAL']})
            if dry_run:
                for source, target, error in db.execute('SELECT source,output,error FROM jobs'):
                    emit({'type': 'dry_run', 'source': source, 'output': target, 'error': error})
                return {**summary, 'DRY_RUN': True}
            state_path = base / '.book2pdf-state.sqlite3'
            state = State(state_path)
            state.close()
            report_path = Path(report_path) if report_path else base / f"book2pdf-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.jsonl"
            if report_path.suffix.lower() == '.pdf' or report_path.resolve() == state_path.resolve():
                raise ValueError('Report path must be a separate JSONL file')
            report = Report(report_path)
            summary['REPORT'] = str(report_path)
            completed = 0

            def accept(result):
                nonlocal completed
                report.write(result)
                key = {'PASS_EXACT': 'PASS', 'PASS_REPAIRED': 'PASS', 'PASS_DECODED': 'PASS', 'SKIPPED_EXISTING_VALID': 'SKIPPED',
                       'UNSUPPORTED_OR_AMBIGUOUS': 'UNSUPPORTED', 'NEW_VARIANT_DETECTED': 'UNSUPPORTED',
                       'UNSUPPORTED_AFTER_ANALYSIS': 'UNSUPPORTED', 'CANCELLED': 'CANCELLED'}.get(result.status, 'FAILED')
                summary[key] += 1
                if result.status in ('PASS_EXACT', 'PASS_REPAIRED'):
                    summary[result.status] += 1
                completed += 1
                emit({'type': 'result', 'result': result.to_dict(), 'completed': completed, 'summary': dict(summary)})

            gate = ThermalGate(options)
            try:
                with ExitStack() as pools:
                    def new_pool():
                        return pools.enter_context(ProcessPoolExecutor(max_workers=options.jobs, mp_context=multiprocessing.get_context('spawn'),initializer=initialize_process))
                    pool = new_pool()
                    broken = False
                    pending = {}
                    rows = iter(db.execute('SELECT source,output,error FROM jobs ORDER BY source'))
                    exhausted = False
                    thermal_notified = False
                    while pending or not exhausted:
                        if broken and not pending and not exhausted:
                            pool.shutdown(wait=True)
                            pool = new_pool()
                            broken = False
                        if cancel.is_set() and not exhausted:
                            for source, target, error in rows:
                                accept(Result(source, target, status='CANCELLED', timestamp=datetime.now(timezone.utc).isoformat()))
                            exhausted = True
                        while not exhausted and not broken and len(pending) < options.jobs and not cancel.is_set():
                            if not gate.allows_start():
                                if not thermal_notified:
                                    emit({'type': 'thermal', 'paused': True})
                                    thermal_notified = True
                                break
                            if thermal_notified:
                                emit({'type': 'thermal', 'paused': False})
                                thermal_notified = False
                            row = next(rows, None)
                            if row is None:
                                exhausted = True
                                break
                            source, target, error = row
                            if error:
                                accept(Result(source, target, status='FAIL_REPAIR', error=error))
                                continue
                            emit({'type': 'started', 'source': source})
                            try:
                                future = pool.submit(convert, Path(source), Path(target), options, state_path)
                                pending[future] = row
                            except BrokenProcessPool as exc:
                                broken = True
                                accept(Result(source,target,status='FAIL_REPAIR',error=f'Worker pool failed: {exc}'))
                        if pending:
                            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                            for future in done:
                                source, target, _ = pending.pop(future)
                                try:
                                    result = future.result()
                                except Exception as exc:
                                    result = Result(source, target, status='FAIL_REPAIR', error=f'Worker failed: {exc}')
                                    if isinstance(exc,BrokenProcessPool):
                                        broken = True
                                accept(result)
                        elif not exhausted:
                            cancel.wait(1.0)
                report.finish(summary)
            finally:
                report.close()
            emit({'type': 'finished', 'summary': summary})
            return summary
        finally:
            db.close()
