"""K230 CanMV：宽搜索 ROI + 水管中心轴约束的钢球检测。

特点：
1. 不检测绿色水管，不生成二值图；
2. 手动标定水管左右端中心，ROI 不需要贴合水管；
3. OpenCV HoughCircles 只负责产生钢球候选；
4. 使用候选到水管中心轴的距离排除假圆；
5. 输出沿水管方向和垂直水管方向的像素坐标；
6. 正常显示 320x240 彩色画面、搜索框、中心轴走廊和钢球。
"""

import gc
import math
import os
import sys
import time

import cv2
import image

from machine import FPIOA
from machine import UART
from media.display import Display
from media.sensor import Sensor


# ============================================================
# 1. 相机和显示
# ============================================================

FRAME_WIDTH = 320
FRAME_HEIGHT = 240

DISPLAY_ENABLE = True
DISPLAY_TYPE = Display.VIRT
IDE_PREVIEW_ENABLE = True

# OpenCV 颜色顺序为 BGR。
SEARCH_ROI_COLOR = (255, 0, 0)
AXIS_COLOR = (0, 255, 255)
CORRIDOR_COLOR = (0, 255, 0)
BALL_COLOR = (0, 0, 255)
ZERO_CROSS_COLOR = (255, 255, 255)
ZERO_CROSS_SIZE = 8


# ============================================================
# 2. 宽搜索 ROI
# ============================================================

# 这里只要求水管在框内，不要求框和水管边缘重合。
SEARCH_ROI_X = 0
SEARCH_ROI_Y = 60
SEARCH_ROI_WIDTH = 320
SEARCH_ROI_HEIGHT = 80


# ============================================================
# 3. 手动水管轴标定
# ============================================================

# 零刻度点严格等于宽搜索 ROI 的中心。
# 修改 SEARCH_ROI 后零点十字会自动一起移动。
# 左侧输出负数，右侧输出正数。
PIPE_ZERO = (
    SEARCH_ROI_X + SEARCH_ROI_WIDTH // 2,
    SEARCH_ROI_Y + SEARCH_ROI_HEIGHT // 2,
)

# 两个端点决定水管方向和 25cm 对应的像素长度。
# 应分别放在水管物理长度的左右端中心，方向必须从左指向右。
PIPE_LEFT = (10, 100)
PIPE_RIGHT = (310, 100)

PIPE_PHYSICAL_LENGTH_CM = 12

# 候选圆心距离中心轴不能超过该值。
# 这才是真正参与检测的“窄条”，矩形 SEARCH_ROI 可以很宽。
AXIS_HALF_WIDTH_PIXELS = 12

# 允许候选在左右端点外多出少量像素，防止端点标定误差。
AXIS_END_MARGIN_PIXELS = 8.0


# ============================================================
# 4. 霍夫圆和候选外观参数
# ============================================================

CIRCLE_MIN_RADIUS = 2
CIRCLE_MAX_RADIUS = 6
CIRCLE_EXPECTED_RADIUS = 5.5

CIRCLE_DP = 1.2
CIRCLE_MIN_DISTANCE = 20
CIRCLE_EDGE_THRESHOLD = 80
CIRCLE_ACC_THRESHOLD = 8

# 钢球通常同时存在暗边和高光。
CIRCLE_MIN_CONTRAST = 80.0
CIRCLE_MIN_HIGHLIGHT = 120.0

AXIS_DISTANCE_WEIGHT = 6.0
RADIUS_ERROR_WEIGHT = 3.0


# ============================================================
# 5. 连续帧跟踪
# ============================================================

TRACK_MAX_DISTANCE = 45.0
TRACK_DISTANCE_WEIGHT = 1.5

ACQUIRE_CONFIRM_FRAMES = 3
ACQUIRE_MAX_DISTANCE = 30.0
TRACK_SMOOTH_ALPHA = 0.45
TRACK_HOLD_LOST_FRAMES = 3


# ============================================================
# 6. 厘米坐标滤波
# ============================================================

