"""OCR and duplicates page: file list, run, progress, results, and the review of doubtful pairs."""
import json
import os
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QProcess, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from book2pdf.gui.widgets import InputList

STAGES = {'copy': 'מעתיק את הקובץ', 'ocr': 'מזהה טקסט (OCR)', 'features': 'סורק את העמודים',
          'compare': 'משווה בין עמודים', 'write': 'כותב את הקבצים'}
STATES = {'waiting': 'ממתין', 'running': 'רץ…', 'done': '✅ הושלם', 'failed': '🟥 נכשל', 'stopped': 'נעצר'}


@dataclass
class Job:
    source: Path
    out: Path
    ocr: bool
    deskew: bool
    state: str = 'waiting'
    summary: dict = None
    tail: list = field(default_factory=list)


def load_choices(out):
    try:
        data = json.loads((Path(out) / 'choices.json').read_text('utf-8'))
        return {'remove': sorted(int(page) for page in data['remove']),
                'not_duplicate': [[int(a), int(b)] for a, b in data['not_duplicate']]}
    except (OSError, ValueError, KeyError, TypeError):
        return {'remove': [], 'not_duplicate': []}


def save_choices(out, choices):
    (Path(out) / 'choices.json').write_text(json.dumps(choices, ensure_ascii=False), 'utf-8')


def open_pairs(summary, choices):
    """Doubtful pairs still waiting for an answer."""
    answered = {tuple(pair) for pair in choices['not_duplicate']}
    gone = set(choices['remove'])
    return [p for p in (summary or {}).get('review', [])
            if (p['a'], p['b']) not in answered and p['a'] not in gone and p['b'] not in gone]


def engine_args(job):
    args = ['-m', 'aag_pdf_suite.dedup.engine', str(job.source), '-o', str(job.out), '--managed']
    if not job.ocr:
        args.append('--no-ocr')
    if job.deskew:
        args.append('--deskew')
    remove = load_choices(job.out)['remove']
    if remove:
        args += ['--remove', ','.join(map(str, remove))]
    return args


def same_cache(folder, source, ocr, deskew):
    """True when the folder holds a run of this very file with these options, so its cache can be reused."""
    try:
        stamp = json.loads((folder / '.work' / 'stamp.json').read_text())
        info = source.stat()
    except (OSError, ValueError):
        return False
    return (stamp.get('size'), stamp.get('mtime'), stamp.get('ocr'), stamp.get('deskew')) == (
        info.st_size, int(info.st_mtime), ocr, deskew)


def pick_folder(source, root, taken, ocr, deskew):
    """One folder per file. Never reuses a folder that belongs to another file or to other options."""
    for number in range(1, 1000):
        folder = root / (source.stem if number == 1 else f'{source.stem}-{number}')
        if folder in taken:
            continue
        if not folder.exists() or same_cache(folder, source, ocr, deskew):
            return folder
    raise OSError(f'No free folder for {source.name}')


def isolate(path):
    """Keeps a Latin file name in one piece inside right-to-left text."""
    return f'⁦{path}⁩'


class PageImage(QLabel):
    """A page that fits its space and opens at full size on click."""

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(260, 300)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName('pageImage')
        self.setToolTip('לחיצה תגדיל את העמוד')
        self.pixmap_full = QPixmap()

    def show_file(self, path):
        self.pixmap_full = QPixmap(str(path))
        self.fit()

    def fit(self):
        if self.pixmap_full.isNull():
            self.setText('התמונה לא נמצאה')
            return
        size = self.size() * self.devicePixelRatioF()
        scaled = self.pixmap_full.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(self.devicePixelRatioF())
        self.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()

    def mouseReleaseEvent(self, event):
        if self.pixmap_full.isNull():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('עמוד בגודל מלא')
        dialog.resize(1000, 800)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel()
        label.setPixmap(self.pixmap_full)
        scroll.setWidget(label)
        layout.addWidget(scroll)
        dialog.exec()


