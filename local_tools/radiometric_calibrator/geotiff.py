from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio


def describe_bands(path: str | Path) -> list[str]:
    with rasterio.open(path) as source:
        return [description or f"Band {index}" for index, description in enumerate(source.descriptions, 1)]


def apply_models(
    source_path: str | Path,
    output_path: str | Path,
    models: dict[str, dict],
    band_map: dict[str, int],
    clip: bool = False,
) -> Path:
    source_path, output_path = Path(source_path), Path(output_path)
    ordered_bands = list(models)
    with rasterio.open(source_path) as source:
        profile = source.profile.copy()
        profile.update(
            driver="GTiff",
            dtype="float32",
            count=len(ordered_bands),
            compress="deflate",
            tiled=True,
            predictor=3,
            nodata=np.nan,
            BIGTIFF="IF_SAFER",
        )
        # A source strip profile can carry non-compliant block sizes. Explicitly
        # choose valid TIFF tile dimensions (multiples of 16) for the output.
        profile["blockxsize"] = 256
        profile["blockysize"] = 256
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_path, "w", **profile) as target:
            for output_index, band in enumerate(ordered_bands, 1):
                if band not in band_map:
                    raise ValueError(f"没有为 {band} 指定输入 GeoTIFF 波段。")
                source_index = int(band_map[band])
                model = models[band]
                for _, window in source.block_windows(source_index):
                    raw = source.read(source_index, window=window, masked=True).astype(np.float32)
                    result = raw * np.float32(model["slope"]) + np.float32(model["intercept"])
                    if clip:
                        result = np.ma.clip(result, 0.0, 1.0)
                    target.write(result.filled(np.nan).astype(np.float32), output_index, window=window)
                target.set_band_description(output_index, band)
                target.update_tags(
                    output_index,
                    calibration_slope=str(model["slope"]),
                    calibration_intercept=str(model["intercept"]),
                    calibration_method=str(model["method"]),
                    units="reflectance_0_to_1",
                )
            target.update_tags(
                radiometric_calibration="field_reflectance_panel",
                source_orthophoto=str(source_path.resolve()),
            )
    return output_path
