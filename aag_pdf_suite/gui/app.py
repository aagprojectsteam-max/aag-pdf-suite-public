"""Starts AAG - PDF Suite."""
import multiprocessing
import sys
from pathlib import Path

from .. import APP_NAME


def split_paths(paths):
    """PDF files go to the duplicates page. Folders go to the repair page."""
    pdfs = [path for path in paths if Path(path).suffix.lower() == '.pdf']
    return [path for path in paths if path not in pdfs], pdfs


def main(argv=None):
    multiprocessing.freeze_support()
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont, QIcon
        from PySide6.QtWidgets import QApplication, QMessageBox
    except (ModuleNotFoundError, ImportError) as exc:
        from book2pdf.platforms import setup_error
        setup_error(f'לא ניתן לטעון את ספריית הממשק PySide6.\n{exc}')
        return 2
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName('AAG')
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    app.setFont(QFont('Segoe UI' if sys.platform == 'win32' else 'Noto Sans Hebrew', 10))
    app.setStyle('Fusion')
    icon = Path(__file__).with_name('aag-pdf-suite.svg')
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    paths = argv if argv is not None else sys.argv[1:]
    folders, pdfs = split_paths(paths)
    try:
        from .shell import SuiteWindow
        window = SuiteWindow(folders, pdfs)
    except ImportError as exc:
        QMessageBox.critical(None, APP_NAME, f'חסרה ספרייה נדרשת.\n{exc}')
        return 2
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
