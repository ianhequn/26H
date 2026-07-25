from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass(frozen=True)
class RoiStats:
    images: int = 0
    boxes_in: int = 0
    boxes_out: int = 0
    empty_labels: int = 0

    def add(self, *, images: int = 0, boxes_in: int = 0, boxes_out: int = 0, empty_labels: int = 0) -> "RoiStats":
        return RoiStats(
            self.images + images,
            self.boxes_in + boxes_in,
            self.boxes_out + boxes_out,
            self.empty_labels + empty_labels,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--size", default=224, type=int)
    parser.add_argument("--min-keep-ratio", default=0.35, type=float)
    parser.add_argument("--clean", action="store_true")
    return parser.parse_args()


def yolo_to_xyxy(row: str, width: int, height: int) -> tuple[int, float, float, float, float] | None:
    parts = row.split()
    if len(parts) != 5:
        return None
    cls = int(float(parts[0]))
    x, y, w, h = [float(v) for v in parts[1:]]
    cx = x * width
    cy = y * height
    bw = w * width
    bh = h * height
    return cls, cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2


def clip_box_to_roi(
    box: tuple[int, float, float, float, float],
    roi_x: float,
    roi_y: float,
    roi_size: float,
    min_keep_ratio: float,
) -> str | None:
    cls, x1, y1, x2, y2 = box
    original_area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if original_area <= 0:
        return None

    ix1 = max(x1, roi_x)
    iy1 = max(y1, roi_y)
    ix2 = min(x2, roi_x + roi_size)
    iy2 = min(y2, roi_y + roi_size)
    inter_w = max(0.0, ix2 - ix1)
    inter_h = max(0.0, iy2 - iy1)
    inter_area = inter_w * inter_h
    if inter_area <= 0 or inter_area / original_area < min_keep_ratio:
        return None

    nx1 = (ix1 - roi_x) / roi_size
    ny1 = (iy1 - roi_y) / roi_size
    nx2 = (ix2 - roi_x) / roi_size
    ny2 = (iy2 - roi_y) / roi_size

    cx = (nx1 + nx2) / 2
    cy = (ny1 + ny2) / 2
    bw = nx2 - nx1
    bh = ny2 - ny1
    if bw <= 0 or bh <= 0:
        return None
    return f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"


def process_split(source: Path, output: Path, split: str, out_size: int, min_keep_ratio: float) -> RoiStats:
    src_images = source / "images" / split
    src_labels = source / "labels" / split
    dst_images = output / "images" / split
    dst_labels = output / "labels" / split
    dst_images.mkdir(parents=True, exist_ok=True)
    dst_labels.mkdir(parents=True, exist_ok=True)

    stats = RoiStats()
    for image_path in sorted(p for p in src_images.iterdir() if p.suffix.lower() in IMAGE_EXTS):
        label_path = src_labels / f"{image_path.stem}.txt"
        dst_image_path = dst_images / image_path.name
        dst_label_path = dst_labels / f"{image_path.stem}.txt"

        with Image.open(image_path) as image:
            image = image.convert("RGB")
            width, height = image.size
            roi_size = min(width, height)
            roi_x = (width - roi_size) / 2
            roi_y = (height - roi_size) / 2
            roi = image.crop((int(roi_x), int(roi_y), int(roi_x + roi_size), int(roi_y + roi_size)))
            roi = roi.resize((out_size, out_size), Image.Resampling.LANCZOS)
            roi.save(dst_image_path, quality=95)

        label_rows = []
        if label_path.exists():
            label_rows = [line.strip() for line in label_path.read_text(encoding="utf-8").splitlines() if line.strip()]

        new_rows = []
        dropped = 0
        for row in label_rows:
            box = yolo_to_xyxy(row, width, height)
            if box is None:
                dropped += 1
                continue
            clipped = clip_box_to_roi(box, roi_x, roi_y, roi_size, min_keep_ratio)
            if clipped is None:
                dropped += 1
                continue
            new_rows.append(clipped)

        dst_label_path.write_text("\n".join(new_rows) + ("\n" if new_rows else ""), encoding="utf-8")
        stats = stats.add(
            images=1,
            boxes_in=len(new_rows),
            boxes_out=dropped,
            empty_labels=1 if not new_rows else 0,
        )
    return stats


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if args.clean and output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    total = RoiStats()
    for split in ("train", "val"):
        split_stats = process_split(source, output, split, args.size, args.min_keep_ratio)
        total = total.add(
            images=split_stats.images,
            boxes_in=split_stats.boxes_in,
            boxes_out=split_stats.boxes_out,
            empty_labels=split_stats.empty_labels,
        )
        print(
            f"{split}: images={split_stats.images}, kept_boxes={split_stats.boxes_in}, "
            f"dropped_boxes={split_stats.boxes_out}, empty_labels={split_stats.empty_labels}"
        )

    yaml_text = (
        f'path: "{output.as_posix()}"\n'
        "train: images/train\n"
        "val: images/val\n"
        "names:\n"
        "  0: steel_ball\n"
    )
    (output / "steel_ball.yaml").write_text(yaml_text, encoding="utf-8")
    print(
        f"total: images={total.images}, kept_boxes={total.boxes_in}, "
        f"dropped_boxes={total.boxes_out}, empty_labels={total.empty_labels}"
    )
    print(f"dataset: {output}")


if __name__ == "__main__":
    main()

