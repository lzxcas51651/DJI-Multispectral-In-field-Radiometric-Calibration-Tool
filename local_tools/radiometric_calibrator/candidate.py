from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class Candidate:
    path: Path
    score: float
    rectangle: tuple[int, int, int, int] | None


def _decode_small(path: Path, maximum: int = 480) -> tuple[np.ndarray, float]:
    data = np.fromfile(str(path), dtype=np.uint8)
    reduced = path.suffix.lower() in {".jpg", ".jpeg"}
    image = cv2.imdecode(data, cv2.IMREAD_REDUCED_COLOR_4 if reduced else cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(str(path))
    height, width = image.shape[:2]
    resize_scale = min(1.0, maximum / max(height, width))
    if resize_scale < 1:
        image = cv2.resize(image, None, fx=resize_scale, fy=resize_scale, interpolation=cv2.INTER_AREA)
    decoder_scale = 0.25 if reduced else 1.0
    return image, decoder_scale * resize_scale


def _score(path: Path) -> Candidate:
    try:
        image, scale = _decode_small(path)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(gray, 45, 130)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        # Calibration cloth is often gray or white. A low-saturation mask finds
        # large cloth panels even when their internal grid breaks Canny edges.
        neutral = cv2.inRange(hsv, np.asarray((0, 0, 80)), np.asarray((179, 75, 255)))
        kernel = np.ones((7, 7), np.uint8)
        neutral = cv2.morphologyEx(neutral, cv2.MORPH_CLOSE, kernel, iterations=2)
        contour_sets = []
        for mask in (edges, neutral):
            contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            contour_sets.extend(contours)
        area_image = float(gray.shape[0] * gray.shape[1])
        best_score, best_box = 0.0, None
        for contour in contour_sets:
            area = cv2.contourArea(contour)
            fraction = area / area_image
            # At normal mapping altitude a 1-2 m panel can occupy well below
            # 0.1% of a 20 MP M3M RGB frame.
            if fraction < 0.00025 or fraction > 0.75:
                continue
            rectangle = cv2.minAreaRect(contour)
            rw, rh = rectangle[1]
            if rw < 3 or rh < 3:
                continue
            aspect = max(rw, rh) / max(min(rw, rh), 1.0)
            if aspect > 6.0:
                continue
            polygon = cv2.boxPoints(rectangle).astype(np.int32)
            x, y, w, h = cv2.boundingRect(polygon)
            rectangularity = area / max(float(rw * rh), 1.0)
            if rectangularity < 0.55:
                continue
            mask = np.zeros_like(gray)
            cv2.fillPoly(mask, [polygon], 255)
            values = gray[mask != 0]
            uniformity = max(0.0, 1.0 - float(values.std()) / 90.0)
            brightness = min(1.0, float(values.mean()) / 180.0)
            size_score = min(1.0, fraction / 0.02)
            score = 0.38 * rectangularity + 0.34 * uniformity + 0.18 * size_score + 0.10 * brightness
            if score > best_score:
                best_score = score
                inv = 1.0 / max(scale, 1e-12)
                best_box = tuple(int(round(v * inv)) for v in (x, y, w, h))
        return Candidate(path, best_score, best_box)
    except Exception:
        return Candidate(path, 0.0, None)


def find_candidates(paths: list[Path], result_limit: int = 16, scan_limit: int = 160) -> list[Candidate]:
    """Fast, opt-in panel candidate ranking.

    Only first/last captures are scanned because field panels are normally photographed
    immediately before or after a mission. Images are decoded at reduced resolution and
    processed in parallel. This function is never called during folder opening.
    """
    if len(paths) > scan_limit:
        half = scan_limit // 2
        selected = paths[:half] + paths[-half:]
    else:
        selected = paths
    workers = min(8, max(1, len(selected)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        candidates = list(executor.map(_score, selected))
    candidates.sort(key=lambda item: item.score, reverse=True)
    return [candidate for candidate in candidates[:result_limit] if candidate.score > 0]
