"""Generate deterministic WiX v4 components from the onedir release only."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

NAMESPACE = "http://wixtoolset.org/schemas/v4/wxs"
COMPONENT_NAMESPACE = uuid.UUID("301e65ed-4d4e-4b43-8a6b-daf60b5b4ea2")
EXE_NAME = "DJI_Radiometric_Calibration_Tool.exe"
ET.register_namespace("", NAMESPACE)


def element(parent, tag, **attributes):
    return ET.SubElement(parent, f"{{{NAMESPACE}}}{tag}", attributes)


def identifier(prefix: str, relative: str) -> str:
    return prefix + hashlib.sha256(relative.lower().encode("utf-8")).hexdigest()[:32]


def generate(payload: Path, output: Path) -> int:
    payload = payload.resolve(strict=True)
    if not (payload / EXE_NAME).is_file() or not (payload / "_internal").is_dir():
        raise ValueError("Expected a PyInstaller onedir release containing EXE and _internal")
    # Do not permit collecting a repository/data root accidentally. The release
    # root must contain only the launcher and its bundled dependency directory.
    unexpected = [p.name for p in payload.iterdir() if p.name not in {EXE_NAME, "_internal"}]
    if unexpected:
        raise ValueError(f"Unexpected files in release root (remove/move them first): {unexpected}")
    entries = sorted(payload.rglob("*"), key=lambda p: p.relative_to(payload).as_posix().lower())
    for path in entries:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Release must not contain links or junctions: {path}")
        if not path.resolve().is_relative_to(payload):
            raise ValueError(f"Release entry escapes payload: {path}")
    files = [p for p in entries if p.is_file()]
    root = ET.Element(f"{{{NAMESPACE}}}Wix")
    fragment = element(root, "Fragment")
    directory_root = element(fragment, "DirectoryRef", Id="INSTALLFOLDER")
    directories = {".": directory_root}
    group = element(fragment, "ComponentGroup", Id="ApplicationPayload")
    seen = set()
    for path in files:
        relative = path.relative_to(payload)
        key = relative.as_posix().lower()
        if key in seen:
            raise ValueError(f"Case-insensitive path collision: {relative}")
        seen.add(key)
        parent = Path(".")
        for part in relative.parts[:-1]:
            child = parent / part
            if child.as_posix() not in directories:
                directories[child.as_posix()] = element(
                    directories[parent.as_posix()], "Directory",
                    Id=identifier("Dir_", child.as_posix()), Name=part,
                )
            parent = child
        component_id = identifier("Cmp_", key)
        component = element(
            directories[parent.as_posix()], "Component", Id=component_id,
            Guid=str(uuid.uuid5(COMPONENT_NAMESPACE, key)).upper(), Bitness="always64",
        )
        file_id = "ApplicationExecutable" if relative.as_posix() == EXE_NAME else identifier("File_", key)
        element(component, "File", Id=file_id, Name=relative.name, KeyPath="yes",
                Source="$(var.PayloadDir)\\" + str(relative).replace("/", "\\"))
        element(group, "ComponentRef", Id=component_id)
    ET.indent(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = generate(args.payload, args.output)
    print(f"Generated {count} file components: {args.output}")
