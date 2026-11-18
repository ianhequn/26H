"""K230 单文件测试版：绿色槽体、小球检测、像素误差和 UART2 输出。"""

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
# 1. 相机、屏幕、串口配置
# ============================================================

# 官方也建议实时 OpenCV 优先使用 320x240，先保证稳定再提高分辨率。
FRAME_WIDTH = 320
FRAME_HEIGHT = 240

# 相机固定后只处理中间横向长条。ROI 外的像素不会进入 HSV 和轮廓检测。
# 该范围覆盖前面两张 K230 二值图里的槽体上下摆动位置。
ROI_X = 0
ROI_Y = 65
ROI_WIDTH = 320
ROI_HEIGHT = 110

# 虚拟帧缓冲区也只创建ROI大小。
DISPLAY_WIDTH = ROI_WIDTH
DISPLAY_HEIGHT = ROI_HEIGHT
DISPLAY_TYPE = Display.VIRT
BINARY_DEBUG_MODE = False
NORMAL_COLOR_DISPLAY_ENABLE = True
DISPLAY_ENABLE = (
    BINARY_DEBUG_MODE
    or NORMAL_COLOR_DISPLAY_ENABLE
)
IDE_PREVIEW_ENABLE = True

# IO11/IO12 对应 UART2。只发送时实际上只需接 TX 和 GND。
UART_TX_PIN = 11
UART_RX_PIN = 12
UART_BAUDRATE = 115200

SEND_EVERY_N_FRAMES = 1
PRINT_EVERY_N_FRAMES = 30
GC_EVERY_N_FRAMES = (
    1 if BINARY_DEBUG_MODE else 10
)


# ============================================================
# 2. OpenCV 检测参数
# ============================================================

# OpenCV HSV：H 的范围是 0~179。
# 现场换光照后，优先调 S_MIN、V_MIN。
H_MIN = 35
H_MAX = 95
S_MIN = 60
S_MAX = 255
V_MIN = 35
V_MAX = 255

# 固定 ROI 中绿色像素足够多时，直接用图像矩求槽体轴线。
# 不再做槽体闭运算和槽体轮廓，减少每帧的计算量和内存分配。
RAIL_MIN_GREEN_PIXELS = 1200
RAIL_TARGET_X = ROI_WIDTH // 2
RAIL_MAX_ABS_ANGLE_DEG = 20.0
RAIL_AXIS_HALF_BAND = 18.0

# 小球是绿色槽体中的非绿色近圆形区域。
BALL_MIN_DIAMETER = 6
BALL_MAX_DIAMETER = 30
BALL_MIN_ASPECT = 0.50
BALL_MAX_ASPECT = 1.80
BALL_MIN_CIRCULARITY = 0.35
BALL_SURROUND_PADDING = 5
BALL_MIN_GREEN_SURROUND_RATIO = 0.22

# 固定相机下利用相邻帧连续性拒绝螺丝、反光点造成的坐标大跳。
BALL_MAX_FRAME_JUMP = 40.0
BALL_REACQUIRE_AFTER_MISSES = 5

# 与 SDK 示例一致：阈值数组只创建一次，禁止每帧反复分配。
COLOR_LO = np.array(
    [H_MIN, S_MIN, V_MIN],
    dtype=np.uint8,
)
COLOR_HI = np.array(
    [H_MAX, S_MAX, V_MAX],
    dtype=np.uint8,
)

# ============================================================
# 3. 基础工具函数
# ============================================================

