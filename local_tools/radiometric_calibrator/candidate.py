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
    rectangles: tuple[tuple[int, int, int, int], ...] = ()
    panel_count: int = 0


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
        # Reflectance panels must be black/gray/white: hue is irrelevant, while
        # saturation/chroma stays low. Include dark panels (V >= 8), unlike the
        # former bright-only mask.
        neutral = cv2.inRange(hsv, np.asarray((0, 0, 8)), np.asarray((179, 65, 255)))
        kernel = np.ones((7, 7), np.uint8)
        neutral = cv2.morphologyEx(neutral, cv2.MORPH_CLOSE, kernel, iterations=2)
        contour_sets = []
        for mask in (edges, neutral):
            contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            contour_sets.extend(contours)
        area_image = float(gray.shape[0] * gray.shape[1])
        detections = []
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
            perimeter = cv2.arcLength(contour, True)
            polygon = cv2.approxPolyDP(contour, 0.035 * perimeter, True)
            # Straight edges are mandatory: retain convex quadrilaterals only.
            if len(polygon) != 4 or not cv2.isContourConvex(polygon):
                continue
            polygon = polygon.reshape(-1, 2).astype(np.int32)
            x, y, w, h = cv2.boundingRect(polygon)
            rectangularity = area / max(float(rw * rh), 1.0)
            if rectangularity < 0.55:
                continue
            mask = np.zeros_like(gray)
            cv2.fillPoly(mask, [polygon], 255)
            values = gray[mask != 0]
            saturation = hsv[:, :, 1][mask != 0]
            # Judge the region as a whole. Do not require an arbitrary share of
            # individual pixels to pass a binary threshold: labels, stains,
            # specular highlights and a little border background are tolerated.
            mean_saturation = float(np.mean(saturation))
            if mean_saturation > 85.0:
                continue
            neutrality = max(0.0, 1.0 - mean_saturation / 85.0)
            uniformity = max(0.0, 1.0 - float(values.std()) / 90.0)
            size_score = min(1.0, fraction / 0.02)
            straightness = min(1.0, cv2.arcLength(polygon, True) / max(perimeter, 1.0))
            score = (0.32 * rectangularity + 0.23 * straightness + 0.20 * uniformity
                     + 0.15 * neutrality + 0.10 * size_score)
            detections.append((score, (x, y, w, h)))

        # Edge and neutral masks can find the same panel. Keep non-overlapping
        # boxes, then combine several panels into one image-level score.
        kept = []
        for score, box in sorted(detections, reverse=True):
            x, y, w, h = box
            duplicate = False
            for _, (kx, ky, kw, kh) in kept:
                intersection = max(0, min(x+w, kx+kw)-max(x, kx)) * max(0, min(y+h, ky+kh)-max(y, ky))
                union = w*h + kw*kh - intersection
                if union and intersection / union > 0.45:
                    duplicate = True
                    break
            if not duplicate:
                kept.append((score, box))
            if len(kept) == 8:
                break
        if not kept:
            return Candidate(path, 0.0, None)
        inv = 1.0 / max(scale, 1e-12)
        boxes = tuple(tuple(int(round(v * inv)) for v in box) for _, box in kept)
        scores = [value for value, _ in kept[:4]]
        image_score = min(1.0, 0.75 * scores[0] + 0.25 * sum(scores) / len(scores)
                          + min(0.08, 0.02 * (len(kept) - 1)))
        return Candidate(path, image_score, boxes[0], boxes, len(boxes))
    except Exception:
        return Candidate(path, 0.0, None)


def find_candidates(paths: list[Path], result_limit: int = 16, progress=None) -> list[Candidate]:
    """Fast, opt-in panel candidate ranking.

    Every RGB capture is scanned. Images are decoded at reduced resolution and processed
    in parallel. This function is never called during folder opening.
    """
    selected = list(paths)
    if not selected:
        return []
    report = progress or (lambda done, total: None)
    workers = min(8, len(selected))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        candidates = []
        for done, candidate in enumerate(executor.map(_score, selected), 1):
            candidates.append(candidate)
            report(done, len(selected))
    candidates.sort(key=lambda item: item.score, reverse=True)
    return [candidate for candidate in candidates[:result_limit] if candidate.score >= 0.55]
