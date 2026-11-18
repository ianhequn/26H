"""绿色槽体 + 金属小球检测器。

同一份代码同时用于：
1. Windows/PC 上用 OpenCV 验证图片；
2. K230 CanMV 固件上的 cv2 + ulab.numpy 实时检测。
"""

import math

import cv2

try:
    from ulab import numpy as np
except ImportError:
    import numpy as np


# OpenCV HSV 范围：H 取值 0~179。
# 样图中的绿色槽体落在此范围内；换光照后优先调 S_MIN 和 V_MIN。
H_MIN = 35
H_MAX = 95
S_MIN = 60
V_MIN = 35

# 槽体几何约束：必须是画面中的长条绿色区域。
RAIL_MIN_WIDTH_RATIO = 0.35
RAIL_MIN_AREA_RATIO = 0.02
RAIL_MIN_ASPECT = 3.5
RAIL_MAX_HEIGHT_RATIO = 0.45

# 只在槽体内部的中间高度带寻找小球，排除上下边缘和支架。
BALL_BAND_TOP = 0.18
BALL_BAND_BOTTOM = 0.82
BALL_SIDE_MARGIN = 0.02
BALL_MIN_DIAMETER_RATIO = 0.12
BALL_MAX_DIAMETER_RATIO = 0.45
BALL_MIN_ASPECT = 0.65
BALL_MAX_ASPECT = 1.45
BALL_MIN_CIRCULARITY = 0.55


def _odd_at_least(value, minimum):
    value = max(int(value), int(minimum))
    if value % 2 == 0:
        value += 1
    return value


def _find_external_contours(binary):
    result = cv2.findContours(
        binary,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    # OpenCV 3 返回 3 项，OpenCV 4/5 与 K230 CanMV 返回 2 项。
    if len(result) == 3:
        return result[1]
    return result[0]


def _green_mask(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lower = np.array([H_MIN, S_MIN, V_MIN], dtype=np.uint8)
    upper = np.array([H_MAX, 255, 255], dtype=np.uint8)
    return cv2.inRange(hsv, lower, upper)


def _detect_rail(frame, raw_green_mask):
    frame_h, frame_w = frame.shape[:2]

    close_w = _odd_at_least(frame_w / 90, 7)
    close_h = _odd_at_least(frame_h / 150, 3)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (close_w, close_h),
    )
    merged = cv2.morphologyEx(
        raw_green_mask,
        cv2.MORPH_CLOSE,
        kernel,
    )

    best = None
    best_area = 0.0

    for contour in _find_external_contours(merged):
        area = float(cv2.contourArea(contour))
        x, y, w, h = cv2.boundingRect(contour)
        if h <= 0:
            continue

        aspect = float(w) / float(h)
        if w < frame_w * RAIL_MIN_WIDTH_RATIO:
            continue
        if area < frame_w * frame_h * RAIL_MIN_AREA_RATIO:
            continue
        if aspect < RAIL_MIN_ASPECT:
            continue
        if h > frame_h * RAIL_MAX_HEIGHT_RATIO:
            continue

        if area > best_area:
            best_area = area
            best = {
                "bbox": (int(x), int(y), int(w), int(h)),
                "center": (
                    int(round(x + w / 2.0)),
                    int(round(y + h / 2.0)),
                ),
                "area": area,
                "aspect": aspect,
            }

    return best, merged


def _suppress_outside_search_band(mask, width, height):
    top = int(height * BALL_BAND_TOP)
    bottom = int(height * BALL_BAND_BOTTOM)
    left = int(width * BALL_SIDE_MARGIN)
    right = int(width * (1.0 - BALL_SIDE_MARGIN))

    if top > 0:
        cv2.rectangle(mask, (0, 0), (width - 1, top - 1), 0, -1)
    if bottom < height:
        cv2.rectangle(
            mask,
            (0, bottom),
            (width - 1, height - 1),
            0,
            -1,
        )
    if left > 0:
        cv2.rectangle(mask, (0, 0), (left - 1, height - 1), 0, -1)
    if right < width:
        cv2.rectangle(
            mask,
            (right, 0),
            (width - 1, height - 1),
            0,
            -1,
        )


def _detect_ball(raw_green_mask, rail):
    rail_x, rail_y, rail_w, rail_h = rail["bbox"]
    rail_green = raw_green_mask[
        rail_y:rail_y + rail_h,
        rail_x:rail_x + rail_w,
    ]

    # 金属球不是绿色，因此先取反，再只保留槽体内部搜索带。
    candidates_mask = cv2.bitwise_not(rail_green)
    _suppress_outside_search_band(
        candidates_mask,
        rail_w,
        rail_h,
    )

    open_size = _odd_at_least(rail_h / 55, 3)
    close_size = _odd_at_least(rail_h / 35, 3)
    open_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (open_size, open_size),
    )
    close_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (close_size, close_size),
    )
    candidates_mask = cv2.morphologyEx(
        candidates_mask,
        cv2.MORPH_OPEN,
        open_kernel,
    )
    candidates_mask = cv2.morphologyEx(
        candidates_mask,
        cv2.MORPH_CLOSE,
        close_kernel,
    )

    min_diameter = max(
        6,
        int(rail_h * BALL_MIN_DIAMETER_RATIO),
    )
    max_diameter = max(
        min_diameter + 2,
        int(rail_h * BALL_MAX_DIAMETER_RATIO),
    )

    best = None
    best_score = -999.0

    for contour in _find_external_contours(candidates_mask):
        area = float(cv2.contourArea(contour))
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 0 or h <= 0:
            continue

        diameter = max(w, h)
        if diameter < min_diameter or diameter > max_diameter:
            continue

        aspect = float(w) / float(h)
        if aspect < BALL_MIN_ASPECT or aspect > BALL_MAX_ASPECT:
            continue

        perimeter = float(cv2.arcLength(contour, True))
        if perimeter <= 0.0:
            continue

        circularity = 4.0 * math.pi * area / (perimeter * perimeter)
        if circularity < BALL_MIN_CIRCULARITY:
            continue

        # 最接近圆、尺寸最接近槽高 1/4 的候选优先。
        size_ratio = float(diameter) / float(rail_h)
        score = (
            2.0 * circularity
            - abs(aspect - 1.0)
            - abs(size_ratio - 0.25)
        )

        if score > best_score:
            frame_x = rail_x + x
            frame_y = rail_y + y
            best_score = score
            best = {
                "bbox": (
                    int(frame_x),
                    int(frame_y),
                    int(w),
                    int(h),
                ),
                "center": (
                    int(round(frame_x + w / 2.0)),
                    int(round(frame_y + h / 2.0)),
                ),
                "area": area,
                "aspect": aspect,
                "circularity": circularity,
                "score": score,
            }

    return best, candidates_mask


