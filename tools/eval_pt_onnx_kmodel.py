from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
IOU_VECTOR = np.linspace(0.50, 0.95, 10)
DEPLOY_CONF_THRESHOLD = 0.25
DEPLOY_NMS_THRESHOLD = 0.45


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统一评测YOLOv5 PT、ONNX和K230 KModel。"
    )
    parser.add_argument("--pt", type=Path)
    parser.add_argument("--onnx", type=Path)
    parser.add_argument("--kmodel", type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--imgsz", default=224, type=int)
    parser.add_argument("--conf-thres", default=0.001, type=float)
    parser.add_argument("--iou-thres", default=0.60, type=float)
    parser.add_argument("--max-det", default=5, type=int)
    parser.add_argument("--save-dir", required=True, type=Path)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--save-images", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--yolov5-root", type=Path)
    parser.add_argument("--merge-existing", action="store_true")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_simple_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("YAML根节点必须是字典")
        return data
    except ImportError:
        result: dict[str, Any] = {}
        names: dict[int, str] = {}
        in_names = False
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if line.startswith("names:"):
                in_names = True
                continue
            if in_names and line.startswith("  "):
                key, value = line.strip().split(":", 1)
                names[int(key)] = value.strip().strip("\"'")
                continue
            in_names = False
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip("\"'")
        result["names"] = names
        return result


