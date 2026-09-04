from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImageMetadata:
    sensor: str | None
    make: str | None
    model: str | None
    band_name: str | None
    capture_uuid: str | None
    evidence: tuple[str, ...]


def _decoded_metadata_bytes(path: Path, limit: int = 512 * 1024) -> str:
    """Read enough EXIF/XMP payload to identify DJI cameras without ExifTool.

    DJI embeds the useful XML/XMP block near the start of its JPEG/TIFF files.
    Latin-1 provides a lossless byte-to-text mapping for regex searches.
    """
    with path.open("rb") as stream:
        head = stream.read(limit)
        stream.seek(0, 2)
        size = stream.tell()
        tail = b""
        if size > limit:
            stream.seek(max(0, size - limit))
            tail = stream.read(limit)
        return (head + b"\n" + tail).decode("latin-1", errors="ignore")


def _tag(text: str, names: tuple[str, ...]) -> str | None:
    for name in names:
        escaped = re.escape(name)
        patterns = (
            rf"{escaped}\s*=\s*[\"']([^\"']+)[\"']",
            rf"<{escaped}>([^<]+)</{escaped}>",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
    return None


def _clean(value: object | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).replace("\x00", "").strip()
    return cleaned or None


def read_image_metadata(path: str | Path) -> ImageMetadata:
    path = Path(path)
    make = model = band = capture_uuid = None
    evidence: list[str] = []

    # Read XMP first. This is faster than asking Pillow to parse a large TIFF
    # and contains DJI's CaptureUUID/BandName even for manual photographs.
    try:
        text = _decoded_metadata_bytes(path)
        make = _tag(text, ("tiff:Make", "exif:Make", "drone-dji:Make"))
        model = _tag(
            text,
            ("tiff:Model", "exif:Model", "drone-dji:Model", "drone-dji:CameraModelName"),
        )
        band = _tag(text, ("Camera:BandName", "camera:BandName", "drone-dji:BandName"))
        capture_uuid = _tag(text, ("drone-dji:CaptureUUID", "Camera:CaptureUUID"))
    except Exception:
        text = ""

    # Fall back to standard EXIF when a vendor does not mirror Make/Model in XMP.
    if not make or not model:
        try:
            from PIL import Image, ExifTags

            with Image.open(path) as image:
                exif = image.getexif()
                names = {ExifTags.TAGS.get(key, str(key)): value for key, value in exif.items()}
                make = make or _clean(names.get("Make"))
                model = model or _clean(names.get("Model"))
        except Exception:
            pass

    make, model, band, capture_uuid = map(_clean, (make, model, band, capture_uuid))

    combined = " ".join(value for value in (make, model) if value).upper()
    # FC6360 is the camera identifier used by original P4 Multispectral imagery.
    if any(token in combined for token in ("MAVIC 3 MULTISPECTRAL", "MAVIC3M", " M3M", "M3M ")) or combined == "M3M":
        sensor = "M3M"
        evidence.append(f"照片元数据型号匹配 M3M：{model or combined}")
    elif any(token in combined for token in ("PHANTOM 4 MULTISPECTRAL", "P4 MULTISPECTRAL", "P4M", "FC6360")):
        sensor = "P4M"
        evidence.append(f"照片元数据型号匹配 P4M：{model or combined}")
    else:
        sensor = None
    if make:
        evidence.append(f"Make={make}")
    if band:
        evidence.append(f"BandName={band}")
    return ImageMetadata(sensor, make, model, band, capture_uuid, tuple(evidence))