# 先用 3 帧中值去掉单帧跳点，再做自适应低通。
CM_FILTER_STABLE_RANGE = 0.40
CM_FILTER_STABLE_ALPHA = 0.35
CM_FILTER_MOVING_ALPHA = 0.75

# 当前约45FPS，严格限制为0.1cm/帧时仍有约4.5cm/s跟随速度。
CM_FILTER_MAX_STEP = 0.10
CM_FILTER_DEADBAND = 0.02


# ============================================================
# 7. UART
# ============================================================

UART_ENABLE = True
UART_TX_PIN = 11
UART_RX_PIN = 12
UART_BAUDRATE = 115200

SEND_EVERY_N_FRAMES = 1
PRINT_EVERY_N_FRAMES = 15
GC_EVERY_N_FRAMES = 10


def validate_config():
    """检查搜索 ROI 和水管端点是否位于画面内。"""
    if SEARCH_ROI_X < 0 or SEARCH_ROI_Y < 0:
        raise ValueError("SEARCH_ROI_X/Y 不能小于 0")
    if SEARCH_ROI_WIDTH <= 0 or SEARCH_ROI_HEIGHT <= 0:
        raise ValueError("SEARCH_ROI_WIDTH/HEIGHT 必须大于 0")
    if SEARCH_ROI_X + SEARCH_ROI_WIDTH > FRAME_WIDTH:
        raise ValueError("SEARCH_ROI 横向超出画面")
    if SEARCH_ROI_Y + SEARCH_ROI_HEIGHT > FRAME_HEIGHT:
        raise ValueError("SEARCH_ROI 纵向超出画面")

    for point in (
        PIPE_ZERO,
        PIPE_LEFT,
        PIPE_RIGHT,
    ):
        if point[0] < 0 or point[0] >= FRAME_WIDTH:
            raise ValueError("PIPE_LEFT/RIGHT 横坐标超出画面")
        if point[1] < 0 or point[1] >= FRAME_HEIGHT:
            raise ValueError("PIPE_LEFT/RIGHT 纵坐标超出画面")


def build_pipe_axis():
    """根据左右端点生成中心、单位轴向量和法向量。"""
    vector_x = float(
        PIPE_RIGHT[0] - PIPE_LEFT[0]
    )
    vector_y = float(
        PIPE_RIGHT[1] - PIPE_LEFT[1]
    )
    length_pixels = math.sqrt(
        vector_x * vector_x
        + vector_y * vector_y
    )

    if length_pixels <= 1.0:
        raise ValueError("PIPE_LEFT 和 PIPE_RIGHT 不能重合")

    unit_x = vector_x / length_pixels
    unit_y = vector_y / length_pixels

    center_x = float(PIPE_ZERO[0])
    center_y = float(PIPE_ZERO[1])
    left_along = (
        (PIPE_LEFT[0] - center_x) * unit_x
        + (PIPE_LEFT[1] - center_y) * unit_y
    )
    right_along = (
        (PIPE_RIGHT[0] - center_x) * unit_x
        + (PIPE_RIGHT[1] - center_y) * unit_y
    )

    if left_along >= 0.0 or right_along <= 0.0:
        raise ValueError(
            "PIPE_LEFT 必须在零点左侧，PIPE_RIGHT 必须在零点右侧"
        )

    return {
        # 位置零点独立使用 PIPE_ZERO，不使用端点中点。
        "center_x": center_x,
        "center_y": center_y,
        "length_pixels": length_pixels,
        "left_along": left_along,
        "right_along": right_along,
        "unit_x": unit_x,
        "unit_y": unit_y,
        "normal_x": -unit_y,
        "normal_y": unit_x,
    }


def project_to_pipe_axis(frame_x, frame_y, pipe_axis):
    """将整帧坐标投影成沿水管坐标 along 和垂直坐标 normal。"""
    delta_x = (
        float(frame_x)
        - pipe_axis["center_x"]
    )
    delta_y = (
        float(frame_y)
        - pipe_axis["center_y"]
    )

    along = (
        delta_x * pipe_axis["unit_x"]
        + delta_y * pipe_axis["unit_y"]
    )
    normal = (
        delta_x * pipe_axis["normal_x"]
        + delta_y * pipe_axis["normal_y"]
    )
    return along, normal


