from __future__ import annotations

import argparse
import csv
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


@dataclass(frozen=True)
class ImageRecord:
    path: Path
    label_count: int
    brightness: float
    contrast: float
    sharpness: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="创建可重复的 KModel 平衡量化校准集。")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--negative-count", type=int, default=20)
    return parser.parse_args()


def inspect_image(image_path: Path, label_path: Path) -> ImageRecord:
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"无法读取图片: {image_path}")
    labels = []
    if label_path.is_file():
        labels = [line for line in label_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ImageRecord(
        path=image_path,
        label_count=len(labels),
        brightness=float(image.mean()),
        contrast=float(image.std()),
        sharpness=float(cv2.Laplacian(image, cv2.CV_64F).var()),
    )


def diverse_sample(records: list[ImageRecord], count: int) -> list[ImageRecord]:
    if count >= len(records):
        return sorted(records, key=lambda item: item.path.name)
    features = np.asarray(
        [[item.brightness, item.contrast, np.log1p(item.sharpness)] for item in records],
        dtype=np.float64,
    )
    span = np.ptp(features, axis=0)
    normalized = (features - features.min(axis=0)) / np.where(span > 0, span, 1.0)

    selected = [int(np.argmax(np.linalg.norm(normalized - normalized.mean(axis=0), axis=1)))]
    min_distance = np.linalg.norm(normalized - normalized[selected[0]], axis=1)
    while len(selected) < count:
        min_distance[selected] = -1.0
        next_index = int(np.argmax(min_distance))
        selected.append(next_index)
        min_distance = np.minimum(
            min_distance,
            np.linalg.norm(normalized - normalized[next_index], axis=1),
        )
    return [records[index] for index in selected]


def main() -> None:
    args = parse_args()
    dataset = args.dataset.resolve()
    output = args.output.resolve()
    image_dir = dataset / "images" / "train"
    label_dir = dataset / "labels" / "train"
    if not image_dir.is_dir() or not label_dir.is_dir():
        raise FileNotFoundError(f"训练集目录不完整: {dataset}")
    if args.count <= 0 or args.negative_count < 0 or args.negative_count > args.count:
        raise ValueError("校准图片数量参数无效")

    records = [
        inspect_image(image_path, label_dir / f"{image_path.stem}.txt")
        for image_path in sorted(image_dir.iterdir())
        if image_path.is_file() and image_path.suffix.lower() in IMAGE_SUFFIXES
    ]
    negatives = [item for item in records if item.label_count == 0]
    rare_positives = [item for item in records if item.label_count in {1, 2, 3, 5}]
    common_positives = [item for item in records if item.label_count >= 4 and item.label_count != 5]

    selected = diverse_sample(negatives, min(args.negative_count, len(negatives)))
    remaining = args.count - len(selected)
    selected.extend(diverse_sample(rare_positives, min(remaining, len(rare_positives))))
    remaining = args.count - len(selected)
    selected.extend(diverse_sample(common_positives, min(remaining, len(common_positives))))
    remaining = args.count - len(selected)
    if remaining:
        unused = [item for item in records if item not in selected]
        selected.extend(diverse_sample(unused, min(remaining, len(unused))))
    if len(selected) != args.count:
        raise RuntimeError(f"只能选择 {len(selected)} 张，目标为 {args.count} 张")

    output.mkdir(parents=True, exist_ok=True)
    for old_file in output.glob("calib_*"):
        if old_file.is_file():
            old_file.unlink()

    manifest_path = output / "manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8-sig") as manifest_file:
        writer = csv.writer(manifest_file)
        writer.writerow(["calibration_file", "source_file", "label_count", "brightness", "contrast", "sharpness"])
        for index, record in enumerate(selected):
            target = output / f"calib_{index:03d}{record.path.suffix.lower()}"
            shutil.copy2(record.path, target)
            writer.writerow(
                [
                    target.name,
                    record.path.name,
                    record.label_count,
                    f"{record.brightness:.4f}",
                    f"{record.contrast:.4f}",
                    f"{record.sharpness:.4f}",
                ]
            )

    distribution: dict[int, int] = {}
    for record in selected:
        distribution[record.label_count] = distribution.get(record.label_count, 0) + 1
    print(f"已生成 {len(selected)} 张校准图片: {output}")
    print(f"标签数量分布: {dict(sorted(distribution.items()))}")
    print(f"清单: {manifest_path}")


if __name__ == "__main__":
    main()
