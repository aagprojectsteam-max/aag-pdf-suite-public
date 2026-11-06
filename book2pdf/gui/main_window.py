from pathlib import Path
from PySide6.QtCore import QSettings, Qt, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QLineEdit, QCheckBox, QComboBox, QProgressBar, QPlainTextEdit,
    QFileDialog, QMessageBox, QDialog, QGridLayout, QScrollArea,
)

from ..models import Options
from ..report import STATUS_LABELS
from .widgets import InputList
from .workers import BatchThread

STYLE = '''
QMainWindow, QDialog { background: #f3f5f9; }
QWidget { color: #18283d; font-size: 14px; }
QGroupBox { background: white; border: 1px solid #dbe2ed; border-radius: 12px;
    margin-top: 0px; padding: 12px 16px; font-weight: 600; }
QPushButton { background: white; border: 1px solid #bcc9da; border-radius: 7px;
    padding: 9px 15px; min-height: 20px; }
QPushButton:hover { background: #eaf0fc; border-color: #628bdd; }
QPushButton:disabled { color: #8b96a5; background: #e9edf2; border-color: #dce2eb; }
QPushButton#primary { background: #2555c7; color: white; border: none; font-weight: 700;
    padding: 13px 26px; font-size: 16px; }
QPushButton#primary:hover { background: #1d46a8; }
QPushButton#primary:disabled { background: #96acd9; }
QLineEdit, QListWidget, QPlainTextEdit, QComboBox { background: #fcfdff;
    border: 1px solid #ccd6e4; border-radius: 6px; padding: 7px; }
QProgressBar { border: none; background: #e5eaf4; border-radius: 6px; text-align: center; height: 22px; }
QProgressBar::chunk { background: #2d67d8; border-radius: 6px; }
QLabel#muted { color: #586a82; }
QLabel#title { font-size: 29px; font-weight: 700; color: #193c7b; }
QCheckBox { spacing: 8px; padding: 3px; }
'''


