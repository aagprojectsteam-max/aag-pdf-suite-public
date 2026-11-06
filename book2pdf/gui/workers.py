import threading
from PySide6.QtCore import QThread, Signal
from ..batch import run_batch


class BatchThread(QThread):
    event = Signal(dict)
    failed = Signal(str)

    def __init__(self, inputs, output, options, parent=None):
        super().__init__(parent)
        self.inputs = inputs
        self.output = output
        self.options = options
        self.cancel_event = threading.Event()

    def run(self):
        try:
            run_batch(self.inputs, self.output, self.options, cancel=self.cancel_event, on_event=self.event.emit)
        except Exception as exc:
            self.failed.emit(str(exc))

    def cancel(self):
        self.cancel_event.set()
