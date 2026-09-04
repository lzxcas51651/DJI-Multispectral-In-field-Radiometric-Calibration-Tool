from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QImageReader, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .calibration import BandModel, fit_models
from . import __version__
from .candidate import Candidate, find_candidates
from .catalog import BANDS, Capture, Catalog, scan_folder
from .geotiff import apply_models, describe_bands
from .metadata import read_image_metadata
from .project import COEFFICIENTS_FILENAME, load_coefficients, save_coefficients
from .registration import register_rgb_to_band, transform_polygon
from .roi import RgbRoiAnnotation, RoiSample, read_unchanged, roi_statistics


COLORS = [
    "#ff3b30",
    "#007aff",
    "#34c759",
    "#ff9500",
    "#af52de",
    "#00c7be",
    "#ff2d55",
]


def load_preview(path: Path, maximum: int = 1600) -> tuple[QImage, QSize]:
    # JPEG previews decode directly at a reduced size. Preserve raw pixel
    # orientation, because ROI coordinates refer to the original sensor image.
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        reader = QImageReader(str(path))
        reader.setAutoTransform(False)
        original_size = reader.size()
        if original_size.isValid():
            reader.setScaledSize(original_size.scaled(maximum, maximum, Qt.KeepAspectRatio))
            image = reader.read()
            if not image.isNull():
                return image, original_size
    image = read_unchanged(path)
    original_size = QSize(image.shape[1], image.shape[0])
    if image.ndim == 2:
        finite = image[np.isfinite(image)]
        if finite.size == 0:
            raise ValueError(f"影像没有有效像素：{path}")
        low, high = np.percentile(finite, (1, 99))
        if high <= low:
            high = low + 1.0
        display = np.clip((image.astype(np.float32) - low) * 255.0 / (high - low), 0, 255).astype(np.uint8)
        display = cv2.cvtColor(display, cv2.COLOR_GRAY2RGB)
    else:
        if image.shape[2] == 4:
            display = cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA)
        else:
            if image.dtype != np.uint8:
                finite = image[np.isfinite(image)]
                low, high = np.percentile(finite, (1, 99))
                image = np.clip((image.astype(np.float32) - low) * 255.0 / max(high - low, 1.0), 0, 255).astype(np.uint8)
            display = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    height, width = display.shape[:2]
    scale = min(1.0, maximum / max(width, height))
    if scale < 1:
        display = cv2.resize(display, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    height, width = display.shape[:2]
    fmt = QImage.Format_RGBA8888 if display.ndim == 3 and display.shape[2] == 4 else QImage.Format_RGB888
    return QImage(display.data, width, height, display.strides[0], fmt).copy(), original_size


def preview_image(path: Path, maximum: int = 1600) -> QImage:
    return load_preview(path, maximum)[0]


class ThumbnailThread(QThread):
    ready = Signal(str, QImage)

    def __init__(self, captures: list[Capture]):
        super().__init__()
        self.captures = captures

    def run(self) -> None:
        for capture in self.captures:
            if self.isInterruptionRequested():
                return
            path = capture.preview_path
            if path is None:
                continue
            try:
                self.ready.emit(capture.key, preview_image(path, 180))
            except Exception:
                continue


class CandidateThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, paths: list[Path]):
        super().__init__()
        self.paths = paths

    def run(self) -> None:
        try:
            result = find_candidates(self.paths)
            if not self.isInterruptionRequested():
                self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class RoiDialog(QDialog):
    def __init__(self, bands: tuple[str, ...], parent=None):
        super().__init__(parent)
        self.setWindowTitle("新增 RGB 定标布 ROI")
        self.setMinimumWidth(420)
        layout = QFormLayout(self)
        self.panel_id = QComboBox()
        self.panel_id.setEditable(True)
        self.panel_id.addItems(["Panel-01", "Panel-02", "Panel-03"])
        layout.addRow("定标布编号", self.panel_id)
        layout.addRow(QLabel("输入定标布证书中各波段的反射率："))
        self.reflectance_inputs: dict[str, QDoubleSpinBox] = {}
        for band in bands:
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1.5)
            spin.setDecimals(6)
            spin.setSingleStep(0.01)
            spin.setValue(0.5)
            self.reflectance_inputs[band] = spin
            layout.addRow(f"{band} 反射率（0～1）", spin)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    @property
    def reflectance_by_band(self) -> dict[str, float]:
        return {band: spin.value() for band, spin in self.reflectance_inputs.items()}


