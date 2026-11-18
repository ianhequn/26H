"""在 PC 上批量验证样图并生成标注图。"""

import argparse
import json
from pathlib import Path

import cv2

from detector import (
    build_uart_packet,
    detect_green_rail_and_ball,
    draw_result,
)


def result_for_json(result):
    data = {
        "valid": result["valid"],
        "error": result["error"],
        "rail_center": None,
        "rail_bbox": None,
        "ball_center": None,
        "ball_bbox": None,
    }
    if result["rail"] is not None:
        data["rail_center"] = result["rail"]["center"]
        data["rail_bbox"] = result["rail"]["bbox"]
    if result["ball"] is not None:
        data["ball_center"] = result["ball"]["center"]
        data["ball_bbox"] = result["ball"]["bbox"]
        data["ball_circularity"] = round(
            result["ball"]["circularity"],
            4,
        )
    return data


def process_image(image_path, output_dir):
    frame = cv2.imread(str(image_path))
    if frame is None:
        raise RuntimeError("无法读取图片: %s" % image_path)

    result, green_mask, ball_mask = detect_green_rail_and_ball(frame)
    annotated = draw_result(frame.copy(), result)

    output_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(
        str(output_dir / (image_path.stem + "_annotated.jpg")),
        annotated,
    )
    cv2.imwrite(
        str(output_dir / (image_path.stem + "_green_mask.png")),
        green_mask,
    )
    if ball_mask is not None:
        cv2.imwrite(
            str(output_dir / (image_path.stem + "_ball_mask.png")),
            ball_mask,
        )

    print("%s -> %s" % (
        image_path.name,
        build_uart_packet(result).strip(),
    ))
    return result_for_json(result)


def main():
    parser = argparse.ArgumentParser(
        description="验证绿色槽体与金属小球 OpenCV 检测",
    )
    parser.add_argument(
        "images",
        nargs="*",
        type=Path,
        help="待检测图片；不传则读取 test_images/*.jpg",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="调试结果目录",
    )
    args = parser.parse_args()

    images = args.images
    if not images:
        images = sorted(Path("test_images").glob("*.jpg"))
    if not images:
        raise SystemExit("没有找到待检测图片")

    report = {}
    for image_path in images:
        report[image_path.name] = process_image(
            image_path,
            args.output,
        )

    report_path = args.output / "results.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("结果已写入: %s" % report_path)


if __name__ == "__main__":
    main()