def get_circle_count(circles):
    """兼容 PC OpenCV 和 K230 OpenCV 的圆数组形状。"""
    if circles is None:
        return 0

    shape = circles.shape
    if len(shape) == 3 and shape[0] == 1:
        return shape[1]
    return shape[0]


def get_circle(circles, index):
    """读取第 index 个圆的 ROI 局部 x、y、r。"""
    shape = circles.shape

    if len(shape) == 3 and shape[0] == 1:
        circle = circles[0, index]
    elif len(shape) == 3:
        circle = circles[index, 0]
    else:
        circle = circles[index]

    return (
        float(circle[0]),
        float(circle[1]),
        float(circle[2]),
    )


def get_local_features(
    gray,
    center_x,
    center_y,
    radius,
):
    """返回候选圆附近的灰度极差、最暗值和最亮值。"""
    patch_radius = max(
        CIRCLE_MIN_RADIUS,
        int(round(radius)),
    )
    x0 = max(0, center_x - patch_radius)
    y0 = max(0, center_y - patch_radius)
    x1 = min(
        SEARCH_ROI_WIDTH,
        center_x + patch_radius + 1,
    )
    y1 = min(
        SEARCH_ROI_HEIGHT,
        center_y + patch_radius + 1,
    )

    if x1 <= x0 or y1 <= y0:
        return 0.0, 255.0, 0.0

    patch = gray[y0:y1, x0:x1]
    min_value, max_value, _, _ = cv2.minMaxLoc(
        patch
    )
    contrast = float(max_value - min_value)
    patch = None

    return (
        contrast,
        float(min_value),
        float(max_value),
    )


def detect_ball_candidate(
    roi_frame,
    pipe_axis,
    tracking_reference,
):
    """使用霍夫圆、中心轴距离和连续性选择本帧最佳候选。"""
    gray = cv2.cvtColor(
        roi_frame,
        cv2.COLOR_BGR2GRAY,
    )
    blurred = cv2.GaussianBlur(
        gray,
        (5, 5),
        1.2,
    )

    circles = cv2.HoughCircles(
        blurred,
        3,
        dp=CIRCLE_DP,
        minDist=CIRCLE_MIN_DISTANCE,
        param1=CIRCLE_EDGE_THRESHOLD,
        param2=CIRCLE_ACC_THRESHOLD,
        minRadius=CIRCLE_MIN_RADIUS,
        maxRadius=CIRCLE_MAX_RADIUS,
    )

    best_ball = None
    best_score = -9999.0
    for index in range(get_circle_count(circles)):
        roi_x, roi_y, radius = get_circle(
            circles,
            index,
        )
        roi_center_x = int(round(roi_x))
        roi_center_y = int(round(roi_y))

        frame_x = SEARCH_ROI_X + roi_x
        frame_y = SEARCH_ROI_Y + roi_y
        along, normal = project_to_pipe_axis(
            frame_x,
            frame_y,
            pipe_axis,
        )

        if abs(normal) > AXIS_HALF_WIDTH_PIXELS:
            continue
        if (
            along
            < pipe_axis["left_along"]
            - AXIS_END_MARGIN_PIXELS
        ):
            continue
        if (
            along
            > pipe_axis["right_along"]
            + AXIS_END_MARGIN_PIXELS
        ):
            continue

        (
            contrast,
            min_value,
            max_value,
        ) = get_local_features(
            gray,
            roi_center_x,
            roi_center_y,
            radius,
        )

        if contrast < CIRCLE_MIN_CONTRAST:
            continue
        if max_value < CIRCLE_MIN_HIGHLIGHT:
            continue

        score = (
            contrast
            - AXIS_DISTANCE_WEIGHT
            * abs(normal)
            - RADIUS_ERROR_WEIGHT
            * abs(
                radius
                - CIRCLE_EXPECTED_RADIUS
            )
        )

        if tracking_reference is not None:
            delta_x = (
                frame_x
                - tracking_reference["frame_x"]
            )
            delta_y = (
                frame_y
                - tracking_reference["frame_y"]
            )
            distance = math.sqrt(
                delta_x * delta_x
                + delta_y * delta_y
            )

            if distance > TRACK_MAX_DISTANCE:
                continue

            score += 60.0
            score -= (
                distance
                * TRACK_DISTANCE_WEIGHT
            )

        if score > best_score:
            best_score = score
            best_ball = {
                "frame_x_float": frame_x,
                "frame_y_float": frame_y,
                "frame_x": int(round(frame_x)),
                "frame_y": int(round(frame_y)),
                "radius": int(round(radius)),
                "along": along,
                "normal": normal,
                "contrast": contrast,
                "min_value": min_value,
                "max_value": max_value,
                "score": score,
            }

    circles = None
    blurred = None
    gray = None
    return best_ball


