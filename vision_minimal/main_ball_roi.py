"""K230 CanMV：固定窄 ROI 内只检测钢球。

本版本不识别绿色水管，也不生成二值图：
1. 手动把 BALL_ROI 放到水管内部；
2. ROI 内转灰度并轻度模糊；
3. 使用 HoughCircles 检测钢球；
4. 正常彩色画面只画窄 ROI 框和钢球圆圈；
5. ROI 中心作为水管中心，输出钢球像素误差。
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
# 1. 相机和手动窄 ROI
# ============================================================

FRAME_WIDTH = 320
FRAME_HEIGHT = 240

# 只需要调这四个数，把框压在绿色水管内部。
# 框可以很宽，但高度尽量只覆盖水管内部，减少背景误检。
BALL_ROI_X = 0
BALL_ROI_Y = 85
BALL_ROI_WIDTH = 320
BALL_ROI_HEIGHT = 30

DISPLAY_ENABLE = True
DISPLAY_TYPE = Display.VIRT
IDE_PREVIEW_ENABLE = True

# OpenCV 使用 BGR 颜色顺序。
ROI_BOX_COLOR = (255, 0, 0)
BALL_COLOR = (0, 0, 255)


# ============================================================
# 2. 霍夫圆参数
# ============================================================

# 实拍图缩放到 320x240 后，钢球半径大约为 4~7 像素。
CIRCLE_MIN_RADIUS = 3
CIRCLE_MAX_RADIUS = 10
CIRCLE_EXPECTED_RADIUS = 5.5

CIRCLE_DP = 1.2
CIRCLE_MIN_DISTANCE = 20
CIRCLE_EDGE_THRESHOLD = 100
CIRCLE_ACC_THRESHOLD = 10

# 钢球内部同时有高光和暗边，局部 max-min 通常明显大于普通纹理。
CIRCLE_MIN_CONTRAST = 120.0
CIRCLE_MIN_HIGHLIGHT = 160.0
ROI_CENTER_DISTANCE_WEIGHT = 4.0
RADIUS_ERROR_WEIGHT = 3.0

# 上一帧检测成功后，优先选择邻近候选，降低结果跳动。
TRACK_MAX_DISTANCE = 45.0
TRACK_DISTANCE_WEIGHT = 1.5

# 新目标连续出现 3 帧才确认；确认后使用低通滤波并允许短暂丢失。
ACQUIRE_CONFIRM_FRAMES = 3
ACQUIRE_MAX_DISTANCE = 30.0
TRACK_SMOOTH_ALPHA = 0.45
TRACK_HOLD_LOST_FRAMES = 3


# ============================================================
# 3. 串口和运行频率
# ============================================================

UART_ENABLE = True
UART_TX_PIN = 11
UART_RX_PIN = 12
UART_BAUDRATE = 115200

SEND_EVERY_N_FRAMES = 1
PRINT_EVERY_N_FRAMES = 15
GC_EVERY_N_FRAMES = 10


def validate_roi():
    """启动时检查 ROI 是否完全位于 320x240 画面内。"""
    if BALL_ROI_X < 0 or BALL_ROI_Y < 0:
        raise ValueError("BALL_ROI_X/Y 不能小于 0")
    if BALL_ROI_WIDTH <= 0 or BALL_ROI_HEIGHT <= 0:
        raise ValueError("BALL_ROI_WIDTH/HEIGHT 必须大于 0")
    if BALL_ROI_X + BALL_ROI_WIDTH > FRAME_WIDTH:
        raise ValueError("BALL_ROI 横向超出画面")
    if BALL_ROI_Y + BALL_ROI_HEIGHT > FRAME_HEIGHT:
        raise ValueError("BALL_ROI 纵向超出画面")


def get_circle_count(circles):
    """兼容 PC OpenCV 和 K230 OpenCV 的圆数组形状。"""
    if circles is None:
        return 0

    shape = circles.shape
    if len(shape) == 3 and shape[0] == 1:
        return shape[1]
    return shape[0]


def get_circle(circles, index):
    """读取第 index 个圆的 x、y、r。"""
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


def get_local_features(gray, center_x, center_y, radius):
    """返回候选附近的灰度极差、最暗值和最亮值。"""
    patch_radius = max(
        CIRCLE_MIN_RADIUS,
        int(round(radius)),
    )
    x0 = max(0, center_x - patch_radius)
    y0 = max(0, center_y - patch_radius)
    x1 = min(
        BALL_ROI_WIDTH,
        center_x + patch_radius + 1,
    )
    y1 = min(
        BALL_ROI_HEIGHT,
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


def detect_ball_in_roi(roi_frame, previous_ball):
    """不做二值化，直接在灰度 ROI 上使用霍夫圆检测钢球。"""
    gray = cv2.cvtColor(
        roi_frame,
        cv2.COLOR_BGR2GRAY,
    )
    blurred = cv2.GaussianBlur(
        gray,
        (5, 5),
        1.2,
    )

    # K230 SDK 官方示例使用 method=3。
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
    roi_center_y = BALL_ROI_HEIGHT / 2.0

    for index in range(get_circle_count(circles)):
        circle_x, circle_y, radius = get_circle(
            circles,
            index,
        )
        center_x = int(round(circle_x))
        center_y = int(round(circle_y))

        contrast, min_value, max_value = get_local_features(
            gray,
            center_x,
            center_y,
            radius,
        )
        if contrast < CIRCLE_MIN_CONTRAST:
            continue
        if max_value < CIRCLE_MIN_HIGHLIGHT:
            continue

        # ROI 应该正好压在水管内部，因此越靠近 ROI 中线越可信。
        score = (
            contrast
            - ROI_CENTER_DISTANCE_WEIGHT
            * abs(circle_y - roi_center_y)
            - RADIUS_ERROR_WEIGHT
            * abs(
                radius
                - CIRCLE_EXPECTED_RADIUS
            )
        )

        if previous_ball is not None:
            delta_x = (
                circle_x
                - previous_ball["roi_x"]
            )
            delta_y = (
                circle_y
                - previous_ball["roi_y"]
            )
            distance = math.sqrt(
                delta_x * delta_x
                + delta_y * delta_y
            )

            if distance <= TRACK_MAX_DISTANCE:
                score -= (
                    distance
                    * TRACK_DISTANCE_WEIGHT
                )
                score += 60.0
            else:
                # 已经跟踪后，不允许结果突然跳到远处的假圆。
                continue

        if score > best_score:
            best_score = score
            best_ball = {
                "roi_x": circle_x,
                "roi_y": circle_y,
                "frame_x": int(
                    round(
                        BALL_ROI_X
                        + circle_x
                    )
                ),
                "frame_y": int(
                    round(
                        BALL_ROI_Y
                        + circle_y
                    )
                ),
                "radius": int(
                    round(radius)
                ),
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
    """计算两个 ROI 局部坐标之间的距离。"""
    if ball_a is None or ball_b is None:
        return 9999.0

    delta_x = (
        ball_a["roi_x"]
        - ball_b["roi_x"]
    )
    delta_y = (
        ball_a["roi_y"]
        - ball_b["roi_y"]
    )
    return math.sqrt(
        delta_x * delta_x
        + delta_y * delta_y
    )


def smooth_ball(previous_ball, current_ball):
    """对确认后的钢球坐标做一阶低通滤波。"""
    if previous_ball is None:
        return current_ball

    alpha = TRACK_SMOOTH_ALPHA
    roi_x = (
        previous_ball["roi_x"]
        * (1.0 - alpha)
        + current_ball["roi_x"]
        * alpha
    )
    roi_y = (
        previous_ball["roi_y"]
        * (1.0 - alpha)
        + current_ball["roi_y"]
        * alpha
    )
    radius = (
        previous_ball["radius"]
        * (1.0 - alpha)
        + current_ball["radius"]
        * alpha
    )

    return {
        "roi_x": roi_x,
        "roi_y": roi_y,
        "frame_x": int(
            round(
                BALL_ROI_X
                + roi_x
            )
        ),
        "frame_y": int(
            round(
                BALL_ROI_Y
                + roi_y
            )
        ),
        "radius": int(round(radius)),
        "contrast": current_ball["contrast"],
        "min_value": current_ball["min_value"],
        "max_value": current_ball["max_value"],
        "score": current_ball["score"],
    }


def build_result(ball):
    """使用固定 ROI 中心作为水管中点。"""
    pipe_center_x = (
        BALL_ROI_X
        + BALL_ROI_WIDTH // 2
    )
    pipe_center_y = (
        BALL_ROI_Y
        + BALL_ROI_HEIGHT // 2
    )

    if ball is None:
        return {
            "valid": False,
            "dx": 0,
            "dy": 0,
            "ball_x": -1,
            "ball_y": -1,
            "pipe_x": pipe_center_x,
            "pipe_y": pipe_center_y,
        }

    return {
        "valid": True,
        "dx": ball["frame_x"] - pipe_center_x,
        "dy": ball["frame_y"] - pipe_center_y,
        "ball_x": ball["frame_x"],
        "ball_y": ball["frame_y"],
        "pipe_x": pipe_center_x,
        "pipe_y": pipe_center_y,
    }


def build_uart_packet(result):
    """保持原来的 GB 固定字段格式。"""
    if not result["valid"]:
        return "GB,0,0,0,-1,-1,%d,%d\n" % (
            result["pipe_x"],
            result["pipe_y"],
        )

    return "GB,1,%d,%d,%d,%d,%d,%d\n" % (
        result["dx"],
        result["dy"],
        result["ball_x"],
        result["ball_y"],
        result["pipe_x"],
        result["pipe_y"],
    )


def draw_overlay(display_np, ball):
    """彩色画面只画固定窄 ROI 框和检测到的钢球。"""
    cv2.rectangle(
        display_np,
        (
            BALL_ROI_X,
            BALL_ROI_Y,
        ),
        (
            BALL_ROI_X
            + BALL_ROI_WIDTH
            - 1,
            BALL_ROI_Y
            + BALL_ROI_HEIGHT
            - 1,
        ),
        ROI_BOX_COLOR,
        2,
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
    clock = time.clock()

    try:
        validate_roi()
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
            "BALL ROI=(%d,%d,%d,%d) radius=[%d,%d]"
            % (
                BALL_ROI_X,
                BALL_ROI_Y,
                BALL_ROI_WIDTH,
                BALL_ROI_HEIGHT,
                CIRCLE_MIN_RADIUS,
                CIRCLE_MAX_RADIUS,
            )
        )
        print(
            "binary=False display=%s UART=%s"
            % (
                str(DISPLAY_ENABLE),
                str(UART_ENABLE),
            )
        )

        while True:
            os.exitpoint()
            clock.tick()
            frame_id += 1

            img = sensor.snapshot()
            frame = img.to_numpy_ref()
            roi_frame = frame[
                BALL_ROI_Y:
                BALL_ROI_Y + BALL_ROI_HEIGHT,
                BALL_ROI_X:
                BALL_ROI_X + BALL_ROI_WIDTH,
            ]

            if tracked_ball is not None:
                tracking_reference = tracked_ball
            else:
                tracking_reference = pending_ball

            raw_ball = detect_ball_in_roi(
                roi_frame,
                tracking_reference,
            )

            # 已确认目标：位置门限 + 平滑 + 最多保持 3 个丢失帧。
            if tracked_ball is not None:
                if raw_ball is not None:
                    tracked_ball = smooth_ball(
                        tracked_ball,
                        raw_ball,
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
                # 未确认目标：连续靠近的候选达到 3 帧才允许输出。
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

            result = build_result(ball)
            packet = build_uart_packet(result)

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
                # 必须 copy 成连续缓冲区，避免 ALLOC_REF 显示错位。
                display_np = frame.copy()
                draw_overlay(
                    display_np,
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
