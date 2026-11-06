"""The main window: side navigation around the three pages."""
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from book2pdf.gui.main_window import STYLE, MainWindow

from .. import APP_NAME, VERSION
from ..components import CONVERT, DEDUP, missing
from .components_page import ComponentsPage
from .dedup_page import DedupPage

SHELL_STYLE = '''
QFrame#nav { background: #193c7b; }
QFrame#nav QLabel { color: white; }
QLabel#brand { font-size: 21px; font-weight: 700; }
QLabel#version { color: #b9cbec; font-size: 13px; }
QFrame#nav QPushButton { background: transparent; color: white; border: none; border-radius: 8px;
    padding: 13px 14px; font-size: 15px; text-align: right; }
QFrame#nav QPushButton:hover { background: #28519b; }
QFrame#nav QPushButton:checked { background: white; color: #193c7b; font-weight: 700; }
QLabel#health { font-size: 13px; }
QLabel#summary { font-size: 15px; font-weight: 600; }
QLabel#pageImage { background: white; border: 1px solid #dbe2ed; border-radius: 8px; }
QPushButton#answer { padding: 14px; font-size: 15px; font-weight: 600; }
QPushButton#answer:checked { background: #2555c7; color: white; border-color: #2555c7; }
QPushButton#accent { border-color: #2555c7; color: #1d46a8; font-weight: 600; }
QTableWidget { background: white; border: 1px solid #dbe2ed; border-radius: 6px; gridline-color: #e5eaf4; }
QHeaderView::section { background: #eef2f8; border: none; padding: 7px; font-weight: 600; }
'''


class ConverterPage(MainWindow):
    """The PDF repair window as a page."""
    closed = Signal()
    outputs_changed = Signal(list)

    def __init__(self, inputs=None):
        super().__init__(inputs)
        self.setWindowFlags(Qt.WindowType.Widget)
        self.outputs = []

    @Slot(dict)
    def on_event(self, event):
        super().on_event(event)
        if event['type'] == 'plan':
            self.outputs = []
            self.outputs_changed.emit(self.outputs)
        elif event['type'] == 'result':
            path = event['result'].get('output_path')
            passed = event['result']['status'].startswith('PASS')
            if passed and path and Path(path).suffix.lower() == '.pdf' and Path(path).is_file():
                self.outputs.append(path)
                self.outputs_changed.emit(self.outputs)

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self.closed.emit()

    def busy(self):
        return bool(self.worker and self.worker.isRunning())


class SuiteWindow(QMainWindow):
    def __init__(self, inputs=None, pdfs=None, settings=None):
        super().__init__()
        self.ready_to_close = False
        self.converter_closed = False
        self.setWindowTitle(APP_NAME)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setStyleSheet(STYLE + SHELL_STYLE)
        self.settings = settings or QSettings('AAG', 'PDFSuite')
        self.setMinimumSize(1000, 720)
        self.resize(1180, 860)
        geometry = self.settings.value('geometry')
        if geometry:
            self.restoreGeometry(geometry)

        self.converter = ConverterPage(inputs)
        self.dedup = DedupPage(self.settings)
        self.components = ComponentsPage()
        self.pages = QStackedWidget()
        for page in (self.converter, self.dedup, self.components):
            self.pages.addWidget(page)

        nav = QFrame()
        nav.setObjectName('nav')
        nav.setFixedWidth(230)
        side = QVBoxLayout(nav)
        side.setContentsMargins(14, 20, 14, 16)
        side.setSpacing(6)
        brand = QLabel(APP_NAME)
        brand.setObjectName('brand')
        brand.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        brand.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        version = QLabel(f'גרסה {VERSION}')
        version.setObjectName('version')
        side.addWidget(brand)
        side.addWidget(version)
        side.addSpacing(16)
        self.nav_group = QButtonGroup(self)
        self.nav_buttons = []
        for index, label in enumerate((CONVERT, DEDUP, 'בדיקת רכיבים')):
            button = QPushButton(label)
            button.setCheckable(True)
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch(1)
        self.health = QLabel('בודק רכיבים…')
        self.health.setObjectName('health')
        self.health.setWordWrap(True)
        side.addWidget(self.health)
        self.nav_group.idClicked.connect(self.pages.setCurrentIndex)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(nav)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        self.converter.outputs_changed.connect(self.dedup.set_converted)
        self.converter.closed.connect(self.on_converter_closed)
        self.dedup.stopped.connect(self.finish_close)
        self.components.checked.connect(self.on_checked)
        self.components.run_check()

        self.show_page(1 if pdfs else 0)
        if pdfs:
            self.dedup.add_paths(pdfs)

    def show_page(self, index):
        self.nav_buttons[index].setChecked(True)
        self.pages.setCurrentIndex(index)

    def on_checked(self, rows):
        gaps = missing(rows, DEDUP)
        self.dedup.set_blocked(f"חסר: {', '.join(row.title for row in gaps)}. הפרטים בבדיקת רכיבים." if gaps else '')
        broken = len(gaps) + len(missing(rows, CONVERT))
        self.health.setText(f'🟥 חסרים רכיבים' if broken else '✅ כל הרכיבים תקינים')

    def on_converter_closed(self):
        self.converter_closed = True
        self.finish_close()

    def finish_close(self):
        if self.converter_closed and not self.dedup.busy():
            self.ready_to_close = True
            # Qt ignores close() while a close event of this window is still being handled.
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.ready_to_close:
            self.settings.setValue('geometry', self.saveGeometry())
            if self.components.thread:
                self.components.thread.wait(10000)
            event.accept()
            return
        if self.converter.busy() or self.dedup.busy():
            answer = QMessageBox.question(self, APP_NAME, 'יש עבודה שרצה עכשיו. לעצור אותה ולסגור?\n'
                                                          'עבודת OCR שנעצרה תמשיך מההתחלה של אותו ספר.')
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.ignore()
        self.dedup.stop()
        self.converter.close()