class MainWindow(QMainWindow):
    def __init__(self, inputs=None, settings=None):
        super().__init__()
        self.settings = settings or QSettings('AAG', 'PDFRepair')
        self.worker = None
        self.pending_close = False
        self.last_summary = None
        self.last_error = ''
        self.setWindowTitle('תיקון PDF')
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setStyleSheet(STYLE)
        self.resize(920, 850)
        self.setMinimumSize(700, 620)
        central = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)
        title = QLabel('תיקון PDF')
        title.setObjectName('title')
        layout.addWidget(title)
        subtitle = QLabel('מתקן קובצי PDF פגומים לתיקייה חדשה. קובץ תקין מועתק כמו שהוא.')
        subtitle.setObjectName('muted')
        layout.addWidget(subtitle)

        self.input_group = QGroupBox()
        il = QVBoxLayout(self.input_group)
        il.addWidget(QLabel('<b>1  ·  בחירת קבצים</b>'))
        buttons = QHBoxLayout()
        self.single_button = QPushButton('בחירת קובץ')
        self.files_button = QPushButton('בחירת קבצים')
        self.folder_button = QPushButton('בחירת תיקייה')
        self.clear_button = QPushButton('נקה רשימה')
        for button in (self.single_button, self.files_button, self.folder_button, self.clear_button):
            buttons.addWidget(button)
        il.addLayout(buttons)
        self.inputs = InputList()
        il.addWidget(self.inputs)
        self.selection_label = QLabel('אפשר גם לגרור לכאן קובצי PDF או תיקיות')
        self.selection_label.setObjectName('muted')
        il.addWidget(self.selection_label)
        layout.addWidget(self.input_group)

        self.output_group = QGroupBox()
        ol = QVBoxLayout(self.output_group)
        ol.addWidget(QLabel('<b>2  ·  תיקיית יעד והגדרות</b>'))
        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.settings.value('output', '')))
        self.output_edit.setPlaceholderText('בחירת תיקייה לשמירת קובצי PDF')
        self.output_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.output_edit.setAccessibleName('תיקיית יעד')
        self.output_button = QPushButton('תיקיית יעד')
        output_row.addWidget(self.output_button)
        output_row.addWidget(self.output_edit, 1)
        ol.addLayout(output_row)
        grid = QGridLayout()
        self.recursive = QCheckBox('כלול תיקיות משנה')
        self.skip = QCheckBox('דלג על קבצים שכבר תוקנו')
        self.overwrite = QCheckBox('דרוס PDF קיים')
        self.strict = QCheckBox('אימות מלא של כל העמודים')
        self.thermal = QCheckBox('השהה בעומס חום גבוה')
        self.salvage = QCheckBox('אפשר שחזור בטוח של מבנה פגום')
        self.salvage.setChecked(self.settings.value('salvage', True, type=bool))
        self.salvage.setToolTip('שחזור מוגבל ומתועד. ערכים חסרים אינם מוצגים כמידע מקורי מוכח.')
        self.thermal.setToolTip('השהיית התחלות חדשות ב־85°C; חידוש ב־75°C. ללא חיישנים זמינים התיקון ממשיך.')
        self.strict.setToolTip('רינדור כל עמוד לבדיקה. ברירת המחדל בודקת עמוד ראשון, אמצעי ואחרון.')
        self.skip.setToolTip('דילוג רק כאשר חתימות המקור והפלט תואמות לרישום שאומת בהצלחה.')
        for key, control, default in [('recursive', self.recursive, False), ('skip', self.skip, True),
                                      ('strict', self.strict, False), ('thermal', self.thermal, False)]:
            control.setChecked(self.settings.value(key, default, type=bool))
        from ..thermal import supported as thermal_supported
        if not thermal_supported():
            self.thermal.setChecked(False)
            self.thermal.setEnabled(False)
            self.thermal.setToolTip('ניטור טמפרטורה אינו זמין במערכת זו. התיקון פועל כרגיל.')
        for i, control in enumerate((self.recursive, self.skip, self.strict, self.overwrite, self.thermal)):
            grid.addWidget(control, i // 2, i % 2)
        workers = QHBoxLayout()
        workers.addWidget(QLabel('תהליכים מקבילים:'))
        self.jobs = QComboBox()
        self.jobs.setMinimumSize(90, 38)
        self.jobs.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        for text, value in [('1',1), ('2',2), ('4',4), ('8',8), ('Auto',2)]:
            self.jobs.addItem(text, value)
        self.jobs.setCurrentIndex(int(self.settings.value('jobs_index', 1)))
        self.jobs.setToolTip('ברירת המחדל ו־Auto מפעילים שני תהליכים בלבד.')
        workers.addWidget(self.jobs)
        workers.addStretch()
        grid.addLayout(workers, 2, 1)
        grid.addWidget(self.salvage, 3, 0, 1, 2)
        ol.addLayout(grid)
        layout.addWidget(self.output_group)

        actions = QHBoxLayout()
        self.start_button = QPushButton('התחל תיקון')
        self.start_button.setObjectName('primary')
        self.start_button.setEnabled(False)
        self.cancel_button = QPushButton('עצור')
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        self.status = QLabel('מוכן לתיקון')
        actions.addWidget(self.status)
        layout.addLayout(actions)

        progress_group = QGroupBox()
        pl = QVBoxLayout(progress_group)
        pl.addWidget(QLabel('<b>3  ·  התקדמות</b>'))
        self.current = QLabel('קובץ נוכחי: —')
        self.current.setTextFormat(Qt.TextFormat.PlainText)
        pl.addWidget(self.current)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        pl.addWidget(self.progress)
        self.stats = QLabel('הושלמו: 0 / 0   ·   הצליחו: 0   ·   דולגו: 0   ·   נכשלו: 0   ·   לא נתמכו: 0')
        pl.addWidget(self.stats)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        self.log.setMinimumHeight(85)
        self.log.setAccessibleName('יומן פעילות')
        pl.addWidget(self.log)
        layout.addWidget(progress_group, 1)
        footer = QHBoxLayout()
        self.open_output_button = QPushButton('פתח תיקיית יעד')
        self.open_report_button = QPushButton('פתח דוח')
        self.details_button = QPushButton('פרטים טכניים')
        self.open_report_button.setEnabled(False)
        self.details_button.setEnabled(False)
        for button in (self.open_output_button, self.open_report_button, self.details_button):
            footer.addWidget(button)
        footer.addStretch()
        footer.addWidget(QLabel('קובצי המקור נשמרים ללא שינוי'))
        layout.addLayout(footer)

        self.single_button.clicked.connect(self.pick_single)
        self.files_button.clicked.connect(self.pick_files)
        self.folder_button.clicked.connect(self.pick_folder)
        self.output_button.clicked.connect(self.pick_output)
        self.clear_button.clicked.connect(self.clear_inputs)
        self.inputs.paths_dropped.connect(self.add_paths)
        self.output_edit.textChanged.connect(self.update_ready)
        self.start_button.clicked.connect(self.start_conversion)
        self.cancel_button.clicked.connect(self.cancel)
        self.overwrite.toggled.connect(self.overwrite_changed)
        self.open_output_button.clicked.connect(self.open_output)
        self.open_report_button.clicked.connect(self.open_report)
        self.details_button.clicked.connect(self.show_details)
        geometry = self.settings.value('geometry')
        if geometry:
            self.restoreGeometry(geometry)
        if inputs:
            self.add_paths(inputs)

    def recovery_mode(self):
        return 'safe-salvage' if self.salvage.isChecked() else 'exact'

    def pick_single(self):
        path, _ = QFileDialog.getOpenFileName(self, 'בחירת קובץ', self.last_input(), 'PDF (*.pdf *.PDF)')
        if path:
            self.add_paths([path])

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'בחירת קבצים', self.last_input(), 'PDF (*.pdf *.PDF)')
        self.add_paths(paths)

    def pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'בחירת תיקייה', self.last_input())
        if path:
            self.add_paths([path])

    def pick_output(self):
        path = QFileDialog.getExistingDirectory(self, 'תיקיית יעד', self.output_edit.text() or self.last_input())
        if path:
            self.output_edit.setText(path)

    def last_input(self):
        return str(self.settings.value('input', str(Path.home())))

    @Slot(list)
    def add_paths(self, paths):
        if self.worker and self.worker.isRunning():
            return
        existing = {self.inputs.item(i).text() for i in range(self.inputs.count())}
        self.inputs.setUpdatesEnabled(False)
        try:
            for raw in paths:
                path = Path(raw).expanduser().absolute()
                if str(path) not in existing and (path.is_dir() or path.suffix.lower() == '.pdf'):
                    self.inputs.addItem(str(path))
                    existing.add(str(path))
            if paths:
                first = Path(paths[0])
                self.settings.setValue('input', str(first if first.is_dir() else first.parent))
                if not self.output_edit.text():
                    self.output_edit.setText(str((first if first.is_dir() else first.parent) / 'PDF'))
        finally:
            self.inputs.setUpdatesEnabled(True)
        self.selection_label.setText(f'נבחרו {self.inputs.count()} קבצים או תיקיות  ·  אפשר לגרור פריטים נוספים לכאן')
        self.update_ready()

    def clear_inputs(self):
        self.inputs.clear()
        self.selection_label.setText('אפשר גם לגרור לכאן קובצי PDF או תיקיות')
        self.update_ready()

    def overwrite_changed(self, enabled):
        self.skip.setEnabled(not enabled)

    def update_ready(self):
        running = bool(self.worker and self.worker.isRunning())
        self.start_button.setEnabled(not running and self.inputs.count() > 0 and bool(self.output_edit.text().strip()))

    def save_settings(self):
        for key, value in {'output': self.output_edit.text(), 'recursive': self.recursive.isChecked(),
                           'skip': self.skip.isChecked(), 'strict': self.strict.isChecked(),
                           'salvage': self.salvage.isChecked(),
                           'thermal': self.thermal.isChecked(), 'jobs_index': self.jobs.currentIndex(),
                           'geometry': self.saveGeometry()}.items():
            self.settings.setValue(key, value)
        self.settings.sync()

    @Slot()
    def start_conversion(self):
        if self.worker and self.worker.isRunning():
            return
        self.save_settings()
        options = Options(overwrite=self.overwrite.isChecked(), skip_existing=self.skip.isChecked(),
                          recursive=self.recursive.isChecked(), jobs=self.jobs.currentData(),
                          validate_all_pages=self.strict.isChecked(), thermal_pause=self.thermal.isChecked(),
                          recovery=self.recovery_mode())
        self.last_summary = None
        self.last_error = ''
        self.details_button.setEnabled(False)
        self.open_report_button.setEnabled(False)
        self.log.clear()
        self.progress.setRange(0, 0)
        self.input_group.setEnabled(False)
        self.output_group.setEnabled(False)
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText('סורק קבצים…')
        self.worker = BatchThread([self.inputs.item(i).text() for i in range(self.inputs.count())],
                                  Path(self.output_edit.text()), options, self)
        self.worker.event.connect(self.on_event)
        self.worker.failed.connect(self.on_failure)
        self.worker.finished.connect(self.on_thread_finished)
        self.worker.start()

    @Slot()
    def cancel(self):
        if self.worker:
            self.worker.cancel()
            self.status.setText('עוצר בבטחה… ממתין לקבצים הפעילים')
            self.cancel_button.setEnabled(False)

    @Slot(dict)
    def on_event(self, event):
        kind = event['type']
        if kind == 'scanning':
            self.status.setText(f"סורק… נמצאו {event['count']} קבצים")
        elif kind == 'plan':
            self.progress.setRange(0, max(1, event['total']))
            self.progress.setValue(0)
        elif kind == 'started':
            self.status.setText('מתקן ובודק PDF…')
            self.current.setText('קובץ נוכחי: \u2066' + Path(event['source']).name + '\u2069')
        elif kind == 'thermal':
            self.status.setText('מושהה — ממתין לירידת הטמפרטורה' if event['paused'] else 'התיקון נמשך')
        elif kind == 'result':
            result = event['result']
            self.progress.setValue(event['completed'])
            self.update_stats(event['summary'], event['completed'])
            self.log.appendPlainText(f"{Path(result['source_path']).name}  ·  {STATUS_LABELS[result['status']]}")
            if result['error'] or result['warning']:
                import json
                self.last_error = result['error'] or (result['warning'] + '\n' + json.dumps(result['reconstructed_objects'], ensure_ascii=False, indent=2))
                self.details_button.setEnabled(True)
        elif kind == 'finished':
            self.last_summary = event['summary']
            self.open_report_button.setEnabled(True)

    def update_stats(self, s, count):
        self.stats.setText(f"הושלמו: {count} / {s['TOTAL']}   ·   הצליחו: {s['PASS']}   ·   דולגו: {s['SKIPPED']}   ·   נכשלו: {s['FAILED']}   ·   לא נתמכו: {s['UNSUPPORTED']}   ·   בוטלו: {s['CANCELLED']}")

    @Slot(str)
    def on_failure(self, error):
        self.last_error = error
        self.details_button.setEnabled(True)
        self.log.appendPlainText('לא ניתן להשלים את התיקון. ניתן לפתוח פרטים טכניים.')

    @Slot()
    def on_thread_finished(self):
        self.worker.wait()
        self.worker.deleteLater()
        self.worker = None
        self.input_group.setEnabled(True)
        self.output_group.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.update_ready()
        if self.pending_close:
            self.close()
            return
        s = self.last_summary
        if s is None:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.status.setText('שגיאה בתיקון')
            return
        self.status.setText('התיקון נעצר' if s['CANCELLED'] or s['SCAN_CANCELLED'] else 'התיקון הסתיים')
        dialog = QDialog(self)
        dialog.setWindowTitle(self.status.text())
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(self.status.text()))
        layout.addWidget(QLabel(f"סה״כ: {s['TOTAL']}  |  הצליחו: {s['PASS']}  |  דולגו: {s['SKIPPED']}\nנכשלו: {s['FAILED']}  |  לא נתמכו: {s['UNSUPPORTED']}  |  בוטלו: {s['CANCELLED']}"))
        row = QHBoxLayout()
        for label, callback in [('פתח תיקיית יעד', self.open_output), ('פתח דוח', self.open_report), ('סגור', dialog.accept)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        dialog.setModal(False)
        self.summary_dialog = dialog
        dialog.show()

    def open_output(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_edit.text()))

    def open_report(self):
        if self.last_summary:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_summary['REPORT'] + '.log'))

    def show_details(self):
        message = QMessageBox(self)
        message.setWindowTitle('פרטים טכניים')
        message.setText('פרטי הבדיקה והשחזור. קובצי המקור לא שונו.')
        message.setDetailedText(self.last_error)
        message.exec()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.pending_close = True
            self.cancel()
            event.ignore()
        else:
            self.save_settings()
            event.accept()
