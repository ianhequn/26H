"""K230 CanMV：水管旋正 + 小球角度坐标映射测试。

流程：
1. 启动时用若干有效帧标定绿色水管的中心、长度、宽度和倾角。
2. 后续只旋转绿色二值掩膜，把水管变成水平长条。
3. 在长条内部寻找非绿色的近圆形小球。
4. 输出小球相对水管中心的沿管误差 along 和垂直误差 normal。

调试画面只显示旋正后的二值长条，不绘制方框或文字。
"""

import gc
import math
import os
import sys
import time

import cv2
import image
from ulab import numpy as np

from machine import FPIOA
from machine import UART
from media.display import Display
from media.sensor import Sensor


# ============================================================
# 1. 图像、ROI、显示和串口配置
# ============================================================

FRAME_WIDTH = 320
FRAME_HEIGHT = 240

# 只处理画面中间的宽长条，坐标相对于 320x240 原图。
ROI_X = 0
ROI_Y = 30
ROI_WIDTH = 320
ROI_HEIGHT = 160

# 该测试模式显示“旋正后的绿色二值长条”。
# 若只测速度，把它改成 False；角度映射仍然正常工作。
STRAIGHT_BINARY_DEBUG = False
STRAIGHT_VIEW_HEIGHT = 64
DISPLAY_TYPE = Display.VIRT
IDE_PREVIEW_ENABLE = True

UART_ENABLE = True
UART_TX_PIN = 11
UART_RX_PIN = 12
UART_BAUDRATE = 115200

SEND_EVERY_N_FRAMES = 1
PRINT_EVERY_N_FRAMES = 15
GC_EVERY_N_FRAMES = 10


# ============================================================
# 2. HSV、标定和候选筛选参数
# ============================================================

# OpenCV 的 H 范围为 0~179。
H_MIN = 35
H_MAX = 95
S_MIN = 60
S_MAX = 255
V_MIN = 35
V_MAX = 255

COLOR_LO = np.array(
    [H_MIN, S_MIN, V_MIN],
    dtype=np.uint8,
)
COLOR_HI = np.array(
    [H_MAX, S_MAX, V_MAX],
    dtype=np.uint8,
)

# 启动时累计这些有效帧，然后锁定水管几何参数。
# 相机或水管位置改变后，需要重新运行脚本完成标定。
CALIBRATION_VALID_FRAMES = 10

RAIL_MIN_LENGTH_RATIO = 0.35
RAIL_MIN_AREA_RATIO = 0.02
RAIL_MIN_ASPECT = 3.5
RAIL_MAX_THICKNESS_RATIO = 0.45

# 只搜索水管中间约 66% 的厚度，避免把上下背景当成小球。
BALL_SEARCH_HALF_THICKNESS_RATIO = 0.33
BALL_END_MARGIN_PIXELS = 4
BALL_MIN_DIAMETER_RATIO = 0.10
BALL_MAX_DIAMETER_RATIO = 0.58
BALL_MIN_ASPECT = 0.50
BALL_MAX_ASPECT = 1.80
BALL_MIN_CIRCULARITY = 0.30

RAIL_CLOSE_KERNEL = cv2.getStructuringElement(
    cv2.MORPH_RECT,
    (17, 9),
)
BALL_OPEN_KERNEL = cv2.getStructuringElement(
    cv2.MORPH_ELLIPSE,
    (3, 3),
)
BALL_CLOSE_KERNEL = cv2.getStructuringElement(
    cv2.MORPH_ELLIPSE,
    (3, 3),
)


# ============================================================
# 3. 基础图像工具
# ============================================================