def find_external_contours(binary):
    """兼容不同 OpenCV 版本的 findContours 返回格式。"""
    result = cv2.findContours(
        binary,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if len(result) == 3:
        return result[1]
    return result[0]


def find_all_contours(binary):
    """查找包含嵌套区域在内的全部轮廓。"""
    result = cv2.findContours(
        binary,
        cv2.RETR_LIST,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if len(result) == 3:
        return result[1]
    return result[0]


def make_green_mask(frame):
    """把绿色槽体分割为白色，其余区域为黑色。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, COLOR_LO, COLOR_HI)
    hsv = None
    return mask


# ============================================================
# 4. 用绿色像素矩估计槽体轴线
# ============================================================

def detect_rail(raw_green_mask):
    """用二值图的二阶矩求长条方向，并计算画面中线处的槽体中点。"""
    moments = cv2.moments(raw_green_mask)
    green_pixels = float(moments["m00"]) / 255.0

    if green_pixels < RAIL_MIN_GREEN_PIXELS:
        return None

    # cv2.moments 对 0/255 图像的 m00 带 255 倍，但各阶矩相除后会抵消。
    centroid_x = float(moments["m10"]) / float(moments["m00"])
    centroid_y = float(moments["m01"]) / float(moments["m00"])

    angle_rad = 0.5 * math.atan2(
        2.0 * float(moments["mu11"]),
        float(moments["mu20"]) - float(moments["mu02"]),
    )
    angle_deg = angle_rad * 180.0 / math.pi

    if abs(angle_deg) > RAIL_MAX_ABS_ANGLE_DEG:
        return None

    axis_x = math.cos(angle_rad)
    axis_y = math.sin(angle_rad)

    # 以 ROI 的水平中点作为槽体物理中点的 x 坐标；
    # y 坐标由当前轴线角度映射得到，避免倾斜后直接用矩形中心产生偏差。
    center_x = float(RAIL_TARGET_X)
    safe_axis_x = axis_x
    if abs(safe_axis_x) < 0.2:
        return None

    center_y = (
        centroid_y
        + (center_x - centroid_x) * axis_y / safe_axis_x
    )

    if center_y < 0.0 or center_y >= ROI_HEIGHT:
        return None

    estimated_height = int(
        max(
            12.0,
            min(
                50.0,
                green_pixels / float(ROI_WIDTH) * 2.0,
            ),
        )
    )
    bbox_y = int(round(center_y - estimated_height / 2.0))
    bbox_y = max(0, min(ROI_HEIGHT - estimated_height, bbox_y))

    return {
        "bbox": (
            0,
            bbox_y,
            ROI_WIDTH,
            estimated_height,
        ),
        "center": (
            int(round(center_x)),
            int(round(center_y)),
        ),
        "axis": (
            float(axis_x),
            float(axis_y),
        ),
        "angle_deg": float(angle_deg),
        "area": green_pixels,
    }


# ============================================================
# 5. 在槽体内部检测小球
# ============================================================


def detect_ball(raw_green_mask, rail, previous_center=None):
    """只接受槽体轴线附近、被绿色包围且与上一帧连续的圆斑。"""
    rail_x, rail_y = rail["center"]
    axis_x, axis_y = rail["axis"]

    # ROI 已经是固定中间长条。RETR_LIST 可取得绿色槽体内部的非绿色孔洞；
    # 后续再用轴线距离和绿色包围率排除 ROI 内的背景点。
    ball_mask = cv2.bitwise_not(raw_green_mask)
    best_ball = None
    best_score = -999.0

    for contour in find_all_contours(ball_mask):
        area = float(cv2.contourArea(contour))
        x, y, w, h = cv2.boundingRect(contour)

        if w <= 0 or h <= 0:
            continue

        diameter = max(w, h)
        if diameter < BALL_MIN_DIAMETER:
            continue
        if diameter > BALL_MAX_DIAMETER:
            continue

        aspect = float(w) / float(h)
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

        # 小球中心直接使用轮廓外接矩形的像素宽高计算，减少一次 moments。
        ball_x = float(x) + float(w) / 2.0
        ball_y = float(y) + float(h) / 2.0

        cross_distance = abs(
            -(ball_x - rail_x) * axis_y
            + (ball_y - rail_y) * axis_x
        )
        if cross_distance > RAIL_AXIS_HALF_BAND:
            continue

        pad = BALL_SURROUND_PADDING
        patch_x0 = max(0, x - pad)
        patch_y0 = max(0, y - pad)
        patch_x1 = min(ROI_WIDTH, x + w + pad)
        patch_y1 = min(ROI_HEIGHT, y + h + pad)
        patch_w = patch_x1 - patch_x0
        patch_h = patch_y1 - patch_y0

        if patch_w <= 0 or patch_h <= 0:
            continue

        green_patch = raw_green_mask[
            patch_y0:patch_y1,
            patch_x0:patch_x1,
        ]
        green_ratio = (
            float(cv2.countNonZero(green_patch))
            / float(patch_w * patch_h)
        )
        if green_ratio < BALL_MIN_GREEN_SURROUND_RATIO:
            continue

        frame_jump = 0.0
        if previous_center is not None:
            jump_x = ball_x - previous_center[0]
            jump_y = ball_y - previous_center[1]
            frame_jump = math.sqrt(
                jump_x * jump_x + jump_y * jump_y
            )
            if frame_jump > BALL_MAX_FRAME_JUMP:
                continue

        score = (
            2.0 * circularity
            - abs(aspect - 1.0)
            + 0.8 * green_ratio
            - 0.02 * frame_jump
        )

        if score > best_score:
            best_score = score

            best_ball = {
                "bbox": (
                    int(x),
                    int(y),
                    int(w),
                    int(h),
                ),
                "center": (
                    int(round(ball_x)),
                    int(round(ball_y)),
                ),
                "area": area,
                "aspect": aspect,
                "circularity": circularity,
                "green_ratio": green_ratio,
            }

    ball_mask = None
    return best_ball


# ============================================================
# 6. 完整检测、误差计算和串口数据包
# ============================================================

def detect_from_green_mask(
    frame,
    raw_green_mask,
    previous_center=None,
):
    """使用已经生成的绿色二值掩膜完成检测。

    dx = ball_x - rail_x
    dy = ball_y - rail_y

    dx < 0：小球在槽体中点左侧。
    dx > 0：小球在槽体中点右侧。
    """
    rail = detect_rail(raw_green_mask)

    result = {
        "valid": False,
        "rail": rail,
        "ball": None,
        "error": None,
    }

    if rail is None:
        return result

    ball = detect_ball(
        raw_green_mask,
        rail,
        previous_center,
    )
    result["ball"] = ball

    if ball is None:
        return result

    rail_x, rail_y = rail["center"]
    ball_x, ball_y = ball["center"]

    result["valid"] = True
    axis_x, axis_y = rail["axis"]
    offset_x = ball_x - rail_x
    offset_y = ball_y - rail_y

    # 把二维像素差投影到槽体坐标系：
    # error[0] 是沿槽体方向的位置误差，error[1] 是偏离槽体轴线的误差。
    result["error"] = (
        int(round(offset_x * axis_x + offset_y * axis_y)),
        int(round(-offset_x * axis_y + offset_y * axis_x)),
    )

    return result


def detect_green_rail_and_ball(frame, previous_center=None):
    """普通运行入口：内部创建绿色二值掩膜。"""
    raw_green_mask = make_green_mask(frame)
    result = detect_from_green_mask(
        frame,
        raw_green_mask,
        previous_center,
    )
    raw_green_mask = None
    return result


def add_roi_offset(result):
    """把ROI局部绝对坐标还原成320x240整帧坐标。

    dx/dy是两个中点之差，不需要额外修改。
    """
    rail = result["rail"]
    ball = result["ball"]

    if rail is not None:
        x, y, w, h = rail["bbox"]
        cx, cy = rail["center"]
        rail["bbox"] = (
            x + ROI_X,
            y + ROI_Y,
            w,
            h,
        )
        rail["center"] = (
            cx + ROI_X,
            cy + ROI_Y,
        )

    if ball is not None:
        x, y, w, h = ball["bbox"]
        cx, cy = ball["center"]
        ball["bbox"] = (
            x + ROI_X,
            y + ROI_Y,
            w,
            h,
        )
        ball["center"] = (
            cx + ROI_X,
            cy + ROI_Y,
        )

    return result


def build_uart_packet(result):
    """生成 STM32/上位机容易解析的固定字段 ASCII 数据包。"""
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


# ============================================================
# 7. UART2 初始化
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


# ============================================================
# 8. K230 实时主循环
# ============================================================

def main():
    sensor = None
    uart = None
    uart_fpioa = None
    frame_id = 0
    previous_ball_center = None
    ball_miss_count = 0
    clock = time.clock()

    try:
        os.exitpoint(os.EXITPOINT_ENABLE)

        # 二值图调试阶段不初始化UART，先把图像链路单独跑稳。
        if not BINARY_DEBUG_MODE:
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
                width=DISPLAY_WIDTH,
                height=DISPLAY_HEIGHT,
                to_ide=IDE_PREVIEW_ENABLE,
                quality=50,
            )

        sensor.run()
        time.sleep(0.5)

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
            "binary_debug=%s ROI=(%d,%d,%d,%d) display=%s"
            % (
                str(BINARY_DEBUG_MODE),
                ROI_X,
                ROI_Y,
                ROI_WIDTH,
                ROI_HEIGHT,
                str(DISPLAY_ENABLE),
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

            result = None
            binary_mask = None
            display_np = None
            display_img = None
            white_pixels = 0

            if BINARY_DEBUG_MODE:
                # 调试模式只做 HSV 分割，不进入轮廓和小球检测分支。
                binary_mask = make_green_mask(roi_frame)
                white_pixels = cv2.countNonZero(binary_mask)

                display_img = image.Image(
                    ROI_WIDTH,
                    ROI_HEIGHT,
                    image.GRAYSCALE,
                    alloc=image.ALLOC_REF,
                    data=binary_mask,
                )
            else:
                # 正常模式仍不画框，只计算坐标和发送串口。
                result = detect_green_rail_and_ball(
                    roi_frame,
                    previous_ball_center,
                )

                # 追踪坐标保持 ROI 局部坐标；连续丢失若干帧后再允许全局重捕获。
                if result["valid"]:
                    previous_ball_center = result["ball"]["center"]
                    ball_miss_count = 0
                else:
                    ball_miss_count += 1
                    if (
                        ball_miss_count
                        >= BALL_REACQUIRE_AFTER_MISSES
                    ):
                        previous_ball_center = None

                result = add_roi_offset(result)

                if DISPLAY_ENABLE:
                    # roi_frame是带起始偏移的ndarray视图，ALLOC_REF可能忽略
                    # 其ROI偏移。复制成连续缓冲后，显示内容才是真正的ROI。
                    display_np = roi_frame.copy()
                    display_img = image.Image(
                        ROI_WIDTH,
                        ROI_HEIGHT,
                        image.RGB888,
                        alloc=image.ALLOC_REF,
                        data=display_np,
                    )

            # 当前每帧通过硬件 UART2 发送一次。
            if (
                result is not None
                and frame_id % SEND_EVERY_N_FRAMES == 0
            ):
                packet = build_uart_packet(result)
                uart.write(packet.encode("utf-8"))

            if DISPLAY_ENABLE:
                Display.show_image(display_img)

            if frame_id % GC_EVERY_N_FRAMES == 0:
                gc.collect()

            # IDE 串口每 30 帧打印一次，方便先不接 STM32 调试。
            if frame_id % PRINT_EVERY_N_FRAMES == 0:
                try:
                    free_kb = gc.mem_free() // 1024
                except Exception:
                    free_kb = -1

                if BINARY_DEBUG_MODE:
                    print(
                        "MASK white=%d/%d fps=%.1f free=%dKB"
                        % (
                            white_pixels,
                            ROI_WIDTH * ROI_HEIGHT,
                            clock.fps(),
                            free_kb,
                        )
                    )
                else:
                    angle_deg = 999.0
                    if result["rail"] is not None:
                        angle_deg = result["rail"]["angle_deg"]

                    print(
                        "%s angle=%+.1f fps=%.1f free=%dKB"
                        % (
                            build_uart_packet(result).strip(),
                            angle_deg,
                            clock.fps(),
                            free_kb,
                        )
                    )

            result = None
            display_img = None
            display_np = None
            binary_mask = None
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
