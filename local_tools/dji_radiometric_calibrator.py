#!/usr/bin/env python3
"""Launch the Windows DJI field-panel radiometric calibration desktop app."""

from __future__ import annotations

import sys
import json
import traceback
from pathlib import Path


def main() -> int:
    diagnostic_path = None
    if "--diagnose-file" in sys.argv:
        index = sys.argv.index("--diagnose-file")
        if index + 1 >= len(sys.argv):
            return 3
        diagnostic_path = Path(sys.argv[index + 1])
    try:
        from radiometric_calibrator.gui import diagnose, run
    except BaseException as exc:
        if diagnostic_path is not None:
            diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostic_path.write_text(
                json.dumps(
                    {
                        "status": "error",
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return 4
        if isinstance(exc, ImportError) and (exc.name == "PySide6" or "PySide6" in str(exc)):
            print(
                "PySide6 is not installed. On Windows run:\n"
                "  py -3.12 -m pip install -r local_tools/radiometric_calibrator/requirements-windows.txt",
                file=sys.stderr,
            )
            return 2
        raise
    if diagnostic_path is not None:
        try:
            return diagnose(diagnostic_path)
        except BaseException as exc:
            diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostic_path.write_text(
                json.dumps(
                    {
                        "status": "error",
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return 5
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