def ball_distance(ball_a, ball_b):
    """计算两个候选在整帧坐标中的距离。"""
    if ball_a is None or ball_b is None:
        return 9999.0

    delta_x = (
        ball_a["frame_x_float"]
        - ball_b["frame_x_float"]
    )
    delta_y = (
        ball_a["frame_y_float"]
        - ball_b["frame_y_float"]
    )
    return math.sqrt(
        delta_x * delta_x
        + delta_y * delta_y
    )


def smooth_ball(
    previous_ball,
    current_ball,
    pipe_axis,
):
    """对确认后的钢球整帧坐标进行一阶低通滤波。"""
    if previous_ball is None:
        return current_ball

    alpha = TRACK_SMOOTH_ALPHA
    frame_x = (
        previous_ball["frame_x_float"]
        * (1.0 - alpha)
        + current_ball["frame_x_float"]
        * alpha
    )
    frame_y = (
        previous_ball["frame_y_float"]
        * (1.0 - alpha)
        + current_ball["frame_y_float"]
        * alpha
    )
    radius = (
        previous_ball["radius"]
        * (1.0 - alpha)
        + current_ball["radius"]
        * alpha
    )
    along, normal = project_to_pipe_axis(
        frame_x,
        frame_y,
        pipe_axis,
    )

    return {
        "frame_x_float": frame_x,
        "frame_y_float": frame_y,
        "frame_x": int(round(frame_x)),
        "frame_y": int(round(frame_y)),
        "radius": int(round(radius)),
        "along": along,
        "normal": normal,
        "contrast": current_ball["contrast"],
        "min_value": current_ball["min_value"],
        "max_value": current_ball["max_value"],
        "score": current_ball["score"],
    }


class PositionCmFilter:
    """3帧中值 + 自适应低通的厘米位置滤波器。"""

    def __init__(self):
        self.samples = []
        self.value = None

    def _median(self):
        count = len(self.samples)

        if count <= 0:
            return 0.0
        if count == 1:
            return self.samples[0]
        if count == 2:
            return (
                self.samples[0]
                + self.samples[1]
            ) / 2.0

        value_a = self.samples[0]
        value_b = self.samples[1]
        value_c = self.samples[2]
        return (
            value_a
            + value_b
            + value_c
            - min(value_a, value_b, value_c)
            - max(value_a, value_b, value_c)
        )

    def update(self, measurement_cm):
        self.samples.append(
            float(measurement_cm)
        )
        if len(self.samples) > 3:
            self.samples.pop(0)

        median_cm = self._median()

        if self.value is None:
            self.value = median_cm
            return self.value

        difference = median_cm - self.value
        absolute_difference = abs(difference)

        if absolute_difference <= CM_FILTER_DEADBAND:
            return self.value

        if absolute_difference <= CM_FILTER_STABLE_RANGE:
            step = (
                difference
                * CM_FILTER_STABLE_ALPHA
            )
        else:
            # 大变化先快速低通，随后仍执行统一的0.1cm/帧限幅。
            step = (
                difference
                * CM_FILTER_MOVING_ALPHA
            )

        if step > CM_FILTER_MAX_STEP:
            step = CM_FILTER_MAX_STEP
        elif step < -CM_FILTER_MAX_STEP:
            step = -CM_FILTER_MAX_STEP

        self.value += step
        return self.value

    def last(self):
        if self.value is None:
            return 0.0
        return self.value


