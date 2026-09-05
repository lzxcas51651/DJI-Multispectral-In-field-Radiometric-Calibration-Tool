from __future__ import annotations

import json
import sys
from copy import deepcopy
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
    QLabel,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressDialog,
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
from .processing import calculate_coefficients
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
    progress = Signal(int, int)

    def __init__(self, paths: list[Path]):
        super().__init__()
        self.paths = paths

    def run(self) -> None:
        try:
            result = find_candidates(self.paths, progress=self.progress.emit)
            if not self.isInterruptionRequested():
                self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class OperationThread(QThread):
    progress = Signal(int, str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.operation(self.progress.emit)
        except Exception as exc:
            self.error = exc


class BandMappingDialog(QDialog):
    def __init__(self, bands, descriptions, parent=None, sensor=None):
        super().__init__(parent)
        self.setWindowTitle("设置 DN 正射影像波段对应关系")
        self.setMinimumWidth(520)
        layout = QFormLayout(self)
        orders = {'P4M': ('Blue', 'Green', 'Red', 'RedEdge', 'NIR'),
                  'M3M': ('Green', 'Red', 'RedEdge', 'NIR')}
        self.fixed_order = orders.get(sensor)
        self.mapping_error = ''
        if self.fixed_order:
            self.ignored_alpha = (len(descriptions) == len(self.fixed_order) + 1
                                  and 'alpha' in descriptions[-1].lower())
            if self.ignored_alpha:
                layout.addRow(QLabel(f"已忽略末尾第 {len(descriptions)} 波段 Alpha（透明度），不参与定标或输出。"))
                descriptions = descriptions[:-1]
            if len(descriptions) != len(self.fixed_order) or set(bands) != set(self.fixed_order):
                self.mapping_error = f'{sensor} 需要 {len(self.fixed_order)} 个光谱波段及完整系数；可附带末尾 Alpha。请检查文件。'
            layout.addRow(QLabel(f"{sensor} 固定波段对应关系（不重排）：输入波段 i → 输出波段 i。"))
            bands = self.fixed_order
        else:
            layout.addRow(QLabel("每个定标波段必须对应一个不同的输入波段。输出按输入编号排序。"))
        self.combos = {}
        for band in bands:
            combo = QComboBox()
            combo.addItem("请选择输入波段", None)
            for index, description in enumerate(descriptions, 1):
                name = self.fixed_order[index-1] if self.fixed_order and index <= len(self.fixed_order) else description
                combo.addItem(f"输入 {index}: {name}（文件描述：{description}）", index)
            match = next((i for i, description in enumerate(descriptions, 1)
                          if band.lower() == description.lower().replace(' ', '')), 0)
            combo.setCurrentIndex(match)
            if self.fixed_order:
                index = self.fixed_order.index(band) + 1
                combo.setCurrentIndex(index if index <= len(descriptions) else 0)
                combo.setEnabled(False)
            self.combos[band] = combo
            combo.currentIndexChanged.connect(self._validate)
            layout.addRow(f"输出 {self.fixed_order.index(band)+1}: {band}" if self.fixed_order else band, combo)
        self.clip = QCheckBox("将反射率裁剪到 0～1（不勾选则保留超范围值供检查）")
        layout.addRow(self.clip)
        self.validation_label = QLabel()
        layout.addRow(self.validation_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addRow(self.buttons)
        self._validate()

    @property
    def band_map(self):
        return {band: combo.currentData() for band, combo in self.combos.items()}

    def _validate(self, *_args):
        if not hasattr(self, 'buttons'):
            return False
        values = list(self.band_map.values())
        selected = [value for value in values if value is not None]
        duplicates = {value for value in selected if selected.count(value) > 1}
        for combo in self.combos.values():
            combo.setStyleSheet('border: 1px solid #d32f2f;' if combo.currentData() in duplicates else '')
        message = ('输入波段重复，请为红框中的波段选择不同的输入。' if duplicates
                   else '请完成所有波段选择。' if None in values else '波段对应关系检查通过。')
        valid = bool(values) and None not in values and not duplicates
        if self.mapping_error:
            valid = False
            message = self.mapping_error
        if valid:
            order = sorted(self.band_map, key=self.band_map.get)
            message = '输出顺序：' + ' → '.join(f'{band}（输入 {self.band_map[band]}）' for band in order)
        self.validation_label.setText(message)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(valid)
        return valid

    def accept(self):
        if self._validate():
            super().accept()


class RoiDialog(QDialog):
    def __init__(self, bands: tuple[str, ...], parent=None, settings: QSettings | None = None):
        super().__init__(parent)
        self.settings = settings if settings is not None else QSettings("DJI-Local-Tools", "Radiometric-Calibrator")
        self.preset_key = "panel_presets/" + "_".join(bands)
        try:
            self.presets = json.loads(self.settings.value(self.preset_key, "{}"))
            if not isinstance(self.presets, dict):
                self.presets = {}
        except (ValueError, TypeError):
            self.presets = {}
        self.setWindowTitle("新增 RGB 定标布 ROI")
        self.setMinimumWidth(420)
        layout = QFormLayout(self)
        self.panel_id = QComboBox()
        self.panel_id.setEditable(True)
        self.panel_id.addItems(list(dict.fromkeys([f"Panel-{i:02d}" for i in range(1, 11)] + list(self.presets))))
        layout.addRow("定标布编号", self.panel_id)
        layout.addRow(QLabel("反射率用小数填写，例如 50% 填 0.5。确认 ROI 时记忆预设。"))
        self.uniform = QCheckBox("所有波段使用同一反射率")
        self.uniform.setChecked(True)
        layout.addRow(self.uniform)
        self.uniform_value = QDoubleSpinBox()
        self.uniform_value.setRange(0.0, 1.0)
        self.uniform_value.setDecimals(6)
        self.uniform_value.setSingleStep(0.01)
        self.uniform_value.setValue(0.5)
        layout.addRow("统一反射率", self.uniform_value)
        self.reflectance_inputs: dict[str, QDoubleSpinBox] = {}
        for band in bands:
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1.0)
            spin.setDecimals(6)
            spin.setSingleStep(0.01)
            spin.setValue(0.5)
            self.reflectance_inputs[band] = spin
            layout.addRow(f"{band} 反射率（0～1）", spin)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.uniform.toggled.connect(self._update_uniform)
        self.uniform_value.valueChanged.connect(self._update_uniform)
        self.panel_id.currentTextChanged.connect(self._load_panel)
        self.panel_id.setCurrentText(self.settings.value(self.preset_key + "_last", "Panel-01"))
        self._load_panel(self.panel_id.currentText())

    def _update_uniform(self, *_args) -> None:
        uniform = self.uniform.isChecked()
        self.uniform_value.setEnabled(uniform)
        for spin in self.reflectance_inputs.values():
            spin.setEnabled(not uniform)
            if uniform:
                spin.setValue(self.uniform_value.value())

    def _load_panel(self, name: str) -> None:
        preset = self.presets.get(name.strip(), {})
        if not isinstance(preset, dict):
            preset = {}
        self.uniform.blockSignals(True)
        self.uniform_value.blockSignals(True)
        self.uniform.setChecked(bool(preset.get("uniform", True)))
        def number(value):
            try:
                value = float(value)
                return value if 0 <= value <= 1 else 0.5
            except (ValueError, TypeError):
                return 0.5
        self.uniform_value.setValue(number(preset.get("value", 0.5)))
        values = preset.get("bands", {})
        if not isinstance(values, dict):
            values = {}
        for band, spin in self.reflectance_inputs.items():
            spin.setValue(number(values.get(band, 0.5)))
        self.uniform.blockSignals(False)
        self.uniform_value.blockSignals(False)
        self._update_uniform()

    def accept(self) -> None:
        name = self.panel_id.currentText().strip()
        if not name:
            QMessageBox.warning(self, "需要编号", "请选择或输入定标布编号。")
            return
        self.presets[name] = {"uniform": self.uniform.isChecked(),
                              "value": self.uniform_value.value(), "bands": self.reflectance_by_band}
        self.settings.setValue(self.preset_key, json.dumps(self.presets, ensure_ascii=False))
        self.settings.setValue(self.preset_key + "_last", name)
        self.settings.sync()
        super().accept()

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
        self.candidate_keys: set[str] = set()
        self.candidate_scores: dict[str, float] = {}
        self.candidate_panel_counts: dict[str, int] = {}
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
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("影像显示"))
        self.image_filter = QComboBox()
        self.image_filter.addItem("全部 RGB 影像", "all")
        self.image_filter.addItem("自动查找的定标布候选", "candidates")
        self.image_filter.currentIndexChanged.connect(self._filter_changed)
        filter_row.addWidget(self.image_filter, 1)
        left_layout.addLayout(filter_row)
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
        self.candidate_keys.clear()
        self.candidate_scores.clear()
        self.candidate_panel_counts.clear()
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
        self.image_filter.blockSignals(True)
        self.image_filter.setCurrentIndex(0)
        self.image_filter.blockSignals(False)

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
        all_rgb = [capture for capture in self.catalog.captures if "RGB" in capture.files]
        candidates_only = self.image_filter.currentData() == "candidates"
        rgb_captures = ([capture for capture in all_rgb if capture.key in self.candidate_keys]
                        if candidates_only else all_rgb)
        for capture in rgb_captures:
            item = QListWidgetItem(QIcon(), capture.files["RGB"].name)
            item.setData(Qt.UserRole, capture.key)
            score = self.candidate_scores.get(capture.key)
            available = ", ".join(capture.files) + (
                f"\n疑似定标布：{self.candidate_panel_counts.get(capture.key, 0)} 块；评分：{score:.2f}"
                if score is not None else "")
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
            self.current_capture = None
            self.canvas.clear_photo(clear_cache=False)
            if candidates_only:
                self.statusBar().showMessage("当前没有定标布候选。点击“自动查找定标布”，或切换为“全部 RGB 影像”。")
                return
            self.statusBar().showMessage("该批次没有可显示的 RGB 照片；左侧按要求不显示单波段影像。")
            return
        mode = "定标布候选" if candidates_only else "全部 RGB 影像"
        self.statusBar().showMessage(f"左侧正在显示 {len(rgb_captures)} 张{mode}。")

    def _filter_changed(self) -> None:
        if self.catalog:
            self._populate_catalog()

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
        self.statusBar().showMessage(f"正在检查全部 {len(paths)} 张 RGB 预览图……")
        self.candidate_thread = CandidateThread(paths)
        generation = self._generation
        self.candidate_thread.completed.connect(
            lambda result, gen=generation: self.show_candidates(result) if gen == self._generation else None
        )
        self.candidate_thread.progress.connect(
            lambda done, total, gen=generation: self.statusBar().showMessage(
                f"正在检查全部 RGB 影像：{done}/{total}（{done * 100 // max(total, 1)}%）"
            ) if gen == self._generation else None
        )
        self.candidate_thread.failed.connect(
            lambda message, gen=generation: QMessageBox.warning(self, "自动查找失败", message) if gen == self._generation else None
        )
        self.candidate_thread.start()

    def show_candidates(self, candidates: list[Candidate]) -> None:
        self.statusBar().showMessage(f"自动查找完成，得到 {len(candidates)} 个候选。")
        if not candidates:
            self.candidate_keys.clear()
            self.candidate_scores.clear()
            self.candidate_panel_counts.clear()
            QMessageBox.information(self, "没有候选", "没有发现明显的规则定标布，请使用缩略图或手动导入。")
            return
        self.candidate_keys.clear()
        self.candidate_scores.clear()
        candidate_paths = {item.path.resolve(): item for item in candidates}
        for capture in self.catalog.captures:
            rgb = capture.files.get("RGB")
            if rgb and rgb.resolve() in candidate_paths:
                self.candidate_keys.add(capture.key)
                candidate = candidate_paths[rgb.resolve()]
                self.candidate_scores[capture.key] = candidate.score
                self.candidate_panel_counts[capture.key] = candidate.panel_count
        self.image_filter.blockSignals(True)
        self.image_filter.setCurrentIndex(self.image_filter.findData("candidates"))
        self.image_filter.blockSignals(False)
        self._populate_catalog()
        QMessageBox.information(
            self, "候选筛选完成",
            f"左侧已切换为 {len(self.candidate_keys)} 张定标布候选。自动结果需要人工确认；可用“影像显示”切回全部图片。",
        )

    def add_roi(self, polygon: list[list[float]]) -> None:
        if not self.current_capture or "RGB" not in self.current_capture.files or not self.catalog:
            QMessageBox.information(self, "没有 RGB", "请先在左侧选择一张 RGB 定标布照片。")
            return
        dialog = RoiDialog(self.catalog.expected_bands, self, settings=self.settings)
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

    def _start_operation(self, title, operation, completed) -> None:
        if self._busy:
            return
        self._busy = True
        self.main_toolbar.setEnabled(False)
        self.centralWidget().setEnabled(False)
        self.progress_dialog = QProgressDialog(title, "", 0, 100, self)
        self.progress_dialog.setCancelButton(None)
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setAutoReset(False)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.setWindowTitle(title)
        self.progress_dialog.setValue(0)
        self.progress_dialog.show()
        self.operation_thread = OperationThread(operation, self)
        self._operation_completed = completed
        self.operation_thread.progress.connect(self._operation_progress)
        self.operation_thread.finished.connect(self._operation_finished)
        self.operation_thread.start()

    def _operation_progress(self, value, message):
        self.progress_dialog.setLabelText(message)
        self.progress_dialog.setValue(value)
        self.statusBar().showMessage(message)

    def _operation_finished(self):
        worker = self.operation_thread
        self.progress_dialog.close()
        self.progress_dialog.deleteLater()
        self._busy = False
        self.main_toolbar.setEnabled(True)
        self.centralWidget().setEnabled(True)
        self.operation_thread = None
        worker.deleteLater()
        if worker.error is not None:
            self.statusBar().showMessage("处理失败")
            QMessageBox.critical(self, "处理失败", str(worker.error) + "\n当前标注和已有系数仍保留。")
        else:
            self.statusBar().showMessage("处理完成")
            self._operation_completed(worker.result)

    def calculate(self) -> None:
        self._calculate()

    def _calculate(self) -> None:
        if self._busy:
            return
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
        catalog, annotations = deepcopy(self.catalog), deepcopy(self.annotations)
        self._start_operation("计算并保存系数",
                              lambda progress: calculate_coefficients(catalog, annotations, progress),
                              self._calculation_completed)

    def _calculation_completed(self, result):
        self.samples, self.models, output, review = result
        lines = []
        for band, model in self._ordered_models():
            r2 = "N/A" if model.r_squared is None else f"{model.r_squared:.4f}"
            lines.append(f"{band}: ρ={model.slope:.9g}×DN{model.intercept:+.9g}；n={model.sample_count}；R²={r2}")
        self.model_label.setText("\n".join(lines))
        note = "\n\n请复核低质量配准：\n" + ", ".join(review) if review else ""
        QMessageBox.information(self, "定标系数已保存", f"已保存：\n{output}{note}")

    def _ordered_models(self):
        order = BANDS.get(self.catalog.sensor if self.catalog else '',
                          ('Blue', 'Green', 'Red', 'RedEdge', 'NIR'))
        names = [band for band in order if band in self.models]
        names += [band for band in self.models if band not in names]
        return [(band, self.models[band]) for band in names]

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
            self.model_label.setText("已载入：\n" + "\n".join(f"{b}: ρ={m.slope:.9g}×DN{m.intercept:+.9g}" for b, m in self._ordered_models()))
        except Exception as exc:
            QMessageBox.critical(self, "载入失败", str(exc))

    def apply_to_orthophoto(self) -> None:
        if self._busy:
            return
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
        mapping = BandMappingDialog(self.models, descriptions, self,
                                    sensor=self.catalog.sensor if self.catalog else None)
        if mapping.exec() != QDialog.Accepted:
            return
        band_map = mapping.band_map
        output, _ = QFileDialog.getSaveFileName(
            self,
            "保存 Float32 反射率正射影像",
            str(Path(source).with_name(f"{Path(source).stem}_ref{Path(source).suffix}")),
            "GeoTIFF (*.tif)",
        )
        if not output:
            return
        models = {band: model.to_dict() for band, model in self.models.items()}
        clip = mapping.clip.isChecked()
        self._start_operation("应用到 DN 正射影像",
                              lambda progress: apply_models(source, output, models, band_map, clip, progress),
                              self._orthophoto_completed)

    def _orthophoto_completed(self, output):
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
