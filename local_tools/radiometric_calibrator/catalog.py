from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .metadata import read_image_metadata


BANDS = {
    "P4M": ("Blue", "Green", "Red", "RedEdge", "NIR"),
    "M3M": ("Green", "Red", "RedEdge", "NIR"),
}

P4M_RE = re.compile(
    r"^(?:\d+_)?(?P<base>DJI_(?P<capture>\d+))(?P<code>[0-5])\.(?P<ext>jpe?g|tiff?)$",
    re.IGNORECASE,
)
M3M_MS_RE = re.compile(
    r"^(?P<base>.+?)_MS_(?P<code>NIR|RE|G|R)\.(?P<ext>tiff?)$", re.IGNORECASE
)
M3M_RGB_RE = re.compile(r"^(?P<base>.+?)_D\.(?P<ext>jpe?g|dng)$", re.IGNORECASE)

P4M_CODES = {
    "0": "RGB",
    "1": "Blue",
    "2": "Green",
    "3": "Red",
    "4": "RedEdge",
    "5": "NIR",
}
M3M_CODES = {"G": "Green", "R": "Red", "RE": "RedEdge", "NIR": "NIR"}


@dataclass
class Capture:
    key: str
    files: dict[str, Path] = field(default_factory=dict)
    capture_uuid: str | None = None

    @property
    def preview_path(self) -> Path | None:
        return self.files.get("RGB") or self.files.get("Green") or next(iter(self.files.values()), None)


@dataclass
class Catalog:
    root: Path
    sensor: str
    confidence: str
    reasons: list[str]
    captures: list[Capture]
    unrecognized: list[Path]

    @property
    def expected_bands(self) -> tuple[str, ...]:
        return BANDS[self.sensor]

    @property
    def complete_captures(self) -> list[Capture]:
        required = set(self.expected_bands)
        return [capture for capture in self.captures if required.issubset(capture.files)]

    @property
    def preview_images(self) -> list[Path]:
        # The calibration UI intentionally exposes only RGB photographs.
        return [capture.files["RGB"] for capture in self.captures if "RGB" in capture.files]


def _scores(paths: list[Path]) -> tuple[dict[str, int], list[str]]:
    scores = {"P4M": 0, "M3M": 0}
    reasons: list[str] = []
    p4_bands: set[str] = set()
    m3_bands: set[str] = set()
    for path in paths:
        p4 = P4M_RE.match(path.name)
        if p4:
            scores["P4M"] += 2
            p4_bands.add(P4M_CODES[p4.group("code")])
        m3 = M3M_MS_RE.match(path.name)
        if m3:
            scores["M3M"] += 3
            m3_bands.add(M3M_CODES[m3.group("code").upper()])
        elif M3M_RGB_RE.match(path.name):
            scores["M3M"] += 1
    if set(BANDS["P4M"]).issubset(p4_bands):
        scores["P4M"] += 100
        reasons.append("检测到 P4M 的 Blue/Green/Red/RedEdge/NIR 完整波段命名")
    if set(BANDS["M3M"]).issubset(m3_bands):
        scores["M3M"] += 100
        reasons.append("检测到 M3M 的 _MS_G/_MS_R/_MS_RE/_MS_NIR 完整波段命名")
    return scores, reasons


def detect_sensor(root: Path, requested: str = "AUTO") -> tuple[str, str, list[str]]:
    requested = requested.upper()
    if requested in BANDS:
        return requested, "人工指定", [f"用户指定传感器为 {requested}"]
    paths = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".dng", ".tif", ".tiff"}]
    scores, reasons = _scores(paths)
    # Read a small, spread-out sample. Metadata is authoritative and does not
    # require a waypoint/route file, so manual panel photographs work too.
    if paths:
        # Prefer JPEG previews, then inspect only until one authoritative model
        # is found. This keeps folder opening fast even for thousands of TIFFs.
        metadata_candidates = sorted(paths, key=lambda p: (p.suffix.lower() not in {".jpg", ".jpeg"}, str(p)))
        for path in metadata_candidates[:8]:
            metadata = read_image_metadata(path)
            if metadata.sensor:
                scores[metadata.sensor] += 500
                reasons.extend(item for item in metadata.evidence if item not in reasons)
                break
    sensor = max(scores, key=scores.get)
    if scores[sensor] == 0:
        raise ValueError("无法从文件名识别 P4M 或 M3M，请在界面中手动指定传感器。")
    other = "M3M" if sensor == "P4M" else "P4M"
    confidence = "高" if scores[sensor] >= 100 and scores[sensor] > scores[other] * 2 else "中"
    reasons.append(f"文件名评分：P4M={scores['P4M']}，M3M={scores['M3M']}")
    return sensor, confidence, reasons


def scan_folder(root: str | Path, requested_sensor: str = "AUTO") -> Catalog:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"输入文件夹不存在：{root}")
    sensor, confidence, reasons = detect_sensor(root, requested_sensor)
    grouped: dict[str, Capture] = {}
    unrecognized: list[Path] = []
    image_exts = {".jpg", ".jpeg", ".dng", ".tif", ".tiff"}
    for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
        if not path.is_file() or path.suffix.lower() not in image_exts:
            continue
        metadata = None
        if sensor == "P4M":
            match = P4M_RE.match(path.name)
            if match:
                key = match.group("base").lower()
                group_id = key
                band = P4M_CODES[match.group("code")]
            else:
                metadata = read_image_metadata(path)
                if metadata.sensor == sensor and metadata.band_name and metadata.capture_uuid:
                    key, group_id = path.stem.lower(), metadata.capture_uuid
                    band = metadata.band_name.replace(" ", "")
                else:
                    unrecognized.append(path)
                    continue
        else:
            match = M3M_MS_RE.match(path.name)
            if match:
                key = match.group("base").lower()
                # The timestamp can differ by a second between manually captured
                # RGB/MS files. DJI's trailing counter remains the shared key.
                counter = key.rsplit("_", 1)[-1]
                group_id = f"m3m-counter-{counter}" if counter.isdigit() else key
                band = M3M_CODES[match.group("code").upper()]
            else:
                match = M3M_RGB_RE.match(path.name)
                if not match:
                    metadata = read_image_metadata(path)
                    if metadata.sensor == sensor and metadata.capture_uuid:
                        key, group_id = path.stem.lower(), metadata.capture_uuid
                        band = metadata.band_name.replace(" ", "") if metadata.band_name else "RGB"
                    else:
                        unrecognized.append(path)
                        continue
                else:
                    key, band = match.group("base").lower(), "RGB"
                    counter = key.rsplit("_", 1)[-1]
                    group_id = f"m3m-counter-{counter}" if counter.isdigit() else key
        if metadata and metadata.band_name:
            aliases = {"G": "Green", "R": "Red", "RE": "RedEdge", "N": "NIR"}
            metadata_band = aliases.get(metadata.band_name.upper(), metadata.band_name.replace(" ", ""))
            if metadata_band in {"Blue", "Green", "Red", "RedEdge", "NIR"}:
                band = metadata_band
        capture_uuid = metadata.capture_uuid if metadata else None
        capture = grouped.setdefault(group_id, Capture(key, capture_uuid=capture_uuid))
        capture.files.setdefault(band, path)
    captures = sorted(grouped.values(), key=lambda c: c.key)
    if not captures:
        raise ValueError(f"没有发现可用的 {sensor} 影像。")
    return Catalog(root, sensor, confidence, reasons, captures, unrecognized)
