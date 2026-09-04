from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .calibration import BandModel
from .roi import RgbRoiAnnotation, RoiSample


COEFFICIENTS_FILENAME = "radiometric_calibration_coefficients.json"


def save_coefficients(
    project_dir: str | Path,
    input_dir: str | Path,
    sensor: str,
    samples: list[RoiSample],
    models: dict[str, BandModel],
    input_domain: str = "raw_dn",
    rgb_annotations: list[RgbRoiAnnotation] | None = None,
) -> Path:
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    output = project_dir / COEFFICIENTS_FILENAME
    payload = {
        "schema_version": 2,
        "saved_utc": datetime.now(timezone.utc).isoformat(),
        "sensor": sensor,
        "input_directory": str(Path(input_dir).resolve()),
        "input_domain": input_domain,
        "value_statistic": "trimmed_mean_2_percent",
        "models": {band: model.to_dict() for band, model in models.items()},
        "roi_samples": [sample.to_dict() for sample in samples],
        "rgb_annotations": [annotation.to_dict() for annotation in (rgb_annotations or [])],
    }
    # Keep an existing calibration intact if serialization/writing fails.
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=project_dir,
                                         prefix=".calibration-", suffix=".tmp", delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return output


def load_coefficients(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