class ReviewView(QWidget):
    """One doubtful pair at a time. Answers are saved at once, so closing the window loses nothing."""
    finished = Signal(bool)

    def __init__(self):
        super().__init__()
        self.job = None
        self.pairs = []
        self.base = {'remove': [], 'not_duplicate': []}
        self.answers = {}
        self.index = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)
        self.heading = QLabel()
        self.heading.setObjectName('title')
        layout.addWidget(self.heading)
        self.facts = QLabel()
        self.facts.setObjectName('muted')
        layout.addWidget(self.facts)

        images = QHBoxLayout()
        self.sides = []
        for _ in range(2):
            side = QVBoxLayout()
            image = PageImage()
            caption = QLabel()
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            side.addWidget(image, 1)
            side.addWidget(caption)
            images.addLayout(side, 1)
            self.sides.append((image, caption))
        layout.addLayout(images, 1)

        answers = QHBoxLayout()
        self.keep_button = QPushButton('לא כפול')
        self.remove_a = QPushButton()
        self.remove_b = QPushButton()
        for button in (self.keep_button, self.remove_a, self.remove_b):
            button.setObjectName('answer')
            button.setCheckable(True)
            answers.addWidget(button, 1)
        self.keep_button.clicked.connect(lambda: self.answer('keep'))
        self.remove_a.clicked.connect(lambda: self.answer(self.pairs[self.index]['a']))
        self.remove_b.clicked.connect(lambda: self.answer(self.pairs[self.index]['b']))
        layout.addLayout(answers)

        bottom = QHBoxLayout()
        self.previous_button = QPushButton('הקודם')
        self.next_button = QPushButton('הבא')
        self.previous_button.clicked.connect(lambda: self.move(-1))
        self.next_button.clicked.connect(lambda: self.move(1))
        self.status = QLabel()
        self.status.setObjectName('muted')
        self.back_button = QPushButton('חזרה')
        self.apply_button = QPushButton('החל החלטות')
        self.apply_button.setObjectName('primary')
        self.back_button.clicked.connect(lambda: self.finished.emit(False))
        self.apply_button.clicked.connect(self.apply)
        for widget in (self.previous_button, self.next_button, self.status):
            bottom.addWidget(widget)
        bottom.addStretch(1)
        bottom.addWidget(self.back_button)
        bottom.addWidget(self.apply_button)
        layout.addLayout(bottom)

    def load(self, job):
        self.job = job
        self.base = load_choices(job.out)
        self.pairs = open_pairs(job.summary, self.base)
        self.answers = {}
        self.index = 0
        self.show_pair()

    def show_pair(self):
        pair = self.pairs[self.index]
        a, b = pair['a'], pair['b']
        self.heading.setText(f'זוג {self.index + 1} מתוך {len(self.pairs)}')
        self.facts.setText(f"התאמת דיו: {pair['cover_a']:.0%} / {pair['cover_b']:.0%}"
                           f"  ·  נקודות תואמות: {pair['inliers']}")
        for (image, caption), page, thumb, ppi in ((self.sides[0], a, pair['image_a'], pair['ppi_a']),
                                                   (self.sides[1], b, pair['image_b'], pair['ppi_b'])):
            full = self.job.out / '.work' / 'pages' / f'{page:06d}.png'
            image.show_file(full if full.exists() else self.job.out / thumb)
            caption.setText(f'עמוד {page}' + (f'  ·  {ppi} ppi' if ppi else ''))
        self.remove_a.setText(f'כן, להסיר עמוד {a}')
        self.remove_b.setText(f'כן, להסיר עמוד {b}')
        chosen = self.answers.get((a, b))
        self.keep_button.setChecked(chosen == 'keep')
        self.remove_a.setChecked(chosen == a)
        self.remove_b.setChecked(chosen == b)
        self.previous_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index < len(self.pairs) - 1)
        self.status.setText(f'נענו {len(self.answers)} מתוך {len(self.pairs)}')

    def merged(self):
        remove = set(self.base['remove'])
        keep = list(self.base['not_duplicate'])
        for (a, b), chosen in self.answers.items():
            if chosen == 'keep':
                keep.append([a, b])
            else:
                remove.add(chosen)
        return {'remove': sorted(remove), 'not_duplicate': keep}

    def answer(self, chosen):
        pair = self.pairs[self.index]
        self.answers[(pair['a'], pair['b'])] = chosen
        save_choices(self.job.out, self.merged())
        if self.index < len(self.pairs) - 1:
            self.index += 1
        self.show_pair()

    def move(self, step):
        self.index += step
        self.show_pair()

    def apply(self):
        self.finished.emit(self.merged()['remove'] != self.base['remove'])


