import argparse
import json
from pathlib import Path
import signal
import sys
import threading

from . import __version__
from .batch import run_batch
from .models import Options


def default_output(inputs):
    """Never the source itself: one file gets a new name beside it, anything else a new folder."""
    first = inputs[0]
    if len(inputs) == 1 and not first.is_dir():
        return first.with_name(first.stem + '.repaired.pdf')
    return first.parent / 'repaired'


def main(argv=None):
    import multiprocessing
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog='aag-pdf-repair', description='Repair damaged PDF files without changing the source.')
    parser.add_argument('files', nargs='*', type=Path)
    parser.add_argument('--input', nargs='+', type=Path, default=[])
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--recursive', action='store_true')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--skip-existing', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--jobs', type=int, default=2, choices=range(1,9), metavar='1..8')
    parser.add_argument('--validate-all-pages', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--flat', action='store_true', help='Do not preserve relative subdirectories')
    parser.add_argument('--recovery', choices=['exact', 'safe-salvage'], default='safe-salvage',
                        help='exact only copies valid PDFs; safe-salvage also rebuilds a damaged structure')
    parser.add_argument('--thermal-pause', action='store_true')
    parser.add_argument('--pause-c', type=float, default=85)
    parser.add_argument('--resume-c', type=float, default=75)
    parser.add_argument('--version', action='version', version=__version__)
    args = parser.parse_args(argv)
    inputs = args.files + args.input
    if not inputs:
        parser.error('Provide one or more .pdf files or folders')
    output = args.output or default_output(inputs)
    cancel = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: cancel.set()) for sig in (signal.SIGINT, signal.SIGTERM)}

    def event(data):
        if data['type'] == 'result':
            r = data['result']
            print(f"{r['status']} {r['source_path']!r}" + (f" — {r['error']}" if r['error'] else ''), flush=True)
        elif data['type'] == 'dry_run' or args.verbose:
            print(json.dumps(data, ensure_ascii=False), flush=True)

    try:
        options = Options(overwrite=args.overwrite, skip_existing=args.skip_existing, jobs=args.jobs,
                          recursive=args.recursive, validate_all_pages=args.validate_all_pages,
                          preserve_tree=not args.flat, thermal_pause=args.thermal_pause,
                          pause_c=args.pause_c, resume_c=args.resume_c, recovery=args.recovery)
        summary = run_batch(inputs, output, options, report_path=args.report, cancel=cancel,
                            on_event=event, explicit_file=output.suffix.lower() == '.pdf', dry_run=args.dry_run)
        print(' '.join(f'{k}={v}' for k, v in summary.items()))
        return 130 if cancel.is_set() else 1 if summary['FAILED'] or summary['UNSUPPORTED'] else 0
    except Exception as exc:
        print(f'aag-pdf-repair: {exc}', file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
