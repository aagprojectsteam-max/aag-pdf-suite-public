#!/usr/bin/env python3
"""Installs AAG - PDF Suite for the current user, in its own environment."""
import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

MARKER = 'AAG-PDF-SUITE-MANAGED-V1'
DESKTOP_ID = 'aag-pdf-suite.desktop'


def refresh(prefix):
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(prefix / 'share/applications')], check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    prefix = args.prefix.expanduser().resolve()
    project = Path(__file__).resolve().parents[1]
    appdir = prefix / 'share/aag-pdf-suite'
    launcher = prefix / 'bin/aag-pdf-suite'
    desktop = prefix / 'share/applications' / DESKTOP_ID
    icon = prefix / 'share/icons/hicolor/scalable/apps/aag-pdf-suite.svg'
    managed = (launcher, desktop, icon)

    for path in managed:
        if path.exists() and MARKER not in path.read_text():
            raise ValueError(f'Refusing to touch an unrelated file: {path}')

    if args.uninstall:
        if not (appdir / '.managed').is_file():
            raise ValueError(f'Not a managed install: {appdir}')
        for path in managed:
            path.unlink(missing_ok=True)
        shutil.rmtree(appdir)
        refresh(prefix)
        print('Uninstalled AAG - PDF Suite. PDFs, results and settings were kept.')
        return

    appdir.mkdir(parents=True, exist_ok=True)
    (appdir / '.managed').write_text(MARKER)
    venv = appdir / 'venv'
    python = venv / 'bin/python'
    subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--timeout', '60', str(project)],
                   check=True)

    # Prove the environment loads before the menu item appears.
    subprocess.run([str(python), '-c', 'import cv2, numpy, pikepdf, pymupdf, PIL\n'
                    'from PySide6.QtWidgets import QApplication\nimport aag_pdf_suite.dedup.engine'], check=True)

    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(f'#!/bin/sh\n# {MARKER}\nexec {shlex.quote(str(python))} -m aag_pdf_suite.gui.app "$@"\n')
    launcher.chmod(0o755)
    icon.parent.mkdir(parents=True, exist_ok=True)
    source_icon = (project / 'aag_pdf_suite/gui/aag-pdf-suite.svg').read_text()
    icon.write_text(source_icon.replace('<svg ', f'<!-- {MARKER} -->\n<svg ', 1))
    desktop.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(launcher).replace('\\', '\\\\').replace('"', '\\"')
    desktop.write_text(f'''[Desktop Entry]
# {MARKER}
Type=Application
Name=AAG - PDF Suite
Comment=תיקון PDF, OCR וסינון עמודים כפולים
Exec="{escaped}" %F
Icon=aag-pdf-suite
Terminal=false
Categories=Office;
StartupWMClass=AAG - PDF Suite
MimeType=application/pdf;
''')
    refresh(prefix)
    print(f'Installed AAG - PDF Suite: {launcher}')


if __name__ == '__main__':
    main()
