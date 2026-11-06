from pathlib import Path
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import QFileDialog

from book2pdf.gui.main_window import MainWindow


def test_gui_conversion_pickers_progress_close(qtbot, book, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    window = MainWindow(settings=settings)
    qtbot.addWidget(window)
    window.show()
    assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, **kw: (str(book), ''))
    qtbot.mouseClick(window.single_button, Qt.MouseButton.LeftButton)
    assert window.inputs.count() == 1
    folder = tmp_path / 'folder'
    folder.mkdir()
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *a, **kw: str(folder))
    qtbot.mouseClick(window.folder_button, Qt.MouseButton.LeftButton)
    assert window.inputs.count() == 2
    output = tmp_path / 'pdf'
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *a, **kw: str(output))
    qtbot.mouseClick(window.output_button, Qt.MouseButton.LeftButton)
    assert window.output_edit.text() == str(output)
    ticks = []
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    qtbot.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    assert not window.input_group.isEnabled()
    qtbot.waitUntil(lambda: window.worker is None, timeout=30000)
    assert window.last_summary['PASS'] == 1
    assert window.progress.value() == 1
    assert (output / 'source.pdf').exists()
    assert 'הושלם' in window.log.toPlainText()
    assert len(ticks) > 1
    window.summary_dialog.close()
    window.close()
    assert settings.value('output') == str(output)


def test_gui_close_during_conversion(qtbot, book, tmp_path):
    window = MainWindow([str(book)], QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    qtbot.addWidget(window)
    window.output_edit.setText(str(tmp_path / 'output'))
    window.show()
    window.start_conversion()
    window.close()
    qtbot.waitUntil(lambda: window.worker is None, timeout=30000)
    assert not window.isVisible()
    assert not list((tmp_path / 'output').glob('*.tmp.pdf'))


def test_gui_drop_backend_and_bounded_log(qtbot, book, tmp_path):
    window = MainWindow(settings=QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    qtbot.addWidget(window)
    window.inputs.paths_dropped.emit([str(book), str(book)])
    assert window.inputs.count() == 1
    for i in range(1600):
        window.log.appendPlainText(str(i))
    assert window.log.document().blockCount() == 1500


def test_unsupported_thermal_option_disabled(qtbot, tmp_path, monkeypatch):
    from book2pdf import thermal
    monkeypatch.setattr(thermal, 'supported', lambda: False)
    settings = QSettings(str(tmp_path / 'thermal.ini'), QSettings.Format.IniFormat)
    settings.setValue('thermal', True)
    window = MainWindow(settings=settings)
    qtbot.addWidget(window)
    assert not window.thermal.isEnabled()
    assert not window.thermal.isChecked()
    assert window.thermal.toolTip()
