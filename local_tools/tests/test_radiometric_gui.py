import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QImage, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from radiometric_calibrator.catalog import Catalog, Capture
from radiometric_calibrator.gui import ImageCanvas, MainWindow, load_preview
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
        self.window._retire_workers()
        for thread in self.window._retired_threads:
            thread.wait(5000)
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

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
        with patch('radiometric_calibrator.gui.register_rgb_to_band', return_value=RegistrationResult(np.eye(3), 'test', 1.0)), \
             patch('radiometric_calibrator.gui.roi_statistics', return_value=stats), \
             patch('radiometric_calibrator.gui.QFileDialog.getExistingDirectory', side_effect=AssertionError('unexpected folder dialog')), \
             patch.object(QMessageBox, 'information'):
            self.window._calculate()
        target = self.root / 'radiometric_calibration_coefficients.json'
        self.assertTrue(target.is_file())
        self.assertFalse(any(path.is_dir() for path in self.root.iterdir()))
        previous = target.read_bytes()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No), \
             patch('radiometric_calibrator.gui.register_rgb_to_band') as registration:
            self.window._calculate()
            registration.assert_not_called()
        self.assertEqual(target.read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
