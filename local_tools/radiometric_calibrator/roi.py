from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class RgbRoiAnnotation:
    roi_id: str
    capture_key: str
    image_path: str
    panel_id: str
    polygon: list[list[float]]
    reflectance_by_band: dict[str, float]
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "RgbRoiAnnotation":
        return cls(**value)


@dataclass
class RoiSample:
    roi_id: str
    capture_key: str
    image_path: str
    band: str
    panel_id: str
    polygon: list[list[float]]
    reflectance: float
    pixel_count: int
    mean: float
    median: float
    trimmed_mean: float
    stddev: float
    cv_percent: float
    minimum: float
    maximum: float
    enabled: bool = True
    source_rgb_roi_id: str | None = None
    source_rgb_path: str | None = None
    source_rgb_polygon: list[list[float]] | None = None
    registration_method: str | None = None
    registration_score: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "RoiSample":
        return cls(**value)


def read_unchanged(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"无法读取影像：{path}")
    return image


def roi_statistics(path: str | Path, polygon: list[list[float]]) -> dict[str, float | int]:
    image = read_unchanged(path)
    if image.ndim == 3:
        if image.shape[2] == 1:
            image = image[:, :, 0]
        else:
            raise ValueError("反射率 ROI 必须绘制在单波段 TIFF 上，RGB 仅用于查找定标布。")
    points = np.rint(np.asarray(polygon, dtype=np.float64)).astype(np.int32)
    if points.shape[0] < 3:
        raise ValueError("ROI 至少需要三个顶点。")
    mask = np.zeros(image.shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [points], 255)
    # Erode the edge to avoid mixed panel/background pixels when the ROI is large enough.
    if cv2.countNonZero(mask) >= 400:
        mask = cv2.erode(mask, np.ones((5, 5), np.uint8), iterations=1)
    values = image[mask != 0].astype(np.float64)
    values = values[np.isfinite(values)]
    if values.size < 25:
        raise ValueError("ROI 有效像素少于 25，请重新勾选更大的定标布内部区域。")
    values.sort()
    trim = int(values.size * 0.02)
    trimmed = values[trim : values.size - trim] if trim and values.size > trim * 2 else values
    mean = float(values.mean())
    stddev = float(values.std(ddof=1)) if values.size > 1 else 0.0
    return {
        "pixel_count": int(values.size),
        "mean": mean,
        "median": float(np.median(values)),
        "trimmed_mean": float(trimmed.mean()),
        "stddev": stddev,
        "cv_percent": float(stddev / mean * 100.0) if mean else float("inf"),
        "minimum": float(values[0]),
        "maximum": float(values[-1]),
    }
