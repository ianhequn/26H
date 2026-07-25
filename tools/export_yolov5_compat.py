"""Run a local YOLOv5 export.py with compatibility patches for recent PyTorch."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import torch


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "Usage: python export_yolov5_compat.py YOLOV5_ROOT [export.py arguments...]"
        )

    yolov5_root = Path(sys.argv[1]).resolve()
    export_script = yolov5_root / "export.py"
    if not export_script.is_file():
        raise FileNotFoundError(f"YOLOv5 export.py not found: {export_script}")

    original_load = torch.load
    original_export = torch.onnx.export

    def compatible_load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return original_load(*args, **kwargs)

    def compatible_export(*args, **kwargs):
        kwargs.setdefault("dynamo", False)
        return original_export(*args, **kwargs)

    torch.load = compatible_load
    torch.onnx.export = compatible_export

    sys.path.insert(0, str(yolov5_root))
    sys.argv = [str(export_script), *sys.argv[2:]]
    runpy.run_path(str(export_script), run_name="__main__")


if __name__ == "__main__":
    main()