def find_external_contours(binary):
    """兼容两种 OpenCV findContours 返回格式。"""
    result = cv2.findContours(
        binary,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if len(result) == 3:
        return result[1]
    return result[0]


def make_green_mask(frame):
    """绿色为白色，其余为黑色。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, COLOR_LO, COLOR_HI)
    hsv = None
    return mask


def normalize_pipe_rect(rect):
    """把 minAreaRect 统一成：length >= thickness，angle 属于 [-90, 90)。"""
    center, size, angle_deg = rect
    center_x = float(center[0])
    center_y = float(center[1])
    width = float(size[0])
    height = float(size[1])

    if width >= height:
        length = width
        thickness = height
    else:
        length = height
        thickness = width
        angle_deg += 90.0

    while angle_deg >= 90.0:
        angle_deg -= 180.0
    while angle_deg < -90.0:
        angle_deg += 180.0

    return {
        "center_x": center_x,
        "center_y": center_y,
        "length": length,
        "thickness": thickness,
        "angle_deg": float(angle_deg),
    }


# ============================================================
# 4. 启动标定：识别水管中心和角度
# ============================================================

def detect_pipe_geometry(raw_green_mask):
    """从当前绿色掩膜中寻找最长的绿色旋转矩形。"""
    merged_green = cv2.morphologyEx(
        raw_green_mask,
        cv2.MORPH_CLOSE,
        RAIL_CLOSE_KERNEL,
    )

    best_pipe = None
    best_area = 0.0

    for contour in find_external_contours(merged_green):
        contour_area = float(cv2.contourArea(contour))
        pipe = normalize_pipe_rect(cv2.minAreaRect(contour))
        length = pipe["length"]
        thickness = pipe["thickness"]

        if thickness <= 0.0:
            continue

        aspect = length / thickness

        if length < ROI_WIDTH * RAIL_MIN_LENGTH_RATIO:
            continue
        if contour_area < ROI_WIDTH * ROI_HEIGHT * RAIL_MIN_AREA_RATIO:
            continue
        if aspect < RAIL_MIN_ASPECT:
            continue
        if thickness > ROI_HEIGHT * RAIL_MAX_THICKNESS_RATIO:
            continue

        if contour_area > best_area:
            best_area = contour_area
            pipe["area"] = contour_area
            pipe["aspect"] = aspect
            best_pipe = pipe

    merged_green = None
    return best_pipe


class PipeCalibrator:
    """对多个有效帧求均值，避免单帧反光导致角度抖动。"""

    def __init__(self, target_count):
        self.target_count = target_count
        self.count = 0
        self.sum_center_x = 0.0
        self.sum_center_y = 0.0
        self.sum_length = 0.0
        self.sum_thickness = 0.0

        # 水管方向每 180 度等价，所以用二倍角求平均。
        self.sum_angle_cos2 = 0.0
        self.sum_angle_sin2 = 0.0

    def add(self, pipe):
        angle_rad2 = (
            pipe["angle_deg"]
            * math.pi
            / 90.0
        )

        self.sum_center_x += pipe["center_x"]
        self.sum_center_y += pipe["center_y"]
        self.sum_length += pipe["length"]
        self.sum_thickness += pipe["thickness"]
        self.sum_angle_cos2 += math.cos(angle_rad2)
        self.sum_angle_sin2 += math.sin(angle_rad2)
        self.count += 1

    def ready(self):
        return self.count >= self.target_count

    def result(self):
        if self.count <= 0:
            return None

        count = float(self.count)
        average_angle_rad = 0.5 * math.atan2(
            self.sum_angle_sin2,
            self.sum_angle_cos2,
        )
        average_angle_deg = (
            average_angle_rad
            * 180.0
            / math.pi
        )

        pipe = {
            "center_x": self.sum_center_x / count,
            "center_y": self.sum_center_y / count,
            "length": self.sum_length / count,
            "thickness": self.sum_thickness / count,
            "angle_deg": average_angle_deg,
        }

        # 实拍图验证：这里必须直接使用 angle_deg。
        # 写成 -angle_deg 会让原倾角加倍，而不是变水平。
        pipe["rotate_matrix"] = cv2.getRotationMatrix2D(
            (
                pipe["center_x"],
                pipe["center_y"],
            ),
            pipe["angle_deg"],
            1.0,
        )
        return pipe


# ============================================================
# 5. 旋正长条内的小球检测和坐标映射
# ============================================================

def get_straight_search_rect(pipe):
    """返回旋正图中的小球搜索范围 x0, y0, x1, y1。"""
    center_x = pipe["center_x"]
    center_y = pipe["center_y"]
    length = pipe["length"]
    thickness = pipe["thickness"]

    half_search_h = max(
        4,
        int(
            thickness
            * BALL_SEARCH_HALF_THICKNESS_RATIO
        ),
    )

    x0 = max(
        0,
        int(
            round(
                center_x
                - length / 2.0
                + BALL_END_MARGIN_PIXELS
            )
        ),
    )
    x1 = min(
        ROI_WIDTH,
        int(
            round(
                center_x
                + length / 2.0
                - BALL_END_MARGIN_PIXELS
            )
        ),
    )
    y0 = max(
        0,
        int(round(center_y)) - half_search_h,
    )
    y1 = min(
        ROI_HEIGHT,
        int(round(center_y)) + half_search_h + 1,
    )

    return x0, y0, x1, y1


def detect_ball_in_straight_mask(straight_green, pipe):
    """在已经水平的绿色掩膜内寻找非绿色近圆形区域。"""
    x0, y0, x1, y1 = get_straight_search_rect(pipe)

    if x1 <= x0 or y1 <= y0:
        return None

    green_band = straight_green[y0:y1, x0:x1]
    ball_mask = cv2.bitwise_not(green_band)
    ball_mask = cv2.morphologyEx(
        ball_mask,
        cv2.MORPH_OPEN,
        BALL_OPEN_KERNEL,
    )
    ball_mask = cv2.morphologyEx(
        ball_mask,
        cv2.MORPH_CLOSE,
        BALL_CLOSE_KERNEL,
    )

    thickness = pipe["thickness"]
    min_diameter = max(
        4,
        int(
            thickness
            * BALL_MIN_DIAMETER_RATIO
        ),
    )
    max_diameter = max(
        min_diameter + 3,
        int(
            thickness
            * BALL_MAX_DIAMETER_RATIO
        ),
    )

    best_ball = None
    best_score = -999.0

    for contour in find_external_contours(ball_mask):
        area = float(cv2.contourArea(contour))
        x, y, width, height = cv2.boundingRect(contour)

        if width <= 0 or height <= 0:
            continue

        diameter = max(width, height)
        if diameter < min_diameter:
            continue
        if diameter > max_diameter:
            continue

        aspect = float(width) / float(height)
        if aspect < BALL_MIN_ASPECT:
            continue
        if aspect > BALL_MAX_ASPECT:
            continue

        perimeter = float(cv2.arcLength(contour, True))
        if perimeter <= 0.0:
            continue

        circularity = (
            4.0
            * math.pi
            * area
            / (perimeter * perimeter)
        )
        if circularity < BALL_MIN_CIRCULARITY:
            continue

        size_ratio = float(diameter) / thickness
        score = (
            2.0 * circularity
            - abs(aspect - 1.0)
            - abs(size_ratio - 0.28)
        )

        if score > best_score:
            ball_x = x0 + x + width / 2.0
            ball_y = y0 + y + height / 2.0
            best_score = score
            best_ball = {
                "straight_x": ball_x,
                "straight_y": ball_y,
                "width": int(width),
                "height": int(height),
                "circularity": circularity,
            }

    ball_mask = None
    return best_ball


def map_ball_result(ball, pipe):
    """生成沿水管/垂直水管坐标，并还原小球在 320x240 原图的位置。"""
    if ball is None:
        return {
            "valid": False,
            "along": 0,
            "normal": 0,
            "ball_x": -1,
            "ball_y": -1,
        }

    along_float = (
        ball["straight_x"]
        - pipe["center_x"]
    )
    normal_float = (
        ball["straight_y"]
        - pipe["center_y"]
    )

    # straight -> 原 ROI 坐标：对旋正操作做逆旋转。
    angle_rad = (
        pipe["angle_deg"]
        * math.pi
        / 180.0
    )
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    original_dx = (
        cos_a * along_float
        - sin_a * normal_float
    )
    original_dy = (
        sin_a * along_float
        + cos_a * normal_float
    )

    return {
        "valid": True,
        "along": int(round(along_float)),
        "normal": int(round(normal_float)),
        "ball_x": int(
            round(
                pipe["center_x"]
                + original_dx
                + ROI_X
            )
        ),
        "ball_y": int(
            round(
                pipe["center_y"]
                + original_dy
                + ROI_Y
            )
        ),
    }


def build_uart_packet(mapped, pipe):
    """AM: angle mapping。angle 字段放大 100 倍，避免串口解析浮点数。"""
    angle_x100 = int(
        round(
            pipe["angle_deg"]
            * 100.0
        )
    )
    pipe_x = int(
        round(
            pipe["center_x"]
            + ROI_X
        )
    )
    pipe_y = int(
        round(
            pipe["center_y"]
            + ROI_Y
        )
    )

    if not mapped["valid"]:
        return "AM,0,0,0,%d,-1,-1,%d,%d\n" % (
            angle_x100,
            pipe_x,
            pipe_y,
        )

    return "AM,1,%d,%d,%d,%d,%d,%d,%d\n" % (
        mapped["along"],
        mapped["normal"],
        angle_x100,
        mapped["ball_x"],
        mapped["ball_y"],
        pipe_x,
        pipe_y,
    )


def make_debug_view(straight_green, pipe):
    """截出旋正后的固定高度长条；copy 保证帧缓冲区从正确偏移开始。"""
    center_y = int(round(pipe["center_y"]))
    view_y0 = center_y - STRAIGHT_VIEW_HEIGHT // 2
    view_y0 = max(
        0,
        min(
            ROI_HEIGHT - STRAIGHT_VIEW_HEIGHT,
            view_y0,
        ),
    )

    return straight_green[
        view_y0:view_y0 + STRAIGHT_VIEW_HEIGHT,
        0:ROI_WIDTH,
    ].copy()


# ============================================================
# 6. UART 和 K230 主循环
# ============================================================

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
    clock = time.clock()
    calibrator = PipeCalibrator(
        CALIBRATION_VALID_FRAMES
    )
    pipe = None

    try:
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

        if STRAIGHT_BINARY_DEBUG:
            Display.init(
                DISPLAY_TYPE,
                width=ROI_WIDTH,
                height=STRAIGHT_VIEW_HEIGHT,
                to_ide=IDE_PREVIEW_ENABLE,
                quality=50,
            )

        sensor.run()
        time.sleep(0.5)

        print(
            "ANGLE TEST ROI=(%d,%d,%d,%d) calib=%d"
            % (
                ROI_X,
                ROI_Y,
                ROI_WIDTH,
                ROI_HEIGHT,
                CALIBRATION_VALID_FRAMES,
            )
        )
        print(
            "HSV H[%d,%d] S[%d,%d] V[%d,%d]"
            % (
                H_MIN,
                H_MAX,
                S_MIN,
                S_MAX,
                V_MIN,
                V_MAX,
            )
        )
        print(
            "debug=%s UART=%s"
            % (
                str(STRAIGHT_BINARY_DEBUG),
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
                ROI_Y:ROI_Y + ROI_HEIGHT,
                ROI_X:ROI_X + ROI_WIDTH,
            ]
            green_mask = make_green_mask(roi_frame)

            straight_green = None
            ball = None
            mapped = None
            packet = None
            debug_np = None
            debug_img = None

            if pipe is None:
                current_pipe = detect_pipe_geometry(
                    green_mask
                )

                if current_pipe is not None:
                    calibrator.add(current_pipe)
                    print(
                        "CAL %d/%d angle=%.2f center=(%d,%d)"
                        % (
                            calibrator.count,
                            calibrator.target_count,
                            current_pipe["angle_deg"],
                            int(round(current_pipe["center_x"])),
                            int(round(current_pipe["center_y"])),
                        )
                    )

                if calibrator.ready():
                    pipe = calibrator.result()
                    print(
                        "CAL OK angle=%.2f length=%.1f "
                        "thickness=%.1f center=(%.1f,%.1f)"
                        % (
                            pipe["angle_deg"],
                            pipe["length"],
                            pipe["thickness"],
                            pipe["center_x"],
                            pipe["center_y"],
                        )
                    )

                if STRAIGHT_BINARY_DEBUG:
                    # 标定期间先显示未旋正的中间 64 行。
                    preview_y0 = (
                        ROI_HEIGHT
                        - STRAIGHT_VIEW_HEIGHT
                    ) // 2
                    debug_np = green_mask[
                        preview_y0:
                        preview_y0 + STRAIGHT_VIEW_HEIGHT,
                        0:ROI_WIDTH,
                    ].copy()
            else:
                # 只旋转单通道二值图，比旋转 RGB 图更省内存和算力。
                straight_green = cv2.warpAffine(
                    green_mask,
                    pipe["rotate_matrix"],
                    (
                        ROI_WIDTH,
                        ROI_HEIGHT,
                    ),
                )
                ball = detect_ball_in_straight_mask(
                    straight_green,
                    pipe,
                )
                mapped = map_ball_result(
                    ball,
                    pipe,
                )
                packet = build_uart_packet(
                    mapped,
                    pipe,
                )

                if (
                    UART_ENABLE
                    and frame_id % SEND_EVERY_N_FRAMES == 0
                ):
                    uart.write(
                        packet.encode("utf-8")
                    )

                if STRAIGHT_BINARY_DEBUG:
                    debug_np = make_debug_view(
                        straight_green,
                        pipe,
                    )

            if (
                STRAIGHT_BINARY_DEBUG
                and debug_np is not None
            ):
                debug_img = image.Image(
                    ROI_WIDTH,
                    STRAIGHT_VIEW_HEIGHT,
                    image.GRAYSCALE,
                    alloc=image.ALLOC_REF,
                    data=debug_np,
                )
                Display.show_image(debug_img)

            if frame_id % GC_EVERY_N_FRAMES == 0:
                gc.collect()

            if (
                pipe is not None
                and packet is not None
                and frame_id % PRINT_EVERY_N_FRAMES == 0
            ):
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

            debug_img = None
            debug_np = None
            packet = None
            mapped = None
            ball = None
            straight_green = None
            green_mask = None
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

        if STRAIGHT_BINARY_DEBUG:
            Display.deinit()

        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
        time.sleep_ms(100)
        gc.collect()


if __name__ == "__main__":
    main()
