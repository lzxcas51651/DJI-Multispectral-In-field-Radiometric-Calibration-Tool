from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .roi import RoiSample


@dataclass
class BandModel:
    band: str
    slope: float
    intercept: float
    method: str
    sample_count: int
    reflectance_levels: int
    r_squared: float | None
    rmse: float

    def to_dict(self) -> dict:
        return asdict(self)


def _huber_line(x: np.ndarray, y: np.ndarray, iterations: int = 20) -> tuple[float, float]:
    design = np.column_stack((x, np.ones_like(x)))
    weights = np.ones_like(y)
    beta = np.linalg.lstsq(design, y, rcond=None)[0]
    for _ in range(iterations):
        residuals = y - design @ beta
        scale = 1.4826 * np.median(np.abs(residuals - np.median(residuals)))
        if scale <= 1e-12:
            break
        cutoff = 1.345 * scale
        absolute = np.abs(residuals)
        weights = np.where(absolute <= cutoff, 1.0, cutoff / np.maximum(absolute, 1e-12))
        weighted = design * np.sqrt(weights)[:, None]
        target = y * np.sqrt(weights)
        updated = np.linalg.lstsq(weighted, target, rcond=None)[0]
        if np.allclose(beta, updated, rtol=1e-10, atol=1e-12):
            beta = updated
            break
        beta = updated
    return float(beta[0]), float(beta[1])


def fit_models(samples: list[RoiSample], value_field: str = "trimmed_mean") -> dict[str, BandModel]:
    models: dict[str, BandModel] = {}
    bands = sorted({sample.band for sample in samples if sample.enabled and sample.band != "RGB"})
    for band in bands:
        selected = [sample for sample in samples if sample.enabled and sample.band == band]
        x = np.asarray([float(getattr(sample, value_field)) for sample in selected], dtype=np.float64)
        y = np.asarray([sample.reflectance for sample in selected], dtype=np.float64)
        valid = np.isfinite(x) & np.isfinite(y) & (x > 0)
        x, y = x[valid], y[valid]
        if x.size == 0:
            continue
        levels = len({round(float(v), 8) for v in y})
        if levels < 2:
            slope = float(np.dot(x, y) / np.dot(x, x))
            intercept = 0.0
            method = "least_squares_through_origin"
        elif x.size == 2:
            slope, intercept = map(float, np.linalg.lstsq(np.column_stack((x, np.ones_like(x))), y, rcond=None)[0])
            method = "linear_least_squares"
        else:
            slope, intercept = _huber_line(x, y)
            method = "huber_linear_regression"
        predicted = slope * x + intercept
        residual = y - predicted
        rmse = float(np.sqrt(np.mean(residual**2)))
        total = float(np.sum((y - y.mean()) ** 2))
        r_squared = float(1.0 - np.sum(residual**2) / total) if total > 0 else None
        models[band] = BandModel(band, slope, intercept, method, int(x.size), levels, r_squared, rmse)
    return models