def build_result(ball, pipe_axis):
    """生成像素坐标和厘米位置。"""
    pipe_center_x = int(
        round(pipe_axis["center_x"])
    )
    pipe_center_y = int(
        round(pipe_axis["center_y"])
    )

    if ball is None:
        return {
            "valid": False,
            "along": 0,
            "normal": 0,
            "position_cm": 0.0,
            "filtered_position_cm": 0.0,
            "ball_x": -1,
            "ball_y": -1,
            "pipe_x": pipe_center_x,
            "pipe_y": pipe_center_y,
        }

    # 零点为物理中心，左右两段各为 12.5cm。
    # 分段换算可补偿透视造成的左右像素长度轻微不一致。
    half_length_cm = (
        PIPE_PHYSICAL_LENGTH_CM / 2.0
    )
    if ball["along"] < 0.0:
        position_cm = (
            ball["along"]
            * half_length_cm
            / abs(pipe_axis["left_along"])
        )
    else:
        position_cm = (
            ball["along"]
            * half_length_cm
            / pipe_axis["right_along"]
        )

    return {
        "valid": True,
        "along": int(round(ball["along"])),
        "normal": int(round(ball["normal"])),
        "position_cm": position_cm,
        "filtered_position_cm": position_cm,
        "ball_x": ball["frame_x"],
        "ball_y": ball["frame_y"],
        "pipe_x": pipe_center_x,
        "pipe_y": pipe_center_y,
    }


def build_uart_packet(result, sequence):
    """只发送：帧序号、有效标志、滤波位置cm×100。"""
    if not result["valid"]:
        return "GB,%d,0,0\n" % (
            sequence,
        )

    position_x100 = int(
        round(
            result["filtered_position_cm"]
            * 100.0
        )
    )

    return "GB,%d,1,%d\n" % (
        sequence,
        position_x100,
    )


def offset_axis_point(
    point,
    pipe_axis,
    offset,
):
    """沿水管法向量偏移一个点，用于画倾斜走廊。"""
    return (
        int(
            round(
                point[0]
                + pipe_axis["normal_x"]
                * offset
            )
        ),
        int(
            round(
                point[1]
                + pipe_axis["normal_y"]
                * offset
            )
        ),
    )


def draw_overlay(
    display_np,
    pipe_axis,
    ball,
):
    """画宽搜索框、中心轴、窄走廊和钢球。"""
    zero_x = int(round(pipe_axis["center_x"]))
    zero_y = int(round(pipe_axis["center_y"]))

    # 检测区域中心就是零刻度点，十字同时也是坐标计算原点。
    cv2.line(
        display_np,
        (
            zero_x - ZERO_CROSS_SIZE,
            zero_y,
        ),
        (
            zero_x + ZERO_CROSS_SIZE,
            zero_y,
        ),
        ZERO_CROSS_COLOR,
        1,
    )
    cv2.line(
        display_np,
        (
            zero_x,
            zero_y - ZERO_CROSS_SIZE,
        ),
        (
            zero_x,
            zero_y + ZERO_CROSS_SIZE,
        ),
        ZERO_CROSS_COLOR,
        1,
    )

    cv2.rectangle(
        display_np,
        (
            SEARCH_ROI_X,
            SEARCH_ROI_Y,
        ),
        (
            SEARCH_ROI_X
            + SEARCH_ROI_WIDTH
            - 1,
            SEARCH_ROI_Y
            + SEARCH_ROI_HEIGHT
            - 1,
        ),
        SEARCH_ROI_COLOR,
        1,
    )

    cv2.line(
        display_np,
        PIPE_LEFT,
        PIPE_RIGHT,
        AXIS_COLOR,
        2,
    )

    top_left = offset_axis_point(
        PIPE_LEFT,
        pipe_axis,
        -AXIS_HALF_WIDTH_PIXELS,
    )
    top_right = offset_axis_point(
        PIPE_RIGHT,
        pipe_axis,
        -AXIS_HALF_WIDTH_PIXELS,
    )
    bottom_left = offset_axis_point(
        PIPE_LEFT,
        pipe_axis,
        AXIS_HALF_WIDTH_PIXELS,
    )
    bottom_right = offset_axis_point(
        PIPE_RIGHT,
        pipe_axis,
        AXIS_HALF_WIDTH_PIXELS,
    )

    cv2.line(
        display_np,
        top_left,
        top_right,
        CORRIDOR_COLOR,
        1,
    )
    cv2.line(
        display_np,
        bottom_left,
        bottom_right,
        CORRIDOR_COLOR,
        1,
    )
    cv2.line(
        display_np,
        top_left,
        bottom_left,
        CORRIDOR_COLOR,
        1,
    )
    cv2.line(
        display_np,
        top_right,
        bottom_right,
        CORRIDOR_COLOR,
        1,
    )

    if ball is None:
        return

    cv2.circle(
        display_np,
        (
            ball["frame_x"],
            ball["frame_y"],
        ),
        max(
            CIRCLE_MIN_RADIUS,
            ball["radius"],
        ),
        BALL_COLOR,
        2,
    )