def detect_green_rail_and_ball(frame):
    """返回检测结果以及两张调试掩膜。

    坐标定义：
        dx = ball_x - rail_x
        dy = ball_y - rail_y

    所以 dx < 0 表示小球在槽体中点左侧，dx > 0 表示在右侧。
    """
    raw_green = _green_mask(frame)
    rail, merged_green = _detect_rail(frame, raw_green)

    result = {
        "valid": False,
        "rail": rail,
        "ball": None,
        "error": None,
    }

    if rail is None:
        return result, merged_green, None

    ball, ball_mask = _detect_ball(raw_green, rail)
    result["ball"] = ball
    if ball is None:
        return result, merged_green, ball_mask

    rail_x, rail_y = rail["center"]
    ball_x, ball_y = ball["center"]
    result["valid"] = True
    result["error"] = (
        int(ball_x - rail_x),
        int(ball_y - rail_y),
    )
    return result, merged_green, ball_mask


def build_uart_packet(result):
    """构造固定字段 ASCII 串口包，便于 STM32/上位机按逗号解析。"""
    if not result["valid"]:
        return "GB,0,0,0,-1,-1,-1,-1\n"

    dx, dy = result["error"]
    ball_x, ball_y = result["ball"]["center"]
    rail_x, rail_y = result["rail"]["center"]
    return "GB,1,%d,%d,%d,%d,%d,%d\n" % (
        dx,
        dy,
        ball_x,
        ball_y,
        rail_x,
        rail_y,
    )


def draw_result(frame, result):
    """直接在 frame 上画调试信息并返回 frame。"""
    rail = result["rail"]
    ball = result["ball"]

    if rail is not None:
        x, y, w, h = rail["bbox"]
        cx, cy = rail["center"]
        cv2.rectangle(
            frame,
            (x, y),
            (x + w - 1, y + h - 1),
            (255, 0, 0),
            2,
        )
        cv2.circle(frame, (cx, cy), 6, (255, 0, 0), -1)

    if ball is not None:
        x, y, w, h = ball["bbox"]
        cx, cy = ball["center"]
        cv2.rectangle(
            frame,
            (x, y),
            (x + w - 1, y + h - 1),
            (0, 0, 255),
            2,
        )
        cv2.circle(frame, (cx, cy), 5, (0, 0, 255), -1)

    if result["valid"]:
        rail_center = result["rail"]["center"]
        ball_center = result["ball"]["center"]
        dx, dy = result["error"]
        cv2.line(
            frame,
            rail_center,
            ball_center,
            (0, 255, 255),
            2,
        )
        text = "VALID dx=%d dy=%d" % (dx, dy)
        color = (0, 255, 0)
    else:
        text = "NO TARGET"
        color = (0, 0, 255)

    cv2.putText(
        frame,
        text,
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        color,
        2,
    )
    return frame
