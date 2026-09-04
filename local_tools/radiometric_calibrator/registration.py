from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .roi import read_unchanged


@dataclass(frozen=True)
class RegistrationResult:
    matrix: np.ndarray
    method: str
    score: float
    matches: int = 0


def _gray_preview(path: str | Path, maximum: int = 1200) -> tuple[np.ndarray, float, float]:
    image = read_unchanged(path)
    if image.ndim == 3:
        if image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        else:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        raise ValueError(f"影像没有有效像素：{path}")
    low, high = np.percentile(finite, (1, 99))
    gray = np.clip((image.astype(np.float32) - low) * 255.0 / max(high - low, 1.0), 0, 255).astype(np.uint8)
    height, width = gray.shape
    scale = min(1.0, maximum / max(width, height))
    if scale < 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.createCLAHE(2.0, (8, 8)).apply(gray)
    return gray, gray.shape[1] / width, gray.shape[0] / height


def _to_original_matrix(matrix: np.ndarray, rgb_scale: tuple[float, float], band_scale: tuple[float, float]) -> np.ndarray:
    source_scale = np.asarray(
        [[rgb_scale[0], 0.0, 0.0], [0.0, rgb_scale[1], 0.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    target_inverse = np.asarray(
        [[1.0 / band_scale[0], 0.0, 0.0], [0.0, 1.0 / band_scale[1], 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    return target_inverse @ matrix @ source_scale


def register_rgb_to_band(rgb_path: str | Path, band_path: str | Path) -> RegistrationResult:
    """Estimate a lightweight RGB-original -> band-original projective transform."""
    rgb, rgb_sx, rgb_sy = _gray_preview(rgb_path)
    band, band_sx, band_sy = _gray_preview(band_path)

    orb = cv2.ORB_create(nfeatures=3500, fastThreshold=8)
    rgb_points, rgb_desc = orb.detectAndCompute(rgb, None)
    band_points, band_desc = orb.detectAndCompute(band, None)
    if rgb_desc is not None and band_desc is not None:
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(rgb_desc, band_desc, k=2)
        good = [first for first, second in pairs if first.distance < 0.78 * second.distance]
        if len(good) >= 10:
            source = np.float32([rgb_points[item.queryIdx].pt for item in good])
            target = np.float32([band_points[item.trainIdx].pt for item in good])
            matrix, inliers = cv2.findHomography(source, target, cv2.RANSAC, 4.0)
            if matrix is not None and inliers is not None:
                ratio = float(inliers.mean())
                if int(inliers.sum()) >= 8 and ratio >= 0.25:
                    original = _to_original_matrix(matrix, (rgb_sx, rgb_sy), (band_sx, band_sy))
                    return RegistrationResult(original, "ORB-Homography", ratio, len(good))

    # Cross-spectral texture can be weak. ECC operates on gradient structure
    # after resizing the band to the RGB preview canvas and estimates an affine
    # mapping without requiring matching descriptors.
    resized_band = cv2.resize(band, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_AREA)
    template = cv2.GaussianBlur(rgb, (5, 5), 0).astype(np.float32) / 255.0
    moving = cv2.GaussianBlur(resized_band, (5, 5), 0).astype(np.float32) / 255.0
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        score, warp = cv2.findTransformECC(
            template,
            moving,
            warp,
            cv2.MOTION_AFFINE,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5),
            None,
            3,
        )
        # findTransformECC returns a matrix used with WARP_INVERSE_MAP to align
        # moving to template; invert it to obtain RGB-preview -> resized-band.
        inverse = np.linalg.inv(np.vstack([warp, [0.0, 0.0, 1.0]]))
        resize_to_band = np.asarray(
            [[band.shape[1] / rgb.shape[1], 0.0, 0.0], [0.0, band.shape[0] / rgb.shape[0], 0.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        matrix = resize_to_band @ inverse
        original = _to_original_matrix(matrix, (rgb_sx, rgb_sy), (band_sx, band_sy))
        return RegistrationResult(original, "ECC-Affine", float(score))
    except cv2.error:
        # Deterministic final fallback preserves normalized image coordinates.
        matrix = np.asarray(
            [[band.shape[1] / rgb.shape[1], 0.0, 0.0], [0.0, band.shape[0] / rgb.shape[0], 0.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        original = _to_original_matrix(matrix, (rgb_sx, rgb_sy), (band_sx, band_sy))
        return RegistrationResult(original, "Normalized-Scale-Fallback", 0.0)


def transform_polygon(polygon: list[list[float]], matrix: np.ndarray) -> list[list[float]]:
    points = np.asarray(polygon, dtype=np.float32).reshape(1, -1, 2)
    mapped = cv2.perspectiveTransform(points, matrix.astype(np.float64))[0]
    return [[float(x), float(y)] for x, y in mapped]
