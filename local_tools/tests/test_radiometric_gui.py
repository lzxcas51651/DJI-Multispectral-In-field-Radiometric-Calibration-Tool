import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QImage, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox

from radiometric_calibrator.catalog import Catalog, Capture
from radiometric_calibrator.gui import BandMappingDialog, ImageCanvas, MainWindow, RoiDialog, load_preview
from radiometric_calibrator.registration import RegistrationResult
from radiometric_calibrator.roi import RgbRoiAnnotation


class GuiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.photo = self.root / 'rgb.jpg'
        image = QImage(3000, 2000, QImage.Format_RGB32)
        image.fill(Qt.gray)
        self.assertTrue(image.save(str(self.photo)))
        self.window = MainWindow(QSettings(str(self.root / 'preferences.ini'), QSettings.IniFormat))

    def tearDown(self):
        self.wait_operation()
        self.window._retire_workers()
        for thread in self.window._retired_threads:
            thread.wait(5000)
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_operation(self):
        deadline = time.monotonic() + 10
        while self.window._busy and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.window._busy)

    def test_band_mapping_requires_complete_unique_choices(self):
        dialog = BandMappingDialog(['Green', 'Red'], ['Band 1', 'Band 2'])
        button = dialog.buttons.button(QDialogButtonBox.Ok)
        self.assertFalse(button.isEnabled())
        dialog.combos['Green'].setCurrentIndex(1)
        dialog.combos['Red'].setCurrentIndex(1)
        self.assertFalse(button.isEnabled())
        self.assertIn('重复', dialog.validation_label.text())
        dialog.combos['Red'].setCurrentIndex(2)
        self.assertTrue(button.isEnabled())
        self.assertEqual(dialog.band_map, {'Green': 1, 'Red': 2})
        reverse = BandMappingDialog(['Red', 'Green'], ['Green', 'Red'])
        self.assertEqual(reverse.validation_label.text(), '输出顺序：Green（输入 1） → Red（输入 2）')

    def test_background_operation_keeps_event_loop_responsive_and_handles_error(self):
        main_thread = threading.get_ident()
        seen = []
        release = threading.Event()
        def operation(progress):
            self.assertNotEqual(threading.get_ident(), main_thread)
            progress(25, 'working')
            release.wait(3)
            return 42
        self.window._start_operation('test', operation, seen.append)
        try:
            deadline = time.monotonic() + 2
            while self.window.progress_dialog.value() != 25 and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(self.window._busy)
            self.assertEqual(self.window.progress_dialog.value(), 25)
        finally:
            release.set()
        self.wait_operation()
        self.assertEqual(seen, [42])
        def failure(progress):
            raise ValueError('test error')
        with patch.object(QMessageBox, 'critical') as message:
            self.window._start_operation('test', failure, seen.append)
            self.wait_operation()
            message.assert_called_once()
        self.assertTrue(self.window.centralWidget().isEnabled())

    def annotation(self):
        return RgbRoiAnnotation('ROI-001', 'capture', str(self.photo), 'panel',
                                [[100, 100], [300, 100], [300, 300], [100, 300]],
                                {band: 0.5 for band in ('Green', 'Red', 'RedEdge', 'NIR')})

    def catalog(self):
        return Catalog(self.root, 'M3M', 'test', [],
                       [Capture('capture', {'RGB': self.photo, **{
                           band: self.root / f'{band}.tif' for band in ('Green', 'Red', 'RedEdge', 'NIR')
                       }})], [])

    def test_jpeg_thumbnail_uses_scaled_decoder(self):
        with patch('radiometric_calibrator.gui.read_unchanged', side_effect=AssertionError('full decode')):
            preview, original = load_preview(self.photo, 180)
        self.assertEqual(original.width(), 3000)
        self.assertEqual(preview.width(), 180)

    def test_panel_presets_uniform_individual_and_restart(self):
        settings = self.window.settings
        bands = ('Green', 'Red', 'RedEdge', 'NIR')
        dialog = RoiDialog(bands, settings=settings)
        self.assertGreaterEqual(dialog.panel_id.count(), 10)
        dialog.panel_id.setCurrentText('Panel-10')
        dialog.uniform_value.setValue(0.23)
        self.assertEqual(dialog.reflectance_by_band, dict.fromkeys(bands, 0.23))
        dialog.accept()
        restarted = RoiDialog(bands, settings=QSettings(settings.fileName(), QSettings.IniFormat))
        self.assertEqual(restarted.panel_id.currentText(), 'Panel-10')
        self.assertEqual(restarted.reflectance_by_band['NIR'], 0.23)
        restarted.panel_id.setCurrentText('Custom panel')
        restarted.uniform.setChecked(False)
        restarted.reflectance_inputs['NIR'].setValue(0.41)
        restarted.accept()
        again = RoiDialog(bands, settings=settings)
        self.assertFalse(again.uniform.isChecked())
        self.assertEqual(again.reflectance_by_band['NIR'], 0.41)
        again.panel_id.setCurrentText('Panel-10')
        self.assertTrue(again.uniform.isChecked())
        self.assertEqual(again.reflectance_by_band['NIR'], 0.23)
        again.uniform_value.setValue(0.99)
        again.reject()
        final = RoiDialog(bands, settings=settings)
        final.panel_id.setCurrentText('Panel-10')
        self.assertEqual(final.reflectance_by_band['NIR'], 0.23)
        different_sensor = RoiDialog(('Blue',) + bands, settings=settings)
        self.assertEqual(different_sensor.reflectance_by_band['NIR'], 0.5)

    def test_cache_reuses_preview_and_clear_releases_it(self):
        canvas = self.window.canvas
        with patch('radiometric_calibrator.gui.load_preview', wraps=load_preview) as loader:
            canvas.set_photo(self.photo)
            canvas.set_photo(self.photo)
            self.assertEqual(loader.call_count, 1)
        canvas.temp_rect = canvas.scene().addRect(0, 0, 5, 5)
        canvas.clear_photo()
        self.assertEqual(len(canvas.preview_cache), 0)
        self.assertIsNone(canvas.current_path)
        self.assertFalse(canvas.scene().items())

    def test_zoom_anchor_and_middle_drag_do_not_decode(self):
        canvas = self.window.canvas
        self.window.show()
        self.app.processEvents()
        canvas.set_photo(self.photo)
        self.app.processEvents()
        position = QPointF(160, 140)
        before = canvas.mapToScene(position.toPoint())
        wheel = QWheelEvent(position, position, QPoint(), QPoint(0, 120), Qt.NoButton,
                            Qt.NoModifier, Qt.NoScrollPhase, False)
        with patch('radiometric_calibrator.gui.load_preview', side_effect=AssertionError('decode during gesture')):
            canvas.wheelEvent(wheel)
            after = canvas.mapToScene(position.toPoint())
            self.assertLess((after - before).manhattanLength(), 5)
            horizontal = canvas.horizontalScrollBar().value()
            canvas.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, position, Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier))
            canvas.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, position + QPointF(40, 20), Qt.NoButton, Qt.MiddleButton, Qt.NoModifier))
            canvas.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, position + QPointF(40, 20), Qt.MiddleButton, Qt.NoButton, Qt.NoModifier))
            self.assertNotEqual(horizontal, canvas.horizontalScrollBar().value())
        self.assertFalse(canvas.middle_dragging)

    def test_clear_only_clears_session_and_ignores_old_thumbnail(self):
        self.window.catalog = self.catalog()
        self.window._populate_catalog()
        self.window.annotations = [self.annotation()]
        self.window.settings.setValue('last_batch_dir', str(self.root))
        original = self.photo.read_bytes()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.window.clear_task()
        for thread in self.window._retired_threads:
            thread.wait(5000)
        self.app.processEvents()
        self.assertEqual(self.window.capture_list.count(), 0)
        self.assertIsNone(self.window.catalog)
        self.assertIsNone(self.window.canvas.current_path)
        self.assertEqual(self.window.annotations, [])
        self.assertEqual(self.photo.read_bytes(), original)
        self.assertEqual(self.window.settings.value('last_batch_dir'), str(self.root))

    def test_calculation_saves_in_input_without_folder_dialog(self):
        self.window.catalog = self.catalog()
        self.window.annotations = [self.annotation()]
        stats = dict(pixel_count=100, mean=1000., median=1000., trimmed_mean=1000., stddev=1.,
                     cv_percent=0.1, minimum=999., maximum=1001.)
        with patch('radiometric_calibrator.processing.register_rgb_to_band', return_value=RegistrationResult(np.eye(3), 'test', 1.0)), \
             patch('radiometric_calibrator.processing.roi_statistics', return_value=stats), \
             patch('radiometric_calibrator.gui.QFileDialog.getExistingDirectory', side_effect=AssertionError('unexpected folder dialog')), \
             patch.object(QMessageBox, 'information'):
            self.window._calculate()
            self.wait_operation()
        target = self.root / 'radiometric_calibration_coefficients.json'
        self.assertTrue(target.is_file())
        self.assertFalse(any(path.is_dir() for path in self.root.iterdir()))
        previous = target.read_bytes()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No), \
             patch('radiometric_calibrator.processing.register_rgb_to_band') as registration:
            self.window._calculate()
            registration.assert_not_called()
        self.assertEqual(target.read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