def resolve_dataset(data_yaml: Path) -> tuple[list[Path], dict[int, str]]:
    config = load_simple_yaml(data_yaml)
    base = Path(str(config.get("path", data_yaml.parent)))
    if not base.is_absolute():
        base = (data_yaml.parent / base).resolve()
    val_value = config.get("val")
    if not val_value:
        raise ValueError(f"数据配置没有val字段: {data_yaml}")
    val_path = Path(str(val_value))
    if not val_path.is_absolute():
        val_path = base / val_path

    if val_path.is_dir():
        images = sorted(
            path
            for path in val_path.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
    elif val_path.is_file():
        images = []
        for line in val_path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value:
                continue
            image_path = Path(value)
            if not image_path.is_absolute():
                image_path = val_path.parent / image_path
            images.append(image_path.resolve())
    else:
        raise FileNotFoundError(f"验证集路径不存在: {val_path}")

    raw_names = config.get("names", {0: "steel_ball"})
    if isinstance(raw_names, list):
        names = {index: str(name) for index, name in enumerate(raw_names)}
    else:
        names = {int(key): str(value) for key, value in raw_names.items()}
    return images, names


def label_path_for_image(image_path: Path) -> Path:
    parts = list(image_path.parts)
    indices = [index for index, value in enumerate(parts) if value == "images"]
    if not indices:
        raise ValueError(f"图片路径中没有images目录: {image_path}")
    parts[indices[-1]] = "labels"
    return Path(*parts).with_suffix(".txt")


def load_labels(image_path: Path, width: int, height: int) -> np.ndarray:
    label_path = label_path_for_image(image_path)
    if not label_path.exists():
        raise FileNotFoundError(f"标签文件不存在: {label_path}")
    rows = []
    for line in label_path.read_text(encoding="utf-8").splitlines():
        values = line.split()
        if not values:
            continue
        if len(values) != 5:
            raise ValueError(f"标签格式错误: {label_path}: {line}")
        cls_id, cx, cy, box_w, box_h = [float(value) for value in values]
        x1 = (cx - box_w / 2.0) * width
        y1 = (cy - box_h / 2.0) * height
        x2 = (cx + box_w / 2.0) * width
        y2 = (cy + box_h / 2.0) * height
        rows.append([cls_id, x1, y1, x2, y2])
    if not rows:
        return np.zeros((0, 5), dtype=np.float32)
    return np.asarray(rows, dtype=np.float32)


def letterbox_rgb(
    image: Image.Image,
    size: int,
) -> tuple[np.ndarray, tuple[float, float], tuple[float, float]]:
    width, height = image.size
    ratio = min(size / width, size / height)
    new_width = int(round(width * ratio))
    new_height = int(round(height * ratio))
    if (new_width, new_height) != image.size:
        image = image.resize((new_width, new_height), Image.Resampling.BILINEAR)
    pad_x = (size - new_width) / 2.0
    pad_y = (size - new_height) / 2.0
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(image, (int(round(pad_x - 0.1)), int(round(pad_y - 0.1))))
    array = np.asarray(canvas, dtype=np.uint8)
    chw = np.transpose(array, (2, 0, 1))[None, ...]
    return np.ascontiguousarray(chw), (ratio, ratio), (pad_x, pad_y)


def scale_boxes_to_original(
    boxes: np.ndarray,
    original_shape: tuple[int, int],
    ratio: tuple[float, float],
    pad: tuple[float, float],
) -> np.ndarray:
    if boxes.size == 0:
        return boxes.copy()
    scaled = boxes.copy()
    scaled[:, [0, 2]] -= pad[0]
    scaled[:, [1, 3]] -= pad[1]
    scaled[:, [0, 2]] /= ratio[0]
    scaled[:, [1, 3]] /= ratio[1]
    height, width = original_shape
    scaled[:, [0, 2]] = scaled[:, [0, 2]].clip(0, width)
    scaled[:, [1, 3]] = scaled[:, [1, 3]].clip(0, height)
    return scaled


def xywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    output = boxes.copy()
    output[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.0
    output[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.0
    output[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.0
    output[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.0
    return output


def box_iou(boxes_a: np.ndarray, boxes_b: np.ndarray) -> np.ndarray:
    if len(boxes_a) == 0 or len(boxes_b) == 0:
        return np.zeros((len(boxes_a), len(boxes_b)), dtype=np.float32)
    top_left = np.maximum(boxes_a[:, None, :2], boxes_b[None, :, :2])
    bottom_right = np.minimum(boxes_a[:, None, 2:], boxes_b[None, :, 2:])
    intersection = np.clip(bottom_right - top_left, 0, None)
    intersection_area = intersection[..., 0] * intersection[..., 1]
    area_a = np.clip(boxes_a[:, 2] - boxes_a[:, 0], 0, None) * np.clip(
        boxes_a[:, 3] - boxes_a[:, 1], 0, None
    )
    area_b = np.clip(boxes_b[:, 2] - boxes_b[:, 0], 0, None) * np.clip(
        boxes_b[:, 3] - boxes_b[:, 1], 0, None
    )
    return intersection_area / (
        area_a[:, None] + area_b[None, :] - intersection_area + 1e-7
    )


def nms_yolov5(
    prediction: np.ndarray,
    conf_threshold: float,
    iou_threshold: float,
    max_det: int,
) -> np.ndarray:
    prediction = np.asarray(prediction)
    if prediction.ndim == 3:
        if prediction.shape[0] != 1:
            raise ValueError(f"只支持batch=1，收到: {prediction.shape}")
        prediction = prediction[0]
    if prediction.ndim != 2 or prediction.shape[1] < 6:
        raise ValueError(f"YOLO输出必须是[N, 5+nc]，收到: {prediction.shape}")

    class_probs = prediction[:, 5:]
    class_ids = class_probs.argmax(axis=1)
    class_scores = class_probs.max(axis=1)
    scores = prediction[:, 4] * class_scores
    keep_mask = scores > conf_threshold
    if not np.any(keep_mask):
        return np.zeros((0, 6), dtype=np.float32)

    boxes = xywh_to_xyxy(prediction[keep_mask, :4].astype(np.float32))
    scores = scores[keep_mask].astype(np.float32)
    class_ids = class_ids[keep_mask].astype(np.float32)
    order = np.argsort(-scores)
    selected: list[int] = []

    while order.size and len(selected) < max_det:
        current = int(order[0])
        selected.append(current)
        if order.size == 1:
            break
        remaining = order[1:]
        ious = box_iou(boxes[current : current + 1], boxes[remaining])[0]
        same_class = class_ids[remaining] == class_ids[current]
        order = remaining[~((ious > iou_threshold) & same_class)]

    selected_array = np.asarray(selected, dtype=np.int64)
    return np.column_stack(
        (
            boxes[selected_array],
            scores[selected_array],
            class_ids[selected_array],
        )
    ).astype(np.float32)


def adapt_raw_outputs(outputs: list[np.ndarray]) -> np.ndarray:
    if len(outputs) != 1:
        shapes = [list(np.asarray(output).shape) for output in outputs]
        raise RuntimeError(
            "当前评测只接受已解码的单输出YOLOv5模型；"
            f"发现{len(outputs)}个输出: {shapes}。禁止静默猜测多尺度特征图。"
        )
    output = np.asarray(outputs[0])
    if output.ndim != 3 or output.shape[0] != 1 or output.shape[2] < 6:
        raise RuntimeError(f"无法适配YOLOv5输出: {output.shape}")
    return output.astype(np.float32, copy=False)


class PTBackend:
    name = "pt"

    def __init__(self, path: Path, device: str, yolov5_root: Path | None):
        if yolov5_root is None:
            raise ValueError("PTBackend需要--yolov5-root")
        sys.path.insert(0, str(yolov5_root.resolve()))
        import torch

        self.torch = torch
        original_load = torch.load

        def compatible_load(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("weights_only", False)
            return original_load(*args, **kwargs)

        torch.load = compatible_load
        from models.experimental import attempt_load

        self.device = torch.device(device)
        self.model = attempt_load(
            str(path),
            device=self.device,
            inplace=True,
            fuse=True,
        ).float().eval()
        self.path = path
        self.input_dtype = "float32"
        self.input_shape = [1, 3, 224, 224]
        self.output_shapes: list[list[int]] = []
        self.output_dtypes: list[str] = []

    def warmup(self, input_u8: np.ndarray) -> None:
        tensor = self.torch.from_numpy(input_u8).to(self.device).float() / 255.0
        with self.torch.no_grad():
            self.model(tensor)

    def predict(self, input_u8: np.ndarray) -> tuple[list[np.ndarray], float]:
        tensor = self.torch.from_numpy(input_u8).to(self.device).float() / 255.0
        start = time.perf_counter()
        with self.torch.no_grad():
            output = self.model(tensor)
        elapsed = (time.perf_counter() - start) * 1000.0
        if isinstance(output, (tuple, list)):
            output = output[0]
        array = output.detach().cpu().numpy()
        return [array], elapsed


class ONNXBackend:
    name = "onnx"

    def __init__(self, path: Path, _device: str, _yolov5_root: Path | None):
        import onnxruntime as ort

        self.path = path
        self.session = ort.InferenceSession(
            str(path),
            providers=["CPUExecutionProvider"],
        )
        self.input = self.session.get_inputs()[0]
        self.outputs = self.session.get_outputs()
        self.input_dtype = self.input.type
        self.input_shape = list(self.input.shape)
        self.output_shapes = [list(output.shape) for output in self.outputs]
        self.output_dtypes = [output.type for output in self.outputs]

    def warmup(self, input_u8: np.ndarray) -> None:
        self.session.run(
            None,
            {self.input.name: input_u8.astype(np.float32) / 255.0},
        )

    def predict(self, input_u8: np.ndarray) -> tuple[list[np.ndarray], float]:
        input_array = input_u8.astype(np.float32) / 255.0
        start = time.perf_counter()
        outputs = self.session.run(None, {self.input.name: input_array})
        elapsed = (time.perf_counter() - start) * 1000.0
        return [np.asarray(output) for output in outputs], elapsed


class KModelBackend:
    name = "kmodel"

    def __init__(self, path: Path, _device: str, _yolov5_root: Path | None):
        import nncase

        self.nncase = nncase
        self.path = path
        self.simulator = nncase.Simulator()
        self.simulator.load_model(path.read_bytes())
        self.input_desc = self.simulator.get_input_desc(0)
        self.input_shape = list(self.simulator.get_input_shape(0))
        self.input_dtype = str(self.input_desc.dtype)
        self.output_shapes = [
            list(self.simulator.get_output_shape(index))
            for index in range(self.simulator.outputs_size)
        ]
        self.output_dtypes = [
            str(self.simulator.get_output_desc(index).dtype)
            for index in range(self.simulator.outputs_size)
        ]

    def warmup(self, input_u8: np.ndarray) -> None:
        self._run(input_u8)

    def _run(self, input_u8: np.ndarray) -> list[np.ndarray]:
        expected_shape = tuple(self.input_shape)
        if tuple(input_u8.shape) != expected_shape:
            raise ValueError(
                f"KModel输入shape不一致: expected={expected_shape}, actual={input_u8.shape}"
            )
        input_array = np.ascontiguousarray(
            input_u8.astype(self.input_desc.dtype, copy=False)
        )
        tensor = self.nncase.RuntimeTensor.from_numpy(input_array)
        self.simulator.set_input_tensor(0, tensor)
        self.simulator.run()
        return [
            self.simulator.get_output_tensor(index).to_numpy()
            for index in range(self.simulator.outputs_size)
        ]

    def predict(self, input_u8: np.ndarray) -> tuple[list[np.ndarray], float]:
        start = time.perf_counter()
        outputs = self._run(input_u8)
        elapsed = (time.perf_counter() - start) * 1000.0
        return outputs, elapsed


def match_boxes(
    detections: np.ndarray,
    labels: np.ndarray,
    iou_threshold: float,
) -> list[tuple[int, int, float]]:
    if len(detections) == 0 or len(labels) == 0:
        return []
    ious = box_iou(labels[:, 1:5], detections[:, :4])
    candidates = []
    for gt_index in range(len(labels)):
        for pred_index in range(len(detections)):
            if int(labels[gt_index, 0]) != int(detections[pred_index, 5]):
                continue
            iou = float(ious[gt_index, pred_index])
            if iou >= iou_threshold:
                candidates.append((iou, gt_index, pred_index))
    candidates.sort(reverse=True)
    used_gt: set[int] = set()
    used_pred: set[int] = set()
    matches = []
    for iou, gt_index, pred_index in candidates:
        if gt_index in used_gt or pred_index in used_pred:
            continue
        used_gt.add(gt_index)
        used_pred.add(pred_index)
        matches.append((gt_index, pred_index, iou))
    return matches


def process_batch_ap(
    detections: np.ndarray,
    labels: np.ndarray,
    iou_vector: np.ndarray,
) -> np.ndarray:
    correct = np.zeros((len(detections), len(iou_vector)), dtype=bool)
    for threshold_index, threshold in enumerate(iou_vector):
        for _gt_index, pred_index, _iou in match_boxes(
            detections,
            labels,
            float(threshold),
        ):
            correct[pred_index, threshold_index] = True
    return correct


def compute_ap(recall: np.ndarray, precision: np.ndarray) -> float:
    mrec = np.concatenate(([0.0], recall, [1.0]))
    mpre = np.concatenate(([1.0], precision, [0.0]))
    mpre = np.flip(np.maximum.accumulate(np.flip(mpre)))
    x_values = np.linspace(0, 1, 101)
    return float(np.trapz(np.interp(x_values, mrec, mpre), x_values))


def smooth(values: np.ndarray, fraction: float = 0.05) -> np.ndarray:
    width = round(len(values) * fraction * 2) // 2 + 1
    padding = np.ones(width // 2)
    padded = np.concatenate(
        (padding * values[0], values, padding * values[-1]),
        axis=0,
    )
    return np.convolve(padded, np.ones(width) / width, mode="valid")


def calculate_ap_metrics(
    correct: np.ndarray,
    confidence: np.ndarray,
    predicted_class: np.ndarray,
    target_class: np.ndarray,
) -> dict[str, float]:
    if len(target_class) == 0:
        return {
            "precision": 0.0,
            "recall": 0.0,
            "map50": 0.0,
            "map50_95": 0.0,
        }
    if len(confidence) == 0:
        return {
            "precision": 0.0,
            "recall": 0.0,
            "map50": 0.0,
            "map50_95": 0.0,
        }

    order = np.argsort(-confidence)
    correct = correct[order]
    confidence = confidence[order]
    predicted_class = predicted_class[order]
    unique_classes, target_counts = np.unique(target_class, return_counts=True)
    precision_curves = np.zeros((len(unique_classes), 1000))
    recall_curves = np.zeros((len(unique_classes), 1000))
    average_precision = np.zeros((len(unique_classes), correct.shape[1]))
    x_values = np.linspace(0, 1, 1000)

    for class_index, class_id in enumerate(unique_classes):
        mask = predicted_class == class_id
        prediction_count = int(mask.sum())
        label_count = int(target_counts[class_index])
        if prediction_count == 0 or label_count == 0:
            continue
        false_positive_cumulative = (1 - correct[mask]).cumsum(0)
        true_positive_cumulative = correct[mask].cumsum(0)
        recall = true_positive_cumulative / (label_count + 1e-16)
        precision = true_positive_cumulative / (
            true_positive_cumulative + false_positive_cumulative
        )
        recall_curves[class_index] = np.interp(
            -x_values,
            -confidence[mask],
            recall[:, 0],
            left=0,
        )
        precision_curves[class_index] = np.interp(
            -x_values,
            -confidence[mask],
            precision[:, 0],
            left=1,
        )
        for iou_index in range(correct.shape[1]):
            average_precision[class_index, iou_index] = compute_ap(
                recall[:, iou_index],
                precision[:, iou_index],
            )

    f1 = (
        2
        * precision_curves
        * recall_curves
        / (precision_curves + recall_curves + 1e-16)
    )
    best_index = int(smooth(f1.mean(0), 0.1).argmax())
    precision = float(precision_curves[:, best_index].mean())
    recall = float(recall_curves[:, best_index].mean())
    return {
        "precision": precision,
        "recall": recall,
        "map50": float(average_precision[:, 0].mean()),
        "map50_95": float(average_precision.mean()),
    }


def box_center(box: np.ndarray) -> tuple[float, float]:
    return (
        float((box[0] + box[2]) / 2.0),
        float((box[1] + box[3]) / 2.0),
    )


def serialize_detections(detections: np.ndarray) -> list[dict[str, Any]]:
    return [
        {
            "box_xyxy": [float(value) for value in detection[:4]],
            "confidence": float(detection[4]),
            "class_id": int(detection[5]),
            "center": list(box_center(detection[:4])),
        }
        for detection in detections
    ]


def serialize_labels(labels: np.ndarray) -> list[dict[str, Any]]:
    return [
        {
            "class_id": int(label[0]),
            "box_xyxy": [float(value) for value in label[1:5]],
            "center": list(box_center(label[1:5])),
        }
        for label in labels
    ]


def deployment_image_metrics(
    detections: np.ndarray,
    labels: np.ndarray,
) -> dict[str, Any]:
    matches = match_boxes(detections, labels, 0.50)
    errors = []
    match_details = []
    for gt_index, pred_index, iou in matches:
        gt_center = box_center(labels[gt_index, 1:5])
        pred_center = box_center(detections[pred_index, :4])
        error = math.hypot(
            pred_center[0] - gt_center[0],
            pred_center[1] - gt_center[1],
        )
        errors.append(error)
        match_details.append(
            {
                "gt_index": gt_index,
                "pred_index": pred_index,
                "iou": iou,
                "center_error_px": error,
            }
        )
    true_positive = len(matches)
    false_positive = len(detections) - true_positive
    false_negative = len(labels) - true_positive
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "detection_count_error": abs(len(detections) - len(labels)),
        "mean_center_error_px": float(np.mean(errors)) if errors else None,
        "matched_center_errors_px": errors,
        "matches": match_details,
    }


def count_group(count: int) -> str:
    return str(count) if count < 5 else "5+"


def summarize_deployment(per_image: list[dict[str, Any]]) -> dict[str, Any]:
    true_positive = sum(item["true_positive"] for item in per_image)
    false_positive = sum(item["false_positive"] for item in per_image)
    false_negative = sum(item["false_negative"] for item in per_image)
    center_errors = [
        value
        for item in per_image
        for value in item["matched_center_errors_px"]
    ]
    background = [item for item in per_image if item["ground_truth_count"] == 0]
    multi_ball = [item for item in per_image if item["ground_truth_count"] >= 2]
    grouped: dict[str, Any] = {}
    for group_name in ("0", "1", "2", "3", "4", "5+"):
        group_items = [
            item
            for item in per_image
            if count_group(item["ground_truth_count"]) == group_name
        ]
        group_tp = sum(item["true_positive"] for item in group_items)
        group_fp = sum(item["false_positive"] for item in group_items)
        group_fn = sum(item["false_negative"] for item in group_items)
        group_errors = [
            value
            for item in group_items
            for value in item["matched_center_errors_px"]
        ]
        grouped[group_name] = {
            "images": len(group_items),
            "precision": group_tp / (group_tp + group_fp)
            if group_tp + group_fp
            else 0.0,
            "recall": group_tp / (group_tp + group_fn)
            if group_tp + group_fn
            else 0.0,
            "mean_false_positives": float(
                np.mean([item["false_positive"] for item in group_items])
            )
            if group_items
            else 0.0,
            "mean_false_negatives": float(
                np.mean([item["false_negative"] for item in group_items])
            )
            if group_items
            else 0.0,
            "mean_center_error_px": float(np.mean(group_errors))
            if group_errors
            else None,
        }
    return {
        "precision": true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0,
        "recall": true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0,
        "true_positives": true_positive,
        "false_positives": false_positive,
        "false_negatives": false_negative,
        "mean_detection_count_error": float(
            np.mean([item["detection_count_error"] for item in per_image])
        )
        if per_image
        else 0.0,
        "mean_center_error_px": float(np.mean(center_errors))
        if center_errors
        else None,
        "background_images": len(background),
        "background_false_positive_images": sum(
            item["false_positive"] > 0 for item in background
        ),
        "background_false_positive_rate": (
            sum(item["false_positive"] > 0 for item in background)
            / len(background)
            if background
            else 0.0
        ),
        "multi_ball_images": len(multi_ball),
        "multi_ball_miss_images": sum(
            item["false_negative"] > 0 for item in multi_ball
        ),
        "multi_ball_miss_rate": (
            sum(item["false_negative"] > 0 for item in multi_ball)
            / len(multi_ball)
            if multi_ball
            else 0.0
        ),
        "grouped_by_gt_count": grouped,
    }


def evaluate_backend(
    backend: Any,
    images: list[Path],
    names: dict[int, str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    if not images:
        raise RuntimeError("验证集没有图片")
    args.save_dir.mkdir(parents=True, exist_ok=True)
    first_image = Image.open(images[0]).convert("RGB")
    first_input, _, _ = letterbox_rgb(first_image, args.imgsz)
    backend.warmup(first_input)

    correct_rows = []
    confidences = []
    predicted_classes = []
    target_classes = []
    per_image = []
    raw_inspection: dict[str, Any] | None = None

    for image_index, image_path in enumerate(images):
        image = Image.open(image_path).convert("RGB")
        original_width, original_height = image.size
        labels = load_labels(image_path, original_width, original_height)
        input_u8, ratio, pad = letterbox_rgb(image, args.imgsz)
        outputs, inference_ms = backend.predict(input_u8)
        raw_prediction = adapt_raw_outputs(outputs)

        if raw_inspection is None:
            raw_inspection = {
                "image": str(image_path),
                "original_shape": [original_height, original_width, 3],
                "preprocessed_shape": list(input_u8.shape),
                "input_dtype": str(input_u8.dtype),
                "input_min": int(input_u8.min()),
                "input_max": int(input_u8.max()),
                "backend_input_dtype": str(backend.input_dtype),
                "backend_input_shape": list(backend.input_shape),
                "raw_output_count": len(outputs),
                "raw_output_shapes": [list(np.asarray(output).shape) for output in outputs],
                "raw_output_dtypes": [str(np.asarray(output).dtype) for output in outputs],
            }
            print(json.dumps(raw_inspection, ensure_ascii=False, indent=2))

        standard_start = time.perf_counter()
        standard_detections = nms_yolov5(
            raw_prediction,
            args.conf_thres,
            args.iou_thres,
            args.max_det,
        )
        standard_postprocess_ms = (time.perf_counter() - standard_start) * 1000.0
        standard_detections[:, :4] = scale_boxes_to_original(
            standard_detections[:, :4],
            (original_height, original_width),
            ratio,
            pad,
        )
        correct = process_batch_ap(
            standard_detections,
            labels,
            IOU_VECTOR,
        )
        correct_rows.append(correct)
        confidences.append(standard_detections[:, 4])
        predicted_classes.append(standard_detections[:, 5])
        target_classes.append(labels[:, 0])

        deployment_start = time.perf_counter()
        deployment_detections = nms_yolov5(
            raw_prediction,
            DEPLOY_CONF_THRESHOLD,
            DEPLOY_NMS_THRESHOLD,
            args.max_det,
        )
        deployment_postprocess_ms = (
            time.perf_counter() - deployment_start
        ) * 1000.0
        deployment_detections[:, :4] = scale_boxes_to_original(
            deployment_detections[:, :4],
            (original_height, original_width),
            ratio,
            pad,
        )
        image_metrics = deployment_image_metrics(
            deployment_detections,
            labels,
        )
        per_image.append(
            {
                "image": image_path.name,
                "image_path": str(image_path),
                "width": original_width,
                "height": original_height,
                "ground_truth_count": len(labels),
                "prediction_count": len(deployment_detections),
                "ground_truth": serialize_labels(labels),
                "predictions": serialize_detections(deployment_detections),
                "inference_ms": inference_ms,
                "standard_postprocess_ms": standard_postprocess_ms,
                "postprocess_ms": deployment_postprocess_ms,
                **image_metrics,
            }
        )
        print(
            f"[{backend.name}] {image_index + 1}/{len(images)} "
            f"{image_path.name} gt={len(labels)} pred={len(deployment_detections)} "
            f"infer={inference_ms:.3f}ms"
        )

    correct_array = (
        np.concatenate(correct_rows, axis=0)
        if correct_rows
        else np.zeros((0, len(IOU_VECTOR)), dtype=bool)
    )
    confidence_array = (
        np.concatenate(confidences)
        if confidences
        else np.zeros((0,), dtype=np.float32)
    )
    predicted_class_array = (
        np.concatenate(predicted_classes)
        if predicted_classes
        else np.zeros((0,), dtype=np.float32)
    )
    target_class_array = (
        np.concatenate(target_classes)
        if target_classes
        else np.zeros((0,), dtype=np.float32)
    )
    ap_metrics = calculate_ap_metrics(
        correct_array,
        confidence_array,
        predicted_class_array,
        target_class_array,
    )
    deployment = summarize_deployment(per_image)
    result = {
        "model": backend.name,
        "model_path": str(backend.path),
        "model_sha256": sha256_file(backend.path),
        "images": len(images),
        "instances": int(len(target_class_array)),
        "class_names": names,
        "settings": {
            "imgsz": args.imgsz,
            "standard_conf_threshold": args.conf_thres,
            "standard_nms_iou_threshold": args.iou_thres,
            "max_det": args.max_det,
            "evaluation_iou_thresholds": IOU_VECTOR.tolist(),
            "deployment_conf_threshold": DEPLOY_CONF_THRESHOLD,
            "deployment_nms_iou_threshold": DEPLOY_NMS_THRESHOLD,
        },
        "raw_inspection": raw_inspection,
        "standard_metrics": ap_metrics,
        "deployment_metrics": deployment,
        "mean_inference_ms": float(
            np.mean([item["inference_ms"] for item in per_image])
        ),
        "mean_postprocess_ms": float(
            np.mean([item["postprocess_ms"] for item in per_image])
        ),
        "per_image": per_image,
    }
    output_path = args.save_dir / "results" / f"{backend.name}_metrics.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已保存: {output_path}")
    return result


def prediction_array(items: list[dict[str, Any]]) -> np.ndarray:
    if not items:
        return np.zeros((0, 6), dtype=np.float32)
    return np.asarray(
        [
            [
                *item["box_xyxy"],
                item["confidence"],
                item["class_id"],
            ]
            for item in items
        ],
        dtype=np.float32,
    )


def label_array(items: list[dict[str, Any]]) -> np.ndarray:
    if not items:
        return np.zeros((0, 5), dtype=np.float32)
    return np.asarray(
        [[item["class_id"], *item["box_xyxy"]] for item in items],
        dtype=np.float32,
    )


def compare_prediction_sets(
    left_items: list[dict[str, Any]],
    right_items: list[dict[str, Any]],
) -> dict[str, Any]:
    left = prediction_array(left_items)
    right = prediction_array(right_items)
    if len(left) == 0 or len(right) == 0:
        return {
            "same_count": len(left) == len(right),
            "mean_matched_iou": None,
            "mean_center_difference_px": None,
            "mean_confidence_difference": None,
            "confidence_differences": [],
        }
    pseudo_labels = np.column_stack((left[:, 5], left[:, :4]))
    matches = match_boxes(right, pseudo_labels, 0.0)
    ious = []
    center_differences = []
    confidence_differences = []
    for left_index, right_index, iou in matches:
        left_center = box_center(left[left_index, :4])
        right_center = box_center(right[right_index, :4])
        ious.append(iou)
        center_differences.append(
            math.hypot(
                left_center[0] - right_center[0],
                left_center[1] - right_center[1],
            )
        )
        confidence_differences.append(
            float(right[right_index, 4] - left[left_index, 4])
        )
    return {
        "same_count": len(left) == len(right),
        "mean_matched_iou": float(np.mean(ious)) if ious else None,
        "mean_center_difference_px": float(np.mean(center_differences))
        if center_differences
        else None,
        "mean_confidence_difference": float(np.mean(confidence_differences))
        if confidence_differences
        else None,
        "confidence_differences": confidence_differences,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def draw_failure_overlay(
    image_path: Path,
    ground_truth: list[dict[str, Any]],
    predictions: dict[str, list[dict[str, Any]]],
    output_path: Path,
) -> None:
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    colors = {
        "ground_truth": (0, 255, 0),
        "pt": (0, 128, 255),
        "onnx": (255, 215, 0),
        "kmodel": (255, 0, 0),
    }
    gt_array = label_array(ground_truth)

    for index, item in enumerate(ground_truth):
        box = item["box_xyxy"]
        draw.rectangle(box, outline=colors["ground_truth"], width=2)
        draw.text((box[0], max(0, box[1] - 11)), f"GT{index}", fill=colors["ground_truth"])

    for model_name, items in predictions.items():
        detections = prediction_array(items)
        matches = match_boxes(detections, gt_array, 0.50)
        details = {
            pred_index: (iou, math.hypot(
                box_center(detections[pred_index, :4])[0]
                - box_center(gt_array[gt_index, 1:5])[0],
                box_center(detections[pred_index, :4])[1]
                - box_center(gt_array[gt_index, 1:5])[1],
            ))
            for gt_index, pred_index, iou in matches
        }
        for index, item in enumerate(items):
            box = item["box_xyxy"]
            color = colors[model_name]
            draw.rectangle(box, outline=color, width=2)
            center = item["center"]
            radius = 2
            draw.ellipse(
                (
                    center[0] - radius,
                    center[1] - radius,
                    center[0] + radius,
                    center[1] + radius,
                ),
                fill=color,
            )
            suffix = ""
            if index in details:
                suffix = f" I{details[index][0]:.2f} E{details[index][1]:.1f}"
            text = (
                f"{model_name} {item['confidence']:.2f} "
                f"({center[0]:.0f},{center[1]:.0f}){suffix}"
            )
            y_offset = {"pt": 0, "onnx": 11, "kmodel": 22}[model_name]
            draw.text((box[0], min(image.height - 11, box[1] + y_offset)), text, fill=color)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, quality=95)


def generate_plots(
    save_dir: Path,
    models: dict[str, dict[str, Any]],
    per_image_rows: list[dict[str, Any]],
    confidence_changes: list[float],
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("当前环境没有matplotlib，跳过图表生成。")
        return

    plots_dir = save_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    model_names = ["pt", "onnx", "kmodel"]
    display_names = ["PT", "ONNX", "KModel"]

    def bar_plot(values: list[float], title: str, ylabel: str, filename: str) -> None:
        plt.figure(figsize=(6, 4))
        bars = plt.bar(display_names, values, color=["#268bd2", "#b58900", "#dc322f"])
        plt.title(title)
        plt.ylabel(ylabel)
        for bar, value in zip(bars, values):
            plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
        plt.tight_layout()
        plt.savefig(plots_dir / filename, dpi=160)
        plt.close()

    for key, title, filename in (
        ("precision", "Precision comparison", "01_precision.png"),
        ("recall", "Recall comparison", "02_recall.png"),
        ("map50", "mAP50 comparison", "03_map50.png"),
        ("map50_95", "mAP50-95 comparison", "04_map50_95.png"),
    ):
        bar_plot(
            [models[name]["standard_metrics"][key] for name in model_names],
            title,
            key,
            filename,
        )
    bar_plot(
        [models[name]["mean_inference_ms"] for name in model_names],
        "Mean inference time",
        "ms/image",
        "05_inference_time.png",
    )

    plt.figure(figsize=(7, 4))
    for name, color in zip(model_names, ["#268bd2", "#b58900", "#dc322f"]):
        errors = [
            error
            for item in models[name]["per_image"]
            for error in item["matched_center_errors_px"]
        ]
        if errors:
            plt.hist(errors, bins=20, alpha=0.45, label=name, color=color)
    plt.xlabel("center error (px)")
    plt.ylabel("matched detections")
    plt.legend()
    plt.tight_layout()
    plt.savefig(plots_dir / "06_center_error_distribution.png", dpi=160)
    plt.close()

    image_indices = np.arange(len(per_image_rows))
    for field, title, filename in (
        ("pt_onnx_count_difference", "PT - ONNX detection count", "07_pt_onnx_count_difference.png"),
        ("pt_kmodel_count_difference", "PT - KModel detection count", "08_pt_kmodel_count_difference.png"),
    ):
        plt.figure(figsize=(10, 4))
        plt.bar(image_indices, [row[field] for row in per_image_rows])
        plt.xlabel("validation image index")
        plt.ylabel("count difference")
        plt.title(title)
        plt.tight_layout()
        plt.savefig(plots_dir / filename, dpi=160)
        plt.close()

    plt.figure(figsize=(7, 4))
    if confidence_changes:
        plt.hist(confidence_changes, bins=20, color="#dc322f", alpha=0.75)
    plt.xlabel("KModel confidence - PT confidence")
    plt.ylabel("matched detections")
    plt.title("KModel confidence change relative to PT")
    plt.tight_layout()
    plt.savefig(plots_dir / "09_kmodel_confidence_change.png", dpi=160)
    plt.close()

    groups = ["0", "1", "2", "3", "4", "5+"]
    x_values = np.arange(len(groups))
    width = 0.25
    plt.figure(figsize=(8, 4))
    for index, name in enumerate(model_names):
        values = [
            models[name]["deployment_metrics"]["grouped_by_gt_count"][group]["recall"]
            for group in groups
        ]
        plt.bar(x_values + (index - 1) * width, values, width=width, label=name)
    plt.xticks(x_values, groups)
    plt.xlabel("ground-truth ball count")
    plt.ylabel("recall")
    plt.legend()
    plt.tight_layout()
    plt.savefig(plots_dir / "10_recall_by_ball_count.png", dpi=160)
    plt.close()

    bar_plot(
        [
            models[name]["deployment_metrics"]["background_false_positive_images"]
            for name in model_names
        ],
        "Background images with false positives",
        "images",
        "11_background_false_positives.png",
    )
    bar_plot(
        [
            models[name]["deployment_metrics"]["multi_ball_miss_images"]
            for name in model_names
        ],
        "Multi-ball images with misses",
        "images",
        "12_multi_ball_misses.png",
    )


def diagnose(models: dict[str, dict[str, Any]]) -> str:
    pt = models["pt"]["standard_metrics"]
    onnx = models["onnx"]["standard_metrics"]
    kmodel = models["kmodel"]["standard_metrics"]
    pt_onnx_drop = pt["map50"] - onnx["map50"]
    onnx_kmodel_drop = onnx["map50"] - kmodel["map50"]
    if pt["map50"] < 0.70:
        return (
            "情况D：PT本身表现不足，主要问题更可能位于数据、标注、"
            "224输入分辨率、训练方式或模型容量。"
        )
    if pt_onnx_drop > 0.02:
        return (
            "情况B：PT正常但ONNX开始明显下降，优先检查ONNX导出、"
            "opset、simplify和输出解析。"
        )
    if abs(pt_onnx_drop) <= 0.02 and onnx_kmodel_drop > 0.02:
        return (
            "情况A：PT与ONNX基本一致而KModel明显下降，问题集中在量化、"
            "校准集、nncase转换或KModel预处理。"
        )
    return (
        "情况C（离线部分）：PT、ONNX、KModel离线指标接近。若K230现场仍明显较差，"
        "应检查摄像头域差异、曝光、白平衡、运动模糊、ROI映射、板端预处理和阈值。"
    )


def merge_existing(save_dir: Path) -> None:
    results_dir = save_dir / "results"
    models = {}
    for name in ("pt", "onnx", "kmodel"):
        path = results_dir / f"{name}_metrics.json"
        if not path.exists():
            raise FileNotFoundError(f"缺少后端结果: {path}")
        models[name] = json.loads(path.read_text(encoding="utf-8"))

    indices = {
        name: {item["image"]: item for item in result["per_image"]}
        for name, result in models.items()
    }
    image_names = sorted(set(indices["pt"]) & set(indices["onnx"]) & set(indices["kmodel"]))
    per_image_rows = []
    difference_rows = []
    failure_rows = []
    confidence_changes = []

    failure_directories = {
        name: save_dir / "failures" / name
        for name in (
            "pt_failures",
            "onnx_failures",
            "kmodel_failures",
            "quantization_regressions",
            "all_models_fail",
            "background_false_positives",
            "multi_ball_misses",
        )
    }

    for image_name in image_names:
        pt = indices["pt"][image_name]
        onnx = indices["onnx"][image_name]
        kmodel = indices["kmodel"][image_name]
        pt_onnx = compare_prediction_sets(pt["predictions"], onnx["predictions"])
        pt_kmodel = compare_prediction_sets(pt["predictions"], kmodel["predictions"])
        onnx_kmodel = compare_prediction_sets(onnx["predictions"], kmodel["predictions"])
        confidence_changes.extend(pt_kmodel["confidence_differences"])

        per_image_rows.append(
            {
                "image": image_name,
                "ground_truth_count": pt["ground_truth_count"],
                "pt_prediction_count": pt["prediction_count"],
                "onnx_prediction_count": onnx["prediction_count"],
                "kmodel_prediction_count": kmodel["prediction_count"],
                "pt_true_positive": pt["true_positive"],
                "pt_false_positive": pt["false_positive"],
                "pt_false_negative": pt["false_negative"],
                "onnx_true_positive": onnx["true_positive"],
                "onnx_false_positive": onnx["false_positive"],
                "onnx_false_negative": onnx["false_negative"],
                "kmodel_true_positive": kmodel["true_positive"],
                "kmodel_false_positive": kmodel["false_positive"],
                "kmodel_false_negative": kmodel["false_negative"],
                "pt_mean_center_error_px": pt["mean_center_error_px"],
                "onnx_mean_center_error_px": onnx["mean_center_error_px"],
                "kmodel_mean_center_error_px": kmodel["mean_center_error_px"],
                "pt_inference_ms": pt["inference_ms"],
                "onnx_inference_ms": onnx["inference_ms"],
                "kmodel_inference_ms": kmodel["inference_ms"],
                "pt_postprocess_ms": pt["postprocess_ms"],
                "onnx_postprocess_ms": onnx["postprocess_ms"],
                "kmodel_postprocess_ms": kmodel["postprocess_ms"],
                "pt_onnx_count_difference": pt["prediction_count"] - onnx["prediction_count"],
                "pt_kmodel_count_difference": pt["prediction_count"] - kmodel["prediction_count"],
            }
        )
        difference_rows.append(
            {
                "image": image_name,
                "pt_onnx_same_count": pt_onnx["same_count"],
                "pt_kmodel_same_count": pt_kmodel["same_count"],
                "onnx_kmodel_same_count": onnx_kmodel["same_count"],
                "pt_onnx_mean_iou": pt_onnx["mean_matched_iou"],
                "pt_kmodel_mean_iou": pt_kmodel["mean_matched_iou"],
                "onnx_kmodel_mean_iou": onnx_kmodel["mean_matched_iou"],
                "pt_onnx_mean_center_diff_px": pt_onnx["mean_center_difference_px"],
                "pt_kmodel_mean_center_diff_px": pt_kmodel["mean_center_difference_px"],
                "onnx_kmodel_mean_center_diff_px": onnx_kmodel["mean_center_difference_px"],
                "pt_onnx_mean_conf_diff": pt_onnx["mean_confidence_difference"],
                "pt_kmodel_mean_conf_diff": pt_kmodel["mean_confidence_difference"],
                "onnx_kmodel_mean_conf_diff": onnx_kmodel["mean_confidence_difference"],
                "pt_correct_kmodel_miss": pt["false_negative"] == 0 and kmodel["false_negative"] > 0,
                "pt_clean_kmodel_false_positive": pt["false_positive"] == 0 and kmodel["false_positive"] > 0,
                "onnx_correct_kmodel_wrong": (
                    onnx["false_positive"] == 0
                    and onnx["false_negative"] == 0
                    and (kmodel["false_positive"] > 0 or kmodel["false_negative"] > 0)
                ),
            }
        )

        categories = []
        if pt["false_positive"] or pt["false_negative"]:
            categories.append("pt_failures")
        if onnx["false_positive"] or onnx["false_negative"]:
            categories.append("onnx_failures")
        if kmodel["false_positive"] or kmodel["false_negative"]:
            categories.append("kmodel_failures")
        if (
            pt["false_positive"] == 0
            and pt["false_negative"] == 0
            and (kmodel["false_positive"] > 0 or kmodel["false_negative"] > 0)
        ):
            categories.append("quantization_regressions")
        if all(
            item["false_positive"] > 0 or item["false_negative"] > 0
            for item in (pt, onnx, kmodel)
        ):
            categories.append("all_models_fail")
        if pt["ground_truth_count"] == 0 and any(
            item["false_positive"] > 0 for item in (pt, onnx, kmodel)
        ):
            categories.append("background_false_positives")
        if pt["ground_truth_count"] >= 2 and any(
            item["false_negative"] > 0 for item in (pt, onnx, kmodel)
        ):
            categories.append("multi_ball_misses")

        if categories:
            failure_rows.append(
                {
                    "image": image_name,
                    "categories": "|".join(categories),
                    "ground_truth_count": pt["ground_truth_count"],
                    "pt_fp": pt["false_positive"],
                    "pt_fn": pt["false_negative"],
                    "onnx_fp": onnx["false_positive"],
                    "onnx_fn": onnx["false_negative"],
                    "kmodel_fp": kmodel["false_positive"],
                    "kmodel_fn": kmodel["false_negative"],
                }
            )
            source_image = Path(pt["image_path"])
            temp_overlay = save_dir / "failures" / f"_overlay_{image_name}"
            draw_failure_overlay(
                source_image,
                pt["ground_truth"],
                {
                    "pt": pt["predictions"],
                    "onnx": onnx["predictions"],
                    "kmodel": kmodel["predictions"],
                },
                temp_overlay,
            )
            for category in categories:
                destination = failure_directories[category] / image_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(temp_overlay, destination)
            temp_overlay.unlink(missing_ok=True)

    per_image_fields = list(per_image_rows[0].keys()) if per_image_rows else []
    difference_fields = list(difference_rows[0].keys()) if difference_rows else []
    failure_fields = list(failure_rows[0].keys()) if failure_rows else [
        "image",
        "categories",
        "ground_truth_count",
        "pt_fp",
        "pt_fn",
        "onnx_fp",
        "onnx_fn",
        "kmodel_fp",
        "kmodel_fn",
    ]
    write_csv(results_dir / "per_image_results.csv", per_image_rows, per_image_fields)
    write_csv(
        results_dir / "model_output_differences.csv",
        difference_rows,
        difference_fields,
    )
    write_csv(results_dir / "failures.csv", failure_rows, failure_fields)

    summary_rows = []
    for name in ("pt", "onnx", "kmodel"):
        result = models[name]
        summary_rows.append(
            {
                "model": name,
                "images": result["images"],
                "instances": result["instances"],
                "precision": result["standard_metrics"]["precision"],
                "recall": result["standard_metrics"]["recall"],
                "map50": result["standard_metrics"]["map50"],
                "map50_95": result["standard_metrics"]["map50_95"],
                "mean_inference_ms": result["mean_inference_ms"],
                "mean_postprocess_ms": result["mean_postprocess_ms"],
                "false_positives": result["deployment_metrics"]["false_positives"],
                "false_negatives": result["deployment_metrics"]["false_negatives"],
                "mean_center_error_px": result["deployment_metrics"]["mean_center_error_px"],
            }
        )
    write_csv(
        results_dir / "all_models_summary.csv",
        summary_rows,
        list(summary_rows[0].keys()),
    )
    generate_plots(save_dir, models, per_image_rows, confidence_changes)

    diagnosis = diagnose(models)
    report_lines = [
        "# PT / ONNX / KModel 精度一致性评测报告",
        "",
        "## 评测设置",
        "",
        "- 验证集：完全相同的80张ROI图片（包含空标签背景图）",
        "- 标准AP：imgsz=224, conf=0.001, NMS IoU=0.60, max_det=5",
        "- 部署统计：conf=0.25, NMS IoU=0.45, max_det=5",
        "- 匹配规则：IoU >= 0.50的一对一匹配",
        "",
        "## 三模型汇总",
        "",
        "| model | P | R | mAP50 | mAP50-95 | infer ms | post ms | FP | FN | center error px |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        center = row["mean_center_error_px"]
        report_lines.append(
            f"| {row['model']} | {row['precision']:.6f} | {row['recall']:.6f} | "
            f"{row['map50']:.6f} | {row['map50_95']:.6f} | "
            f"{row['mean_inference_ms']:.3f} | {row['mean_postprocess_ms']:.3f} | "
            f"{row['false_positives']} | {row['false_negatives']} | "
            f"{center:.3f} |" if center is not None else
            f"| {row['model']} | {row['precision']:.6f} | {row['recall']:.6f} | "
            f"{row['map50']:.6f} | {row['map50_95']:.6f} | "
            f"{row['mean_inference_ms']:.3f} | {row['mean_postprocess_ms']:.3f} | "
            f"{row['false_positives']} | {row['false_negatives']} | N/A |"
        )
    report_lines.extend(
        [
            "",
            "## 自动诊断",
            "",
            diagnosis,
            "",
            "## 失败案例",
            "",
            f"- 失败图片记录数：{len(failure_rows)}",
            f"- 量化回退图片：{sum('quantization_regressions' in row['categories'] for row in failure_rows)}",
            f"- 背景误检图片：{sum('background_false_positives' in row['categories'] for row in failure_rows)}",
            f"- 多球漏检图片：{sum('multi_ball_misses' in row['categories'] for row in failure_rows)}",
            "",
            "详细逐图结果见 `results/per_image_results.csv`，模型差异见 "
            "`results/model_output_differences.csv`，标注图见 `failures/`。",
            "",
            "## K230现场层说明",
            "",
            "本报告能够定位 PT → ONNX → KModel 的离线变化。"
            "KModel → K230实际部署还需要板端同一批图片回放结果或现场日志，"
            "不能用Simulator结果冒充板端实测。",
        ]
    )
    (save_dir / "EVAL_REPORT.md").write_text(
        "\n".join(report_lines) + "\n",
        encoding="utf-8",
    )
    print(f"合并完成: {save_dir / 'EVAL_REPORT.md'}")


def main() -> None:
    args = parse_args()
    args.data = args.data.resolve()
    args.save_dir = args.save_dir.resolve()
    args.save_dir.mkdir(parents=True, exist_ok=True)
    images, names = resolve_dataset(args.data)
    if args.limit is not None:
        images = images[: args.limit]

    backend_specs = [
        ("pt", args.pt, PTBackend),
        ("onnx", args.onnx, ONNXBackend),
        ("kmodel", args.kmodel, KModelBackend),
    ]
    ran_backend = False
    for _name, path, backend_class in backend_specs:
        if path is None:
            continue
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        backend = backend_class(
            path,
            args.device,
            args.yolov5_root.resolve() if args.yolov5_root else None,
        )
        evaluate_backend(backend, images, names, args)
        ran_backend = True

    if args.merge_existing:
        merge_existing(args.save_dir)
    elif not ran_backend:
        raise ValueError("至少指定--pt、--onnx、--kmodel之一，或使用--merge-existing")


if __name__ == "__main__":
    main()