class ImageCanvas(QGraphicsView):
    roi_created = Signal(object)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.scene().setItemIndexMethod(QGraphicsScene.NoIndex)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setViewportUpdateMode(QGraphicsView.MinimalViewportUpdate)
        self.pixmap_item: QGraphicsPixmapItem | None = None
        self.mode = "rectangle"
        self.start: QPointF | None = None
        self.temp_rect: QGraphicsRectItem | None = None
        self.polygon_points: list[QPointF] = []
        self.temp_polygon: QGraphicsPolygonItem | None = None
        self.overlay_items: list = []
        self.current_path: Path | None = None
        self.image_scale = 1.0
        self.image_scale_y = 1.0
        self.preview_cache = OrderedDict()
        self.quality_timer = QTimer(self)
        self.quality_timer.setSingleShot(True)
        self.quality_timer.timeout.connect(self._restore_quality)
        self.middle_dragging = False
        self.middle_last_position: QPoint | None = None
        self.viewport().setCursor(Qt.CrossCursor)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.setDragMode(QGraphicsView.NoDrag)
        self.viewport().setCursor(Qt.CrossCursor)
        self._clear_temp()

    def set_photo(self, path: Path) -> None:
        if path == self.current_path:
            return
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key not in self.preview_cache:
            self.preview_cache[key] = load_preview(path, 2400)
            while len(self.preview_cache) > 3:
                self.preview_cache.popitem(last=False)
        self.preview_cache.move_to_end(key)
        image, original_size = self.preview_cache[key]
        self.clear_photo(clear_cache=False)
        self.image_scale = image.width() / original_size.width()
        self.image_scale_y = image.height() / original_size.height()
        self.pixmap_item = self.scene().addPixmap(QPixmap.fromImage(image))
        self.pixmap_item.setShapeMode(QGraphicsPixmapItem.BoundingRectShape)
        self.pixmap_item.setTransformationMode(Qt.SmoothTransformation)
        rect = self.pixmap_item.boundingRect()
        # Margin allows cursor-centred zoom and middle-drag even at fit scale.
        self.scene().setSceneRect(rect.adjusted(-rect.width(), -rect.height(), rect.width(), rect.height()))
        self.current_path = path
        self.fitInView(self.pixmap_item, Qt.KeepAspectRatio)

    def clear_photo(self, clear_cache: bool = True) -> None:
        self.quality_timer.stop()
        self._clear_temp()  # Must run before scene.clear deletes C++ items.
        self.scene().clear()
        self.overlay_items.clear()
        self.pixmap_item = None
        self.current_path = None
        self.middle_dragging = False
        self.middle_last_position = None
        self.resetTransform()
        self.scene().setSceneRect(QRectF())
        if clear_cache:
            self.preview_cache.clear()

    def _fast_interaction(self) -> None:
        self.setRenderHint(QPainter.SmoothPixmapTransform, False)
        if self.pixmap_item:
            self.pixmap_item.setTransformationMode(Qt.FastTransformation)
        self.quality_timer.start(140)

    def _restore_quality(self) -> None:
        if self.middle_dragging:
            self.quality_timer.start(140)
            return
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if self.pixmap_item:
            self.pixmap_item.setTransformationMode(Qt.SmoothTransformation)
        self.viewport().update()

    def show_rois(self, samples: list[RoiSample], selected_id: str | None = None) -> None:
        for item in self.overlay_items:
            self.scene().removeItem(item)
        self.overlay_items.clear()
        if self.current_path is None:
            return
        for index, sample in enumerate(samples):
            if Path(sample.image_path).resolve() != self.current_path.resolve():
                continue
            points = [QPointF(x * self.image_scale, y * self.image_scale_y) for x, y in sample.polygon]
            color = QColor(COLORS[(int(sample.roi_id.split("-")[-1]) - 1) % len(COLORS)])
            width = 4 if sample.roi_id == selected_id else 2
            item = self.scene().addPolygon(QPolygonF(points), QPen(color, width))
            item.setBrush(QColor(color.red(), color.green(), color.blue(), 30))
            label = self.scene().addSimpleText(sample.roi_id)
            label.setBrush(color)
            label.setPos(points[0])
            self.overlay_items.extend([item, label])

    def wheelEvent(self, event) -> None:
        if not self.pixmap_item or not event.angleDelta().y():
            event.accept()
            return
        self._fast_interaction()
        old_position = self.mapToScene(event.position().toPoint())
        factor = 1.15 ** (max(-480, min(480, event.angleDelta().y())) / 120.0)
        if not 0.01 <= self.transform().m11() * factor <= 32.0:
            event.accept()
            return
        self.scale(factor, factor)
        new_position = self.mapToScene(event.position().toPoint())
        delta = new_position - old_position
        self.translate(delta.x(), delta.y())
        event.accept()

    def _inside(self, point: QPointF) -> bool:
        return self.pixmap_item is not None and self.pixmap_item.boundingRect().contains(point)

    def _clear_temp(self) -> None:
        for item in (self.temp_rect, self.temp_polygon):
            if item is not None and item.scene() is not None:
                self.scene().removeItem(item)
        self.temp_rect = None
        self.temp_polygon = None
        self.polygon_points = []
        self.start = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._fast_interaction()
            self.middle_dragging = True
            self.middle_last_position = event.position().toPoint()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        point = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.LeftButton and self._inside(point):
            if self.mode == "rectangle":
                self.start = point
                self.temp_rect = self.scene().addRect(QRectF(point, point), QPen(QColor("#ff3b30"), 2))
                return
            if self.mode == "polygon":
                self.polygon_points.append(point)
                if self.temp_polygon is None:
                    self.temp_polygon = self.scene().addPolygon(QPolygonF(self.polygon_points), QPen(QColor("#ff3b30"), 2))
                else:
                    self.temp_polygon.setPolygon(QPolygonF(self.polygon_points))
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.middle_dragging and self.middle_last_position is not None:
            self._fast_interaction()
            current = event.position().toPoint()
            delta = current - self.middle_last_position
            self.middle_last_position = current
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        if self.mode == "rectangle" and self.start is not None and self.temp_rect is not None:
            point = self.mapToScene(event.position().toPoint())
            self.temp_rect.setRect(QRectF(self.start, point).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton and self.middle_dragging:
            self.middle_dragging = False
            self.middle_last_position = None
            self.viewport().setCursor(Qt.CrossCursor)
            self.quality_timer.start(140)
            event.accept()
            return
        if self.mode == "rectangle" and self.temp_rect is not None and self.start is not None:
            rect = self.temp_rect.rect()
            if rect.width() >= 8 and rect.height() >= 8:
                self.roi_created.emit(
                    [[rect.left() / self.image_scale, rect.top() / self.image_scale_y],
                     [rect.right() / self.image_scale, rect.top() / self.image_scale_y],
                     [rect.right() / self.image_scale, rect.bottom() / self.image_scale_y],
                     [rect.left() / self.image_scale, rect.bottom() / self.image_scale_y]]
                )
            self._clear_temp()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self.mode == "polygon" and len(self.polygon_points) >= 3:
            polygon = [[point.x() / self.image_scale, point.y() / self.image_scale_y] for point in self.polygon_points]
            self.roi_created.emit(polygon)
            self._clear_temp()
            return
        super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self.setWindowTitle(f"DJI 多光谱现场辐射定标工具 {__version__}")
        self.resize(1500, 900)
        self.catalog: Catalog | None = None
        self.annotations: list[RgbRoiAnnotation] = []
        self.samples: list[RoiSample] = []
        self.models: dict[str, BandModel] = {}
        self.current_capture: Capture | None = None
        self.current_band = "RGB"
        self.project_dir: Path | None = None
        self.thumbnail_thread: ThumbnailThread | None = None
        self.candidate_thread: CandidateThread | None = None
        self._retired_threads = []
        self._generation = 0
        self._busy = False
        self.settings = settings if settings is not None else QSettings("DJI-Local-Tools", "Radiometric-Calibrator")
        self._build_ui()
        geometry = self.settings.value("window_geometry")
        if geometry:
            self.restoreGeometry(geometry)
        splitter_state = self.settings.value("main_splitter")
        if splitter_state:
            self.splitter.restoreState(splitter_state)

    def _build_ui(self) -> None:
        toolbar = QToolBar("主工具栏")
        self.main_toolbar = toolbar
        self.addToolBar(toolbar)
        actions = [
            ("打开批次", self.open_folder),
            ("清空当前任务", self.clear_task),
            ("导入定标布影像", self.import_image),
            ("自动查找定标布", self.auto_find),
            ("计算并保存系数", self.calculate),
            ("应用到 DN 正射影像", self.apply_to_orthophoto),
            ("载入系数项目", self.load_project),
        ]
        for title, callback in actions:
            action = QAction(title, self)
            action.triggered.connect(callback)
            toolbar.addAction(action)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        sensor_row = QHBoxLayout()
        sensor_row.addWidget(QLabel("传感器"))
        self.sensor_combo = QComboBox()
        self.sensor_combo.addItems(["AUTO", "P4M", "M3M"])
        sensor_row.addWidget(self.sensor_combo)
        left_layout.addLayout(sensor_row)
        self.sensor_info = QLabel("尚未打开批次")
        self.sensor_info.setWordWrap(True)
        left_layout.addWidget(self.sensor_info)
        self.capture_list = QListWidget()
        self.capture_list.setViewMode(QListView.IconMode)
        self.capture_list.setFlow(QListView.TopToBottom)
        self.capture_list.setWrapping(False)
        self.capture_list.setResizeMode(QListView.Adjust)
        self.capture_list.setWordWrap(True)
        self.capture_list.setIconSize(QSize(210, 118))
        self.capture_list.setGridSize(QSize(240, 158))
        self.capture_list.setSpacing(6)
        self.capture_list.currentItemChanged.connect(self.capture_changed)
        left_layout.addWidget(self.capture_list, 1)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        canvas_tools = QHBoxLayout()
        canvas_tools.addWidget(QLabel("RGB 标注工具"))
        for title, mode in (("▭ 矩形 ROI", "rectangle"), ("⬠ 多边形 ROI", "polygon")):
            button = QPushButton(title)
            button.setProperty("toolButton", True)
            button.clicked.connect(lambda checked=False, value=mode: self.canvas.set_mode(value))
            canvas_tools.addWidget(button)
        canvas_tools.addStretch(1)
        canvas_tools.addWidget(QLabel("滚轮缩放 · 中键拖动"))
        center_layout.addLayout(canvas_tools)
        self.canvas = ImageCanvas()
        self.canvas.roi_created.connect(self.add_roi)
        center_layout.addWidget(self.canvas, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("RGB ROI 列表（双击启用/禁用）"))
        self.roi_table = QTableWidget(0, 4)
        self.roi_table.setHorizontalHeaderLabels(["编号", "定标布", "各波段反射率", "使用"])
        self.roi_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.roi_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.roi_table.itemSelectionChanged.connect(self.roi_selected)
        self.roi_table.cellDoubleClicked.connect(self.toggle_roi)
        right_layout.addWidget(self.roi_table, 1)
        buttons = QHBoxLayout()
        delete = QPushButton("删除选中 ROI")
        delete.clicked.connect(self.delete_roi)
        buttons.addWidget(delete)
        right_layout.addLayout(buttons)
        self.model_label = QLabel("尚未计算定标系数")
        self.model_label.setWordWrap(True)
        right_layout.addWidget(self.model_label)

        self.splitter = QSplitter()
        self.splitter.setHandleWidth(8)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(left)
        self.splitter.addWidget(center)
        self.splitter.addWidget(right)
        self.splitter.setSizes([300, 820, 380])
        self.setCentralWidget(self.splitter)
        self.setStyleSheet(self._style_sheet())
        self.statusBar().showMessage("打开一个 P4M 或 M3M 飞行批次开始。自动查找默认不运行。")

    @staticmethod
    def _style_sheet() -> str:
        return """
        QMainWindow, QWidget { background: #f4f6f8; color: #202124; font-family: "Segoe UI", "Microsoft YaHei UI"; font-size: 13px; }
        QToolBar { background: #ffffff; border: none; border-bottom: 1px solid #dfe3e8; spacing: 6px; padding: 8px; }
        QToolBar QToolButton, QPushButton { background: #ffffff; border: 1px solid #cfd6de; border-radius: 6px; padding: 7px 12px; }
        QToolBar QToolButton:hover, QPushButton:hover { background: #eef5ff; border-color: #7aa7df; }
        QPushButton[toolButton="true"] { background: #eaf2ff; color: #174ea6; border-color: #b7cff2; font-weight: 600; }
        QListWidget, QTableWidget, QGraphicsView, QComboBox, QDoubleSpinBox { background: #ffffff; border: 1px solid #d8dde3; border-radius: 7px; }
        QListWidget::item { border-radius: 7px; padding: 5px; }
        QListWidget::item:selected { background: #dceaff; color: #123b72; }
        QHeaderView::section { background: #edf1f5; border: none; border-right: 1px solid #d8dde3; padding: 7px; font-weight: 600; }
        QSplitter::handle { background: #d9dee5; margin: 2px; border-radius: 3px; }
        QSplitter::handle:hover { background: #7aa7df; }
        QStatusBar { background: #ffffff; border-top: 1px solid #dfe3e8; }
        """

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
            self.statusBar().showMessage("正在计算，请完成后关闭。")
            return
        self._retire_workers()
        if any(thread.isRunning() for thread in self._retired_threads):
            event.ignore()
            QTimer.singleShot(200, self.close)
            return
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.setValue("main_splitter", self.splitter.saveState())
        super().closeEvent(event)

    def _retire_workers(self) -> None:
        for thread in (self.thumbnail_thread, self.candidate_thread):
            if thread and thread.isRunning():
                thread.requestInterruption()
                self._retired_threads.append(thread)
        self.thumbnail_thread = self.candidate_thread = None
        self._retired_threads = [thread for thread in self._retired_threads if thread.isRunning()]

    def _confirm_discard(self) -> bool:
        if not self.annotations and not self.samples:
            return True
        return QMessageBox.question(
            self, "清空当前任务？",
            "将清除当前窗口的影像列表、ROI 和计算结果。未保存的标注会丢失。\n"
            "不会删除磁盘上的原始影像或已保存的系数文件。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.Yes

    def _clear_task_state(self) -> None:
        self._generation += 1
        self._retire_workers()
        self.catalog = self.current_capture = None
        self.annotations, self.samples, self.models = [], [], {}
        self.project_dir = None
        self.capture_list.clear()
        self.canvas.clear_photo()
        self.canvas.set_mode("rectangle")
        self.roi_table.blockSignals(True)
        self.roi_table.setRowCount(0)
        self.roi_table.blockSignals(False)
        self.sensor_combo.setCurrentText("AUTO")
        self.sensor_info.setText("尚未打开批次")
        self.model_label.setText("尚未计算定标系数")

    def clear_task(self) -> None:
        if self._busy or not self._confirm_discard():
            return
        self._clear_task_state()
        self.statusBar().showMessage("已清空当前任务，磁盘文件未删除。可重新打开同一批次或选择新批次。")

    def open_folder(self) -> None:
        if self._busy or not self._confirm_discard():
            return
        start = str(self.settings.value("last_batch_dir", ""))
        folder = QFileDialog.getExistingDirectory(self, "选择 P4M/M3M 批次文件夹", start)
        if not folder:
            return
        try:
            catalog = scan_folder(folder, self.sensor_combo.currentText())
        except Exception as exc:
            QMessageBox.critical(self, "无法打开批次", str(exc))
            return
        self.settings.setValue("last_batch_dir", folder)
        self._clear_task_state()
        self.catalog = catalog
        self.project_dir = self.catalog.root
        self._populate_catalog()

    def _populate_catalog(self) -> None:
        assert self.catalog is not None
        self._generation += 1
        self._retire_workers()
        generation = self._generation
        self.capture_list.clear()
        rgb_captures = [capture for capture in self.catalog.captures if "RGB" in capture.files]
        for capture in rgb_captures:
            item = QListWidgetItem(QIcon(), capture.files["RGB"].name)
            item.setData(Qt.UserRole, capture.key)
            available = ", ".join(capture.files)
            item.setToolTip(available)
            self.capture_list.addItem(item)
        complete = len(self.catalog.complete_captures)
        self.sensor_info.setText(
            f"识别：{self.catalog.sensor}（{self.catalog.confidence}）\n"
            f"曝光组：{len(self.catalog.captures)}；完整：{complete}\n"
            + "\n".join(self.catalog.reasons)
        )
        self.sensor_combo.setCurrentText(self.catalog.sensor)
        if self.capture_list.count():
            self.capture_list.setCurrentRow(0)
        self.thumbnail_thread = ThumbnailThread(rgb_captures)
        self.thumbnail_thread.ready.connect(
            lambda key, image, gen=generation: self.set_thumbnail(key, image) if gen == self._generation else None
        )
        self.thumbnail_thread.start()
        self.refresh_roi_table()
        if not rgb_captures:
            self.statusBar().showMessage("该批次没有可显示的 RGB 照片；左侧按要求不显示单波段影像。")
            return
        self.statusBar().showMessage("批次已打开。需要自动推荐时再点击“自动查找定标布”。")

    def set_thumbnail(self, key: str, image: QImage) -> None:
        item = self._item_for_capture(key)
        if item:
            item.setIcon(QIcon(QPixmap.fromImage(image)))

    def _item_for_capture(self, key: str) -> QListWidgetItem | None:
        for row in range(self.capture_list.count()):
            item = self.capture_list.item(row)
            if item.data(Qt.UserRole) == key:
                return item
        return None

    def capture_changed(self, current: QListWidgetItem | None, previous=None) -> None:
        if not current or not self.catalog:
            return
        key = current.data(Qt.UserRole)
        self.current_capture = next((capture for capture in self.catalog.captures if capture.key == key), None)
        if not self.current_capture or "RGB" not in self.current_capture.files:
            return
        try:
            self.current_band = "RGB"
            self.canvas.set_photo(self.current_capture.files["RGB"])
            self.canvas.show_rois(self.annotations)
        except Exception as exc:
            QMessageBox.warning(self, "影像读取失败", str(exc))

    def import_image(self) -> None:
        if self._busy:
            return
        start = str(self.settings.value("last_import_dir", self.settings.value("last_batch_dir", "")))
        filename, _ = QFileDialog.getOpenFileName(self, "导入定标布 RGB 影像", start, "RGB 影像 (*.jpg *.jpeg *.dng)")
        if not filename:
            return
        self.settings.setValue("last_import_dir", str(Path(filename).parent))
        metadata = read_image_metadata(filename)
        if metadata.sensor:
            if self.catalog and metadata.sensor != self.catalog.sensor:
                answer = QMessageBox.warning(
                    self,
                    "传感器型号不一致",
                    f"照片元数据识别为 {metadata.sensor}，当前项目为 {self.catalog.sensor}。\n"
                    "仍然导入可能导致错误定标。是否继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
            else:
                self.statusBar().showMessage(
                    f"导入照片元数据识别为 {metadata.sensor}；不需要航线信息文件。"
                )
        if metadata.band_name and metadata.band_name.upper() not in {"RGB", "VISIBLE"}:
            QMessageBox.warning(self, "请选择 RGB", f"这张照片的元数据波段为 {metadata.band_name}。左侧和工作区只接受 RGB 照片。")
            return
        if self.catalog is None:
            sensor = metadata.sensor or self.sensor_combo.currentText()
            if sensor == "AUTO":
                sensor = "M3M"
            try:
                self.catalog = scan_folder(Path(filename).parent, sensor)
            except Exception:
                self.catalog = Catalog(Path(filename).parent, sensor, "人工导入", ["单张 RGB 影像由用户导入"], [], [])
            self.project_dir = Path(filename).parent
        existing = None
        if metadata.capture_uuid:
            existing = next(
                (capture for capture in self.catalog.captures if capture.capture_uuid == metadata.capture_uuid),
                None,
            )
        if existing:
            existing.files["RGB"] = Path(filename)
            key = existing.key
        else:
            # Prefer the complete capture discovered in the imported image's
            # parent folder, so its multispectral companions remain available
            # for deferred registration.
            discovered = next(
                (capture for capture in self.catalog.captures if capture.files.get("RGB") == Path(filename).resolve()),
                None,
            )
            if discovered:
                key = discovered.key
            else:
                key = f"imported-{len(self.catalog.captures) + 1:04d}"
                self.catalog.captures.append(Capture(key, {"RGB": Path(filename)}, capture_uuid=metadata.capture_uuid))
        self._populate_catalog()
        item = self._item_for_capture(key)
        if item:
            self.capture_list.setCurrentItem(item)

    def auto_find(self) -> None:
        if not self.catalog:
            QMessageBox.information(self, "尚未打开批次", "请先打开一个批次文件夹。")
            return
        if self.candidate_thread and self.candidate_thread.isRunning():
            return
        paths = self.catalog.preview_images
        self.statusBar().showMessage(f"正在快速检查飞行开头和结尾的最多 160 张预览图……")
        self.candidate_thread = CandidateThread(paths)
        generation = self._generation
        self.candidate_thread.completed.connect(
            lambda result, gen=generation: self.show_candidates(result) if gen == self._generation else None
        )
        self.candidate_thread.failed.connect(
            lambda message, gen=generation: QMessageBox.warning(self, "自动查找失败", message) if gen == self._generation else None
        )
        self.candidate_thread.start()

    def show_candidates(self, candidates: list[Candidate]) -> None:
        self.statusBar().showMessage(f"自动查找完成，得到 {len(candidates)} 个候选。")
        if not candidates:
            QMessageBox.information(self, "没有候选", "没有发现明显的规则定标布，请使用缩略图或手动导入。")
            return
        labels = [f"{item.path.name}    评分 {item.score:.2f}" for item in candidates]
        choice, ok = QInputDialog.getItem(self, "定标布候选（需要人工确认）", "选择要打开的影像：", labels, 0, False)
        if not ok:
            return
        candidate = candidates[labels.index(choice)]
        capture = next((c for c in self.catalog.captures if candidate.path in c.files.values()), None)
        if capture:
            item = self._item_for_capture(capture.key)
            if item:
                self.capture_list.setCurrentItem(item)
        QMessageBox.information(self, "候选已打开", "自动结果只是 RGB 候选。请在中间工作区人工确认后绘制 ROI。")

    def add_roi(self, polygon: list[list[float]]) -> None:
        if not self.current_capture or "RGB" not in self.current_capture.files or not self.catalog:
            QMessageBox.information(self, "没有 RGB", "请先在左侧选择一张 RGB 定标布照片。")
            return
        dialog = RoiDialog(self.catalog.expected_bands, self)
        if dialog.exec() != QDialog.Accepted:
            return
        path = self.current_capture.files["RGB"]
        next_id = max((int(item.roi_id.split('-')[-1]) for item in self.annotations), default=0) + 1
        roi_id = f"ROI-{next_id:03d}"
        annotation = RgbRoiAnnotation(
            roi_id=roi_id,
            capture_key=self.current_capture.key,
            image_path=str(path),
            panel_id=dialog.panel_id.currentText().strip() or "Panel-01",
            polygon=polygon,
            reflectance_by_band=dialog.reflectance_by_band,
        )
        self.annotations.append(annotation)
        self.refresh_roi_table()
        self.canvas.show_rois(self.annotations, roi_id)
        self.statusBar().showMessage(f"已添加 {roi_id}。波段配准和 DN 统计将在计算系数时执行。")

    def refresh_roi_table(self) -> None:
        self.roi_table.blockSignals(True)
        self.roi_table.setRowCount(len(self.annotations))
        for row, sample in enumerate(self.annotations):
            values = [
                sample.roi_id,
                sample.panel_id,
                "; ".join(f"{band}={value:.4f}" for band, value in sample.reflectance_by_band.items()),
                "是" if sample.enabled else "否",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, sample.roi_id)
                if not sample.enabled:
                    item.setForeground(QColor("#888888"))
                self.roi_table.setItem(row, column, item)
        self.roi_table.resizeColumnsToContents()
        self.roi_table.blockSignals(False)

    def _selected_sample(self) -> RgbRoiAnnotation | None:
        row = self.roi_table.currentRow()
        return self.annotations[row] if 0 <= row < len(self.annotations) else None

    def roi_selected(self) -> None:
        sample = self._selected_sample()
        if not sample or not self.catalog:
            return
        capture = next((c for c in self.catalog.captures if c.key == sample.capture_key), None)
        if capture:
            item = self._item_for_capture(capture.key)
            if item:
                self.capture_list.setCurrentItem(item)
                self.canvas.show_rois(self.annotations, sample.roi_id)

    def toggle_roi(self, row: int, column: int) -> None:
        if 0 <= row < len(self.annotations):
            self.annotations[row].enabled = not self.annotations[row].enabled
            self.refresh_roi_table()

    def delete_roi(self) -> None:
        row = self.roi_table.currentRow()
        if 0 <= row < len(self.annotations):
            del self.annotations[row]
            self.refresh_roi_table()
            self.canvas.show_rois(self.annotations)

    def calculate(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.main_toolbar.setEnabled(False)
        self.centralWidget().setEnabled(False)
        try:
            self._calculate()
        except Exception as exc:
            QMessageBox.critical(self, "计算或保存失败", f"{exc}\n当前标注仍保留在窗口中。")
        finally:
            self._busy = False
            self.main_toolbar.setEnabled(True)
            self.centralWidget().setEnabled(True)

    def _calculate(self) -> None:
        if not self.catalog or not self.annotations:
            QMessageBox.information(self, "数据不足", "请先在 RGB 照片上添加至少一个 ROI。")
            return
        self.project_dir = self.catalog.root
        output_path = self.project_dir / COEFFICIENTS_FILENAME
        if output_path.exists() and QMessageBox.question(
            self, "覆盖已有系数？", f"计算成功后将替换：\n{output_path}\n是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        generated: list[RoiSample] = []
        registration_cache = {}
        review_records: list[str] = []
        enabled_annotations = [item for item in self.annotations if item.enabled]
        for annotation_index, annotation in enumerate(enabled_annotations, 1):
            capture = next((item for item in self.catalog.captures if item.key == annotation.capture_key), None)
            if capture is None:
                QMessageBox.warning(self, "曝光组缺失", f"找不到 {annotation.roi_id} 对应的曝光组。")
                return
            missing = [band for band in self.catalog.expected_bands if band not in capture.files]
            if missing:
                QMessageBox.warning(
                    self,
                    "波段不完整",
                    f"{annotation.roi_id} 对应曝光组缺少：{', '.join(missing)}。\n请改用包含完整波段的 RGB 照片。",
                )
                return
            for band_index, band in enumerate(self.catalog.expected_bands, 1):
                self.statusBar().showMessage(
                    f"正在配准 {annotation.roi_id} → {band}（{annotation_index}/{len(enabled_annotations)}）……"
                )
                QApplication.processEvents()
                cache_key = (capture.key, band)
                if cache_key not in registration_cache:
                    registration_cache[cache_key] = register_rgb_to_band(capture.files["RGB"], capture.files[band])
                registration = registration_cache[cache_key]
                if (
                    registration.method.endswith("Fallback")
                    or (registration.method == "ORB-Homography" and registration.score < 0.35)
                    or (registration.method == "ECC-Affine" and registration.score < 0.5)
                ):
                    review_records.append(
                        f"{annotation.roi_id}/{band} ({registration.method}, {registration.score:.3f})"
                    )
                mapped_polygon = transform_polygon(annotation.polygon, registration.matrix)
                try:
                    stats = roi_statistics(capture.files[band], mapped_polygon)
                except Exception as exc:
                    QMessageBox.warning(
                        self,
                        "配准后的 ROI 无效",
                        f"{annotation.roi_id} → {band} 无法统计：{exc}\n"
                        f"配准方法：{registration.method}，质量：{registration.score:.3f}",
                    )
                    return
                generated.append(
                    RoiSample(
                        roi_id=f"{annotation.roi_id}-{band}",
                        capture_key=capture.key,
                        image_path=str(capture.files[band]),
                        band=band,
                        panel_id=annotation.panel_id,
                        polygon=mapped_polygon,
                        reflectance=annotation.reflectance_by_band[band],
                        source_rgb_roi_id=annotation.roi_id,
                        source_rgb_path=annotation.image_path,
                        source_rgb_polygon=annotation.polygon,
                        registration_method=registration.method,
                        registration_score=registration.score,
                        **stats,
                    )
                )

        self.samples = generated
        self.models = fit_models(self.samples)
        if not self.models:
            QMessageBox.warning(self, "无法拟合", "配准后没有可用于拟合的有效波段 ROI。")
            return
        output = save_coefficients(
            self.project_dir,
            self.catalog.root,
            self.catalog.sensor,
            self.samples,
            self.models,
            rgb_annotations=self.annotations,
        )
        lines = []
        for band, model in self.models.items():
            r2 = "N/A" if model.r_squared is None else f"{model.r_squared:.4f}"
            lines.append(f"{band}: ρ={model.slope:.9g}×DN{model.intercept:+.9g}；n={model.sample_count}；R²={r2}")
        methods = sorted({sample.registration_method or "unknown" for sample in self.samples})
        lines.append(f"配准：{len(registration_cache)} 个影像对；方法 {', '.join(methods)}")
        self.model_label.setText("\n".join(lines))
        fallback_note = ""
        if review_records:
            fallback_note = "\n\n注意：以下配准质量较低或使用了后备方法，请重点复核：\n" + ", ".join(review_records)
        QMessageBox.information(
            self,
            "定标系数已保存",
            f"已完成 RGB→多光谱配准并保存：\n{output}\n\n文件名固定为：\n{COEFFICIENTS_FILENAME}{fallback_note}",
        )

    def load_project(self) -> None:
        if self._busy or not self._confirm_discard():
            return
        start = str(self.settings.value("last_project_dir", ""))
        filename, _ = QFileDialog.getOpenFileName(self, "载入定标系数项目", start, f"定标系数 ({COEFFICIENTS_FILENAME});;JSON (*.json)")
        if not filename:
            return
        try:
            payload = load_coefficients(filename)
            catalog = scan_folder(payload["input_directory"], payload["sensor"])
            self.catalog = catalog
            self.samples = [RoiSample.from_dict(item) for item in payload.get("roi_samples", [])]
            self.annotations = [RgbRoiAnnotation.from_dict(item) for item in payload.get("rgb_annotations", [])]
            self.models = {band: BandModel(**value) for band, value in payload.get("models", {}).items()}
            self.project_dir = Path(filename).parent
            self.settings.setValue("last_project_dir", str(self.project_dir))
            self._populate_catalog()
            self.model_label.setText("已载入：\n" + "\n".join(f"{b}: ρ={m.slope:.9g}×DN{m.intercept:+.9g}" for b, m in self.models.items()))
        except Exception as exc:
            QMessageBox.critical(self, "载入失败", str(exc))

    def apply_to_orthophoto(self) -> None:
        if not self.models:
            QMessageBox.information(self, "尚无系数", "请先计算系数或载入 radiometric_calibration_coefficients.json。")
            return
        start = str(self.settings.value("last_ortho_dir", str(self.project_dir or "")))
        source, _ = QFileDialog.getOpenFileName(self, "选择 WebODM DN 正射 GeoTIFF", start, "GeoTIFF (*.tif *.tiff)")
        if not source:
            return
        self.settings.setValue("last_ortho_dir", str(Path(source).parent))
        try:
            descriptions = describe_bands(source)
        except Exception as exc:
            QMessageBox.critical(self, "GeoTIFF 读取失败", str(exc))
            return
        options = [f"{index}: {description}" for index, description in enumerate(descriptions, 1)]
        band_map: dict[str, int] = {}
        for band in self.models:
            default = next((i for i, description in enumerate(descriptions) if band.lower() == description.lower().replace(" ", "")), 0)
            choice, ok = QInputDialog.getItem(self, "映射 GeoTIFF 波段", f"请选择 {band} 对应的输入波段：", options, default, False)
            if not ok:
                return
            band_map[band] = int(choice.split(":", 1)[0])
        output, _ = QFileDialog.getSaveFileName(
            self,
            "保存 Float32 反射率正射影像",
            str((self.project_dir or Path(source).parent) / "reflectance_orthophoto.tif"),
            "GeoTIFF (*.tif)",
        )
        if not output:
            return
        clip = QMessageBox.question(self, "反射率范围", "是否将输出裁剪到 0～1？\n选择“否”可以保留超范围值用于质量检查。") == QMessageBox.Yes
        try:
            apply_models(source, output, {band: model.to_dict() for band, model in self.models.items()}, band_map, clip)
        except Exception as exc:
            QMessageBox.critical(self, "转换失败", str(exc))
            return
        QMessageBox.information(self, "处理完成", f"已生成 Float32 反射率 GeoTIFF：\n{output}")


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("DJI Radiometric Calibration Tool")
    window = MainWindow()
    window.show()
    return app.exec()


def diagnose(output: Path) -> int:
    """Construct the real GUI and import geospatial dependencies for packaged smoke tests."""
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    from .geotiff import describe_bands as _describe_bands

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "status": "ok",
                "window_title": window.windowTitle(),
                "qt_version": __import__("PySide6").__version__,
                "rasterio_import": callable(_describe_bands),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    window.close()
    app.processEvents()
    return 0
