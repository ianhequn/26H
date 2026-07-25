from __future__ import annotations

import argparse
import math
import tempfile
from pathlib import Path

import nncase
import numpy as np
import onnx
import onnxsim
from PIL import Image


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert YOLOv5 ONNX to a K230 KModel.")
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target", default="k230")
    parser.add_argument("--input-width", default=224, type=int)
    parser.add_argument("--input-height", default=224, type=int)
    parser.add_argument("--ptq-option", default=2, type=int, choices=range(6))
    parser.add_argument("--calibration-samples", default=100, type=int)
    return parser.parse_args()


def simplify_onnx(model_path: Path, output_path: Path, input_shape: list[int]) -> Path:
    model = onnx.load(str(model_path))
    model = onnx.shape_inference.infer_shapes(model)
    input_name = model.graph.input[0].name
    simplified, valid = onnxsim.simplify(
        model,
        overwrite_input_shapes={input_name: input_shape},
    )
    if not valid:
        raise RuntimeError("Simplified ONNX model validation failed")
    onnx.save(simplified, str(output_path))
    return output_path


def calibration_data(dataset: Path, shape: list[int], samples: int) -> np.ndarray:
    paths = sorted(
        path for path in dataset.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if len(paths) < samples:
        raise ValueError(f"Calibration images not enough: {len(paths)} < {samples}")

    result = []
    height, width = shape[2], shape[3]
    for path in paths[:samples]:
        with Image.open(path) as image:
            image = image.convert("RGB")
            scale = min(width / image.width, height / image.height)
            resized = image.resize(
                (int(image.width * scale), int(image.height * scale)),
                Image.Resampling.BILINEAR,
            )
            canvas = Image.new("RGB", (width, height), (128, 128, 128))
            canvas.paste(
                resized,
                ((width - resized.width) // 2, (height - resized.height) // 2),
            )
            array = np.asarray(canvas, dtype=np.uint8)
            array = np.transpose(array, (2, 0, 1))[np.newaxis, ...]
            result.append([array])
    return np.asarray(result)


def configure_ptq(option: int, samples: int, data: np.ndarray) -> nncase.PTQTensorOptions:
    ptq = nncase.PTQTensorOptions()
    ptq.samples_count = samples

    if option == 0:
        ptq.calibrate_method = "NoClip"
        ptq.w_quant_type = "uint8"
        ptq.quant_type = "uint8"
    elif option == 1:
        ptq.calibrate_method = "NoClip"
        ptq.w_quant_type = "int16"
    elif option == 2:
        ptq.calibrate_method = "NoClip"
        ptq.quant_type = "int16"
    elif option == 3:
        ptq.calibrate_method = "Kld"
        ptq.w_quant_type = "uint8"
        ptq.quant_type = "uint8"
    elif option == 4:
        ptq.calibrate_method = "Kld"
        ptq.w_quant_type = "int16"
    elif option == 5:
        ptq.calibrate_method = "Kld"
        ptq.quant_type = "int16"

    ptq.set_tensor_data(data)
    return ptq


def main() -> None:
    args = parse_args()
    model = args.model.resolve()
    dataset = args.dataset.resolve()
    output = args.output.resolve()
    if not model.is_file():
        raise FileNotFoundError(model)
    if not dataset.is_dir():
        raise FileNotFoundError(dataset)

    width = int(math.ceil(args.input_width / 32.0)) * 32
    height = int(math.ceil(args.input_height / 32.0)) * 32
    input_shape = [1, 3, height, width]
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="k230_nncase_") as temporary:
        simplified = simplify_onnx(
            model,
            Path(temporary) / "simplified.onnx",
            input_shape,
        )

        options = nncase.CompileOptions()
        options.target = args.target
        options.preprocess = True
        options.swapRB = False
        options.input_shape = input_shape
        options.input_type = "uint8"
        options.input_range = [0, 1]
        options.mean = [0, 0, 0]
        options.std = [1, 1, 1]
        options.input_layout = "NCHW"
        options.quant_type = "uint8"

        compiler = nncase.Compiler(options)
        compiler.import_onnx(
            simplified.read_bytes(),
            nncase.ImportOptions(),
        )
        data = calibration_data(
            dataset,
            input_shape,
            args.calibration_samples,
        )
        compiler.use_ptq(
            configure_ptq(
                args.ptq_option,
                args.calibration_samples,
                data,
            )
        )
        compiler.compile()
        output.write_bytes(compiler.gencode_tobytes())

    print(f"KModel: {output}")
    print(f"Input: uint8 {input_shape}")
    print(
        f"PTQ: option={args.ptq_option}, "
        f"samples={args.calibration_samples}, target={args.target}"
    )


if __name__ == "__main__":
    main()