def init_uart():
    fpioa = FPIOA()
    fpioa.set_function(
        UART_TX_PIN,
        fpioa.UART2_TXD,
    )
    fpioa.set_function(
        UART_RX_PIN,
        fpioa.UART2_RXD,
    )

    uart = UART(
        UART.UART2,
        baudrate=UART_BAUDRATE,
        bits=UART.EIGHTBITS,
        parity=UART.PARITY_NONE,
        stop=UART.STOPBITS_ONE,
    )
    return fpioa, uart


def main():
    sensor = None
    uart = None
    uart_fpioa = None
    frame_id = 0
    tracked_ball = None
    pending_ball = None
    pending_hits = 0
    lost_frames = 0
    position_filter = PositionCmFilter()
    uart_sequence = 0
    clock = time.clock()

    try:
        validate_config()
        pipe_axis = build_pipe_axis()
        os.exitpoint(os.EXITPOINT_ENABLE)

        if UART_ENABLE:
            uart_fpioa, uart = init_uart()

        sensor = Sensor(
            width=1280,
            height=960,
            fps=90,
        )
        sensor.reset()
        sensor.set_framesize(
            width=FRAME_WIDTH,
            height=FRAME_HEIGHT,
        )
        sensor.set_pixformat(Sensor.RGB888)

        if DISPLAY_ENABLE:
            Display.init(
                DISPLAY_TYPE,
                width=FRAME_WIDTH,
                height=FRAME_HEIGHT,
                to_ide=IDE_PREVIEW_ENABLE,
                quality=50,
            )

        sensor.run()
        time.sleep(0.5)

        print(
            "AXIS zero=(%d,%d) left=(%d,%d) right=(%d,%d) "
            "left_scale=%.3f right_scale=%.3f cm/px"
            % (
                PIPE_ZERO[0],
                PIPE_ZERO[1],
                PIPE_LEFT[0],
                PIPE_LEFT[1],
                PIPE_RIGHT[0],
                PIPE_RIGHT[1],
                (
                    PIPE_PHYSICAL_LENGTH_CM / 2.0
                    / abs(pipe_axis["left_along"])
                ),
                (
                    PIPE_PHYSICAL_LENGTH_CM / 2.0
                    / pipe_axis["right_along"]
                ),
            )
        )
        print(
            "SEARCH ROI=(%d,%d,%d,%d) corridor=+/-%.1fpx"
            % (
                SEARCH_ROI_X,
                SEARCH_ROI_Y,
                SEARCH_ROI_WIDTH,
                SEARCH_ROI_HEIGHT,
                AXIS_HALF_WIDTH_PIXELS,
            )
        )

        while True:
            os.exitpoint()
            clock.tick()
            frame_id += 1

            img = sensor.snapshot()
            frame = img.to_numpy_ref()
            roi_frame = frame[
                SEARCH_ROI_Y:
                SEARCH_ROI_Y + SEARCH_ROI_HEIGHT,
                SEARCH_ROI_X:
                SEARCH_ROI_X + SEARCH_ROI_WIDTH,
            ]

            if tracked_ball is not None:
                tracking_reference = tracked_ball
            else:
                tracking_reference = pending_ball

            raw_ball = detect_ball_candidate(
                roi_frame,
                pipe_axis,
                tracking_reference,
            )

            if tracked_ball is not None:
                if raw_ball is not None:
                    tracked_ball = smooth_ball(
                        tracked_ball,
                        raw_ball,
                        pipe_axis,
                    )
                    lost_frames = 0
                    ball = tracked_ball
                else:
                    lost_frames += 1
                    if (
                        lost_frames
                        <= TRACK_HOLD_LOST_FRAMES
                    ):
                        ball = tracked_ball
                    else:
                        tracked_ball = None
                        pending_ball = None
                        pending_hits = 0
                        ball = None
            else:
                ball = None

                if raw_ball is None:
                    pending_ball = None
                    pending_hits = 0
                elif (
                    pending_ball is not None
                    and ball_distance(
                        pending_ball,
                        raw_ball,
                    )
                    <= ACQUIRE_MAX_DISTANCE
                ):
                    pending_ball = smooth_ball(
                        pending_ball,
                        raw_ball,
                        pipe_axis,
                    )
                    pending_hits += 1
                else:
                    pending_ball = raw_ball
                    pending_hits = 1

                if (
                    pending_ball is not None
                    and pending_hits
                    >= ACQUIRE_CONFIRM_FRAMES
                ):
                    tracked_ball = pending_ball
                    pending_ball = None
                    pending_hits = 0
                    lost_frames = 0
                    ball = tracked_ball

            result = build_result(
                ball,
                pipe_axis,
            )

            if result["valid"]:
                result["filtered_position_cm"] = (
                    position_filter.update(
                        result["position_cm"]
                    )
                )
            else:
                result["filtered_position_cm"] = (
                    position_filter.last()
                )

            uart_sequence = (
                uart_sequence + 1
            ) & 0xFF
            packet = build_uart_packet(
                result,
                uart_sequence,
            )

            if (
                UART_ENABLE
                and frame_id % SEND_EVERY_N_FRAMES == 0
            ):
                uart.write(
                    packet.encode("utf-8")
                )

            display_np = None
            display_img = None

            if DISPLAY_ENABLE:
                # copy 为连续 RGB888 缓冲区，避免显示偏移。
                display_np = frame.copy()
                draw_overlay(
                    display_np,
                    pipe_axis,
                    ball,
                )
                display_img = image.Image(
                    FRAME_WIDTH,
                    FRAME_HEIGHT,
                    image.RGB888,
                    alloc=image.ALLOC_REF,
                    data=display_np,
                )
                Display.show_image(display_img)

            if frame_id % GC_EVERY_N_FRAMES == 0:
                gc.collect()

            if frame_id % PRINT_EVERY_N_FRAMES == 0:
                try:
                    free_kb = gc.mem_free() // 1024
                except Exception:
                    free_kb = -1

                if result["valid"]:
                    print(
                        "%s cm=%.2f raw=%.2f fps=%.1f free=%dKB"
                        % (
                            packet.strip(),
                            result["filtered_position_cm"],
                            result["position_cm"],
                            clock.fps(),
                            free_kb,
                        )
                    )
                else:
                    print(
                        "%s fps=%.1f free=%dKB"
                        % (
                            packet.strip(),
                            clock.fps(),
                            free_kb,
                        )
                    )

            display_img = None
            display_np = None
            packet = None
            result = None
            ball = None
            raw_ball = None
            tracking_reference = None
            roi_frame = None
            frame = None
            img = None

    except KeyboardInterrupt:
        print("用户停止")
    except BaseException as exc:
        print("运行异常:", exc)
        sys.print_exception(exc)
    finally:
        if isinstance(sensor, Sensor):
            sensor.stop()

        if uart is not None:
            try:
                uart.deinit()
            except Exception:
                pass

        if DISPLAY_ENABLE:
            Display.deinit()

        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
        time.sleep_ms(100)
        gc.collect()


if __name__ == "__main__":
    main()