class DedupPage(QWidget):
    stopped = Signal()

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or QSettings('AAG', 'PDFSuite')
        self.jobs = []
        self.queue = []
        self.current = None
        self.process = None
        self.stopping = False
        self.buffer = b''
        self.blocked = 'בודק רכיבים…'
        self.converted = []
        self.clock = QElapsedTimer()
        self.ticker = QTimer(self)
        self.ticker.setInterval(1000)
        self.ticker.timeout.connect(self.show_time)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.review = ReviewView()
        self.review.finished.connect(self.review_finished)
        self.stack.addWidget(self.build_main())
        self.stack.addWidget(self.review)
        self.update_ready()

    def build_main(self):
        central = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)
        title = QLabel('OCR וסינון כפילויות')
        title.setObjectName('title')
        layout.addWidget(title)
        subtitle = QLabel('מוסיף טקסט לספרים סרוקים ומסיר עמודים שחוזרים על עצמם. הקובץ המקורי לא משתנה.')
        subtitle.setObjectName('muted')
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.input_group = QGroupBox('קובצי PDF')
        files = QVBoxLayout(self.input_group)
        self.inputs = InputList()
        self.inputs.setAccessibleName('קובצי PDF לעיבוד')
        self.inputs.paths_dropped.connect(self.add_paths)
        files.addWidget(self.inputs)
        hint = QLabel('אפשר לגרור לכאן קבצים או תיקייה')
        hint.setObjectName('muted')
        files.addWidget(hint)
        row = QHBoxLayout()
        self.add_button = QPushButton('הוספת קבצים')
        self.remove_button = QPushButton('הסרה מהרשימה')
        self.clear_button = QPushButton('נקה רשימה')
        self.converted_button = QPushButton()
        self.add_button.clicked.connect(self.pick_files)
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button.clicked.connect(self.clear_inputs)
        self.converted_button.clicked.connect(lambda: self.add_paths(self.converted))
        for button in (self.add_button, self.remove_button, self.clear_button, self.converted_button):
            row.addWidget(button)
        row.addStretch(1)
        files.addLayout(row)
        self.converted_button.hide()
        layout.addWidget(self.input_group)

        self.output_group = QGroupBox('תיקיית יעד')
        target = QVBoxLayout(self.output_group)
        line = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.settings.value('dedup/output', '')))
        self.output_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.output_edit.setAccessibleName('תיקיית יעד')
        self.output_edit.textChanged.connect(self.update_ready)
        self.output_button = QPushButton('בחירה')
        self.output_button.clicked.connect(self.pick_output)
        line.addWidget(self.output_edit, 1)
        line.addWidget(self.output_button)
        target.addLayout(line)
        self.ocr_box = QCheckBox('להוסיף טקסט בעברית ובאנגלית (OCR)')
        self.ocr_box.setChecked(self.settings.value('dedup/ocr', True, bool))
        self.deskew_box = QCheckBox('ליישר עמודים עקומים (הקובץ יקודד מחדש, ועלול לאבד מעט איכות)')
        self.deskew_box.setChecked(self.settings.value('dedup/deskew', False, bool))
        target.addWidget(self.ocr_box)
        target.addWidget(self.deskew_box)
        layout.addWidget(self.output_group)

        actions = QHBoxLayout()
        self.start_button = QPushButton('התחל')
        self.start_button.setObjectName('primary')
        self.stop_button = QPushButton('עצור')
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.reason = QLabel()
        self.reason.setObjectName('muted')
        self.reason.setWordWrap(True)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.reason, 1)
        layout.addLayout(actions)

        progress_group = QGroupBox('התקדמות')
        progress = QVBoxLayout(progress_group)
        self.stage = QLabel('מוכן')
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.time_label = QLabel()
        self.time_label.setObjectName('muted')
        for widget in (self.stage, self.progress, self.time_label):
            progress.addWidget(widget)
        layout.addWidget(progress_group)

        results = QGroupBox('תוצאות')
        result_layout = QVBoxLayout(results)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['קובץ', 'מצב', 'נשארו', 'הוסרו', 'לבדיקה'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setMinimumHeight(170)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.update_result_buttons)
        self.table.itemDoubleClicked.connect(lambda _: self.open_review())
        result_layout.addWidget(self.table)
        row = QHBoxLayout()
        self.review_button = QPushButton('בדיקת זוגות מסופקים')
        self.review_button.setObjectName('accent')
        self.folder_button = QPushButton('פתח תיקייה')
        self.report_button = QPushButton('פתח דוח')
        self.clean_button = QPushButton('מחיקת קבצי עבודה')
        self.clean_button.setToolTip('מוחק את קבצי הביניים הגדולים של הספר הזה. התוצאות נשארות.')
        self.review_button.clicked.connect(self.open_review)
        self.folder_button.clicked.connect(lambda: self.open_path(self.selected().out))
        self.report_button.clicked.connect(lambda: self.open_path(self.selected().out / 'report.html'))
        self.clean_button.clicked.connect(self.clean_work)
        for button in (self.review_button, self.folder_button, self.report_button, self.clean_button):
            row.addWidget(button)
        row.addStretch(1)
        result_layout.addLayout(row)
        layout.addWidget(results)
        layout.addStretch(1)
        self.update_result_buttons()
        return scroll

    # ---------------------------------------------------------------- inputs

    def paths(self):
        return [Path(self.inputs.item(i).text()) for i in range(self.inputs.count())]

    def add_paths(self, paths):
        known = set(self.paths())
        for raw in paths:
            path = Path(raw)
            found = sorted(path.glob('*.pdf')) if path.is_dir() else [path]
            for item in found:
                if item.suffix.lower() == '.pdf' and item.is_file() and item not in known:
                    known.add(item)
                    self.inputs.addItem(str(item))
        self.update_ready()

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'בחירת קובצי PDF', str(self.settings.value('dedup/input', '')),
                                                'PDF (*.pdf *.PDF)')
        if paths:
            self.settings.setValue('dedup/input', str(Path(paths[0]).parent))
            self.add_paths(paths)

    def remove_selected(self):
        for item in self.inputs.selectedItems():
            self.inputs.takeItem(self.inputs.row(item))
        self.update_ready()

    def clear_inputs(self):
        self.inputs.clear()
        self.update_ready()

    def pick_output(self):
        path = QFileDialog.getExistingDirectory(self, 'בחירת תיקיית יעד', self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def set_converted(self, paths):
        self.converted = list(paths)
        self.converted_button.setText(f'הוספת ה-PDF מהתיקון האחרון ({len(paths)})')
        self.converted_button.setVisible(bool(paths))

    def set_blocked(self, reason):
        self.blocked = reason
        self.update_ready()

    def update_ready(self):
        if self.blocked:
            reason = self.blocked
        elif not self.inputs.count():
            reason = 'צריך להוסיף לפחות קובץ אחד'
        elif not self.output_edit.text().strip():
            reason = 'צריך לבחור תיקיית יעד'
        else:
            reason = ''
        self.reason.setText(reason)
        self.start_button.setEnabled(not reason and not self.busy())

    # ---------------------------------------------------------------- running

    def busy(self):
        return self.process is not None

    def start(self):
        root = Path(self.output_edit.text().strip()).expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, 'תיקיית יעד', f'אי אפשר ליצור את התיקייה.\n{exc}')
            return
        self.settings.setValue('dedup/output', str(root))
        self.settings.setValue('dedup/ocr', self.ocr_box.isChecked())
        self.settings.setValue('dedup/deskew', self.deskew_box.isChecked())
        ocr, deskew = self.ocr_box.isChecked(), self.deskew_box.isChecked()
        taken = set()
        self.jobs = []
        for source in self.paths():
            folder = pick_folder(source, root, taken, ocr, deskew)
            taken.add(folder)
            self.jobs.append(Job(source, folder, ocr, deskew))
        self.queue = list(self.jobs)
        self.table.setRowCount(len(self.jobs))
        self.refresh_table()
        self.run_queue()

    def run_queue(self):
        self.stopping = False
        self.set_running(True)
        self.clock.start()
        self.ticker.start()
        self.show_time()
        self.next_job()

    def set_running(self, running):
        self.input_group.setEnabled(not running)
        self.output_group.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.update_result_buttons()

    def next_job(self):
        if not self.queue:
            self.finish_all()
            return
        self.current = self.queue.pop(0)
        self.current.state = 'running'
        self.current.out.mkdir(parents=True, exist_ok=True)
        self.buffer = b''
        self.current.tail = []
        self.refresh_table()
        self.stage.setText(f'{self.position()}  ·  מתחיל…')
        self.progress.setRange(0, 0)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.on_finished)
        self.process.errorOccurred.connect(self.on_error)
        self.process.start(sys.executable, engine_args(self.current))
        self.update_ready()

    def position(self):
        number = self.jobs.index(self.current) + 1 if self.current in self.jobs else 1
        return f'{self.current.source.name} ({number} מתוך {len(self.jobs)})'

    def read_output(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        *lines, self.buffer = self.buffer.split(b'\n')
        for raw in lines:
            line = raw.decode('utf-8', 'replace')
            if line.startswith('@@'):
                self.on_progress(json.loads(line[2:]))
            elif line.strip():
                self.current.tail = (self.current.tail + [line])[-12:]

    def on_progress(self, event):
        text = STAGES.get(event['stage'], event['stage'])
        total, done = event['total'], event['done']
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            text += f'  ·  {done:,} מתוך {total:,}'
        else:
            self.progress.setRange(0, 0)
        self.stage.setText(f'{self.position()}\n{text}')

    def on_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.current.tail.append(self.process.errorString())
            self.close_job(1)

    def on_finished(self, code, _status):
        self.read_output()
        self.close_job(code)

    def close_job(self, code):
        job = self.current
        self.process.deleteLater()
        self.process = None
        self.current = None
        if self.stopping:
            job.state = 'stopped'
            self.queue = []
        elif code == 0:
            try:
                job.summary = json.loads((job.out / 'summary.json').read_text('utf-8'))
                job.state = 'done'
            except (OSError, ValueError) as exc:
                job.tail.append(str(exc))
                job.state = 'failed'
        else:
            job.state = 'failed'
        self.refresh_table()
        self.next_job()

    def finish_all(self):
        self.ticker.stop()
        self.set_running(False)
        self.progress.setRange(0, 1)
        failed = [job for job in self.jobs if job.state == 'failed']
        if self.stopping:
            self.stage.setText('נעצר. אפשר להפעיל שוב, והעבודה תמשיך ממה שכבר נשמר.')
            self.progress.setValue(0)
        elif failed:
            self.stage.setText(f'הסתיים, {len(failed)} קבצים נכשלו. הפרטים במצב של כל קובץ.')
            self.progress.setValue(1)
        else:
            self.stage.setText('הסתיים')
            self.progress.setValue(1)
        self.update_ready()
        if self.table.currentRow() < 0 and self.jobs:
            self.table.selectRow(0)
        self.stopped.emit()

    def stop(self):
        if not self.process:
            return
        self.stopping = True
        self.stop_button.setEnabled(False)
        self.stage.setText('עוצר…')
        pid = self.process.processId()
        try:
            if os.getpgid(pid) != pid:
                raise ProcessLookupError
            os.killpg(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            self.process.terminate()

    def show_time(self):
        seconds = self.clock.elapsed() // 1000
        self.time_label.setText(f'זמן שעבר: {seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}')

    # ---------------------------------------------------------------- results

    def refresh_table(self):
        for index, job in enumerate(self.jobs):
            summary = job.summary or {}
            waiting = len(open_pairs(summary, load_choices(job.out))) if summary else ''
            values = (isolate(job.source.name), STATES[job.state], summary.get('kept', ''), summary.get('removed', ''),
                      waiting)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if job.state == 'failed' and job.tail:
                    item.setToolTip('\n'.join(job.tail))
                self.table.setItem(index, column, item)
        self.update_result_buttons()

    def selected(self):
        row = self.table.currentRow()
        return self.jobs[row] if 0 <= row < len(self.jobs) else None

    def update_result_buttons(self):
        job = self.selected()
        idle = not self.busy() and bool(job)
        done = idle and job.state == 'done'
        count = len(open_pairs(job.summary, load_choices(job.out))) if done else 0
        self.review_button.setText(f'בדיקת זוגות מסופקים ({count})' if count else 'בדיקת זוגות מסופקים')
        self.review_button.setEnabled(bool(count))
        self.folder_button.setEnabled(bool(job) and job.out.exists())
        self.report_button.setEnabled(done)
        self.clean_button.setEnabled(done and (job.out / '.work').exists() and not count)

    def open_path(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_review(self):
        job = self.selected()
        if not job or self.busy() or job.state != 'done' or not open_pairs(job.summary, load_choices(job.out)):
            return
        self.review.load(job)
        self.stack.setCurrentIndex(1)

    def review_finished(self, changed):
        self.stack.setCurrentIndex(0)
        job = self.review.job
        if changed:
            job.state = 'waiting'
            self.queue = [job]
            self.run_queue()
        self.refresh_table()

    def clean_work(self):
        job = self.selected()
        work = job.out / '.work'
        answer = QMessageBox.question(self, 'מחיקת קבצי עבודה',
                                      f'למחוק את קבצי העבודה של {isolate(job.source.name)}?\n'
                                      'הקובץ הנקי והדוח נשארים. הרצה חוזרת של הספר תתחיל מהתחלה.')
        if answer == QMessageBox.StandardButton.Yes:
            shutil.rmtree(work, ignore_errors=True)
            self.update_result_buttons()
