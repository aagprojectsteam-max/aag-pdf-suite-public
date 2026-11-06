"""The components check page."""
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..components import CONVERT, DEDUP, broken_libraries, check_all, fix_command, missing


class CheckThread(QThread):
    done = Signal(list)

    def run(self):
        self.done.emit(check_all())


def mark(row):
    if row.ok:
        return '✅ תקין'
    return '🟥 חסר' if row.required else '🟨 חסר, לא חובה'


class ComponentsPage(QWidget):
    checked = Signal(list)

    def __init__(self):
        super().__init__()
        self.thread = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)
        title = QLabel('בדיקת רכיבים')
        title.setObjectName('title')
        layout.addWidget(title)
        subtitle = QLabel('האם כל מה שהתוכנה צריכה נמצא במחשב. הבדיקה רק קוראת, ולא מתקינה כלום.')
        subtitle.setObjectName('muted')
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.summary = QLabel('בודק…')
        self.summary.setObjectName('summary')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['רכיב', 'למה הוא נדרש', 'מצב', 'גרסה'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self.fix_group = QGroupBox('מה צריך להשלים')
        fix = QVBoxLayout(self.fix_group)
        self.fix_note = QLabel()
        self.fix_note.setWordWrap(True)
        fix.addWidget(self.fix_note)
        line = QHBoxLayout()
        self.fix_command = QLineEdit()
        self.fix_command.setReadOnly(True)
        self.fix_command.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.fix_command.setAccessibleName('פקודת ההתקנה')
        self.copy_button = QPushButton('העתקה')
        self.copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.fix_command.text()))
        line.addWidget(self.fix_command, 1)
        line.addWidget(self.copy_button)
        fix.addLayout(line)
        self.fix_group.hide()
        layout.addWidget(self.fix_group)

        buttons = QHBoxLayout()
        self.recheck_button = QPushButton('בדיקה חוזרת')
        self.recheck_button.clicked.connect(self.run_check)
        buttons.addWidget(self.recheck_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    def run_check(self):
        if self.thread:
            return
        self.recheck_button.setEnabled(False)
        self.summary.setText('בודק…')
        self.thread = CheckThread(self)
        self.thread.done.connect(self.show_rows)
        self.thread.finished.connect(self.on_finished)
        self.thread.start()

    def on_finished(self):
        self.thread.deleteLater()
        self.thread = None
        self.recheck_button.setEnabled(True)

    def show_rows(self, rows):
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            detail = row.note and f'{row.purpose} ({row.note})'
            for column, text in enumerate((row.title, detail or row.purpose, mark(row), row.version)):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(index, column, item)
        lines = []
        for feature in (CONVERT, DEDUP):
            gaps = missing(rows, feature)
            lines.append(f"🟥 {feature}: חסר {', '.join(row.title for row in gaps)}" if gaps
                         else f'✅ {feature}: הכול מוכן')
        self.summary.setText('\n'.join(lines))
        command = fix_command(rows)
        broken = broken_libraries(rows)
        notes = []
        if command:
            notes.append('הפקודה הזאת תשלים את הרכיבים החסרים. התוכנה לא מריצה אותה בעצמה.')
        if broken:
            notes.append(f"חלק מהתוכנה עצמה לא נטען: {', '.join(row.title for row in broken)}. "
                         'התקנה מחדש של AAG - PDF Suite אמורה לפתור את זה.')
        self.fix_note.setText('\n'.join(notes))
        self.fix_command.setText(command)
        self.fix_command.setVisible(bool(command))
        self.copy_button.setVisible(bool(command))
        self.fix_group.setVisible(bool(notes))
        self.checked.emit(rows)
