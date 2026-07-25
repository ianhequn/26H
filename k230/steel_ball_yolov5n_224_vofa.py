from libs.PipeLine import PipeLine
from libs.YOLO import YOLOv5

import gc
import os
import time


# =========================================================
# 模型与图像配置
# =========================================================

LABELS = ["steel_ball"]

KMODEL_PATH = "/data/steel_ball_yolov5n_224_roi.kmodel"

MODEL_INPUT_SIZE = [224, 224]
RGB888P_SIZE = [224, 224]

# GC2093 当前原始输出
SENSOR_SOURCE_SIZE = [1920, 1080]
#摄像头的输出
DISPLAY_MODE = "virt"
#mv输出
CONF_THRESH = 0.25
#置信度过滤
NMS_THRESH = 0.45
MAX_BOXES_NUM = 5

MAX_SEND_BALLS = 5
VOFA_SEND_EVERY_N_FRAMES = 2
VOFA_OUTPUT_ENABLE = True
DEBUG_PRINT_ENABLE = False

PRINT_EVERY_N_FRAMES = 60
GC_EVERY_N_FRAMES = 100
#300s回收
# 是否在LCD上画出AI真实检测区域
DRAW_AI_ROI = True


# =========================================================
# 绘制FPS
# =========================================================

def draw_fps(osd_img, fps):
    text = "FPS: %.1f" % fps

    try:
        osd_img.draw_string_advanced(
            8,
            8,
            24,
            text,
            color=(255, 255, 0),
        )
        return
    except Exception:
        pass

    try:
        osd_img.draw_string(
            8,
            8,
            text,
            color=(255, 255, 0),
            scale=2,
        )
    except Exception:
        pass


# =========================================================
# 将224×224 AI坐标映射到完整LCD坐标
# =========================================================

def ai_box_to_display(
    box,
    ai_crop_param,
    display_size,
):
    """
    输入：
        box:
            YOLO返回的 [x, y, w, h]
            当前坐标范围为224×224

        ai_crop_param:
            [crop_x, crop_y, crop_w, crop_h]
            对于当前配置应为：
            [420, 0, 1080, 1080]

        display_size:
            LCD实际尺寸，例如800×480

    输出：
        LCD坐标系中的：
        [display_x, display_y, display_w, display_h]
    """

    x = float(box[0])
    y = float(box[1])
    w = float(box[2])
    h = float(box[3])

    ai_width = float(RGB888P_SIZE[0])
    ai_height = float(RGB888P_SIZE[1])

    crop_x = float(ai_crop_param[0])
    crop_y = float(ai_crop_param[1])
    crop_width = float(ai_crop_param[2])
    crop_height = float(ai_crop_param[3])

    source_width = float(SENSOR_SOURCE_SIZE[0])
    source_height = float(SENSOR_SOURCE_SIZE[1])

    display_width = float(display_size[0])
    display_height = float(display_size[1])

    # -----------------------------------------------------
    # 第一步：224×224 AI坐标 → 1920×1080传感器坐标
    # -----------------------------------------------------

    source_x = crop_x + x * crop_width / ai_width
    source_y = crop_y + y * crop_height / ai_height

    source_box_width = w * crop_width / ai_width
    source_box_height = h * crop_height / ai_height

    # -----------------------------------------------------
    # 第二步：1920×1080传感器坐标 → LCD坐标
    # -----------------------------------------------------

    display_x = int(
        round(
            source_x *
            display_width /
            source_width
        )
    )

    display_y = int(
        round(
            source_y *
            display_height /
            source_height
        )
    )

    display_box_width = int(
        round(
            source_box_width *
            display_width /
            source_width
        )
    )

    display_box_height = int(
        round(
            source_box_height *
            display_height /
            source_height
        )
    )

    return [
        display_x,
        display_y,
        display_box_width,
        display_box_height,
    ]


# =========================================================
# 获取AI检测区域在LCD中的位置
# =========================================================

def ai_roi_to_display(
    ai_crop_param,
    display_size,
):
    crop_x = float(ai_crop_param[0])
    crop_y = float(ai_crop_param[1])
    crop_width = float(ai_crop_param[2])
    crop_height = float(ai_crop_param[3])

    source_width = float(SENSOR_SOURCE_SIZE[0])
    source_height = float(SENSOR_SOURCE_SIZE[1])

    display_width = float(display_size[0])
    display_height = float(display_size[1])

    x = int(
        round(
            crop_x *
            display_width /
            source_width
        )
    )

    y = int(
        round(
            crop_y *
            display_height /
            source_height
        )
    )

    w = int(
        round(
            crop_width *
            display_width /
            source_width
        )
    )

    h = int(
        round(
            crop_height *
            display_height /
            source_height
        )
    )

    return [x, y, w, h]


# =========================================================
# 限制框不越过LCD边界
# =========================================================

def clamp_box(box, display_size):
    x = int(box[0])
    y = int(box[1])
    w = int(box[2])
    h = int(box[3])

    display_width = int(display_size[0])
    display_height = int(display_size[1])

    if x < 0:
        w += x
        x = 0

    if y < 0:
        h += y
        y = 0

    if x >= display_width:
        x = display_width - 1
        w = 1

    if y >= display_height:
        y = display_height - 1
        h = 1

    if x + w > display_width:
        w = display_width - x

    if y + h > display_height:
        h = display_height - y

    if w < 1:
        w = 1

    if h < 1:
        h = 1

    return [x, y, w, h]


# =========================================================
# 绘制检测结果
# =========================================================

def draw_detection_result(
    result,
    osd_img,
    yolo,
    ai_crop_param,
    display_size,
):
    osd_img.clear()

    # -----------------------------------------------------
    # 显示AI真正检测的区域
    # -----------------------------------------------------

    if DRAW_AI_ROI:
        roi_box = ai_roi_to_display(
            ai_crop_param,
            display_size,
        )

        osd_img.draw_rectangle(
            roi_box[0],
            roi_box[1],
            roi_box[2],
            roi_box[3],
            color=(255, 255, 0),
            thickness=2,
        )

    centers_sensor = []

    # YOLOv5返回格式：
    # result[0] = boxes
    # result[1] = class_ids
    # result[2] = scores

    if result is None:
        return centers_sensor

    if len(result) < 3:
        return centers_sensor

    if len(result[0]) == 0:
        return centers_sensor

    for i in range(len(result[0])):
        ai_box = result[0][i]

        class_id = int(result[1][i])
        score = float(result[2][i])

        display_box = ai_box_to_display(
            ai_box,
            ai_crop_param,
            display_size,
        )

        display_box = clamp_box(
            display_box,
            display_size,
        )

        x = display_box[0]
        y = display_box[1]
        w = display_box[2]
        h = display_box[3]

        try:
            color = yolo.colors[class_id]
        except Exception:
            color = (0, 255, 0)

        osd_img.draw_rectangle(
            x,
            y,
            w,
            h,
            color=color,
            thickness=4,
        )

        text_y = y - 28

        if text_y < 0:
            text_y = y

        label_text = "%s %.3f" % (
            LABELS[class_id],
            score,
        )

        try:
            osd_img.draw_string_advanced(
                x,
                text_y,
                24,
                label_text,
                color=color,
            )
        except Exception:
            pass

        # -------------------------------------------------
        # 计算钢球中心在1920×1080传感器坐标中的位置
        # 后面给控制器或串口使用更方便
        # -------------------------------------------------

        ai_x = float(ai_box[0])
        ai_y = float(ai_box[1])
        ai_w = float(ai_box[2])
        ai_h = float(ai_box[3])

        crop_x = float(ai_crop_param[0])
        crop_y = float(ai_crop_param[1])
        crop_w = float(ai_crop_param[2])
        crop_h = float(ai_crop_param[3])

        center_x_sensor = int(
            crop_x +
            (ai_x + ai_w / 2) *
            crop_w /
            RGB888P_SIZE[0]
        )

        center_y_sensor = int(
            crop_y +
            (ai_y + ai_h / 2) *
            crop_h /
            RGB888P_SIZE[1]
        )

        centers_sensor.append(
            (
                center_x_sensor,
                center_y_sensor,
            )
        )

    return centers_sensor


# =========================================================
# 主程序
# =========================================================

def build_vofa_packet(centers_sensor, result):
    """
    Build one fixed-length VOFA+ FireWater packet.

    Ball 0 to ball 4 only mean the left-to-right order in the current
    frame. They are not persistent IDs for the same physical balls,
    and their slot numbers may change between frames.
    """

    balls = []

    if (
        centers_sensor is not None and
        result is not None and
        len(result) >= 3 and
        result[2] is not None
    ):
        pair_count = min(
            len(centers_sensor),
            len(result[2]),
        )

        for i in range(pair_count):
            center = centers_sensor[i]

            if center is None or len(center) < 2:
                continue

            try:
                center_x = int(center[0])
                center_y = int(center[1])
                score = float(result[2][i])
            except Exception:
                continue

            balls.append(
                (
                    center_x,
                    center_y,
                    score,
                )
            )

    # 球0到球4只是当前帧从左到右的顺序，不代表同一个钢球的永久身份。
    # 本程序不做目标跟踪、不保存上一帧坐标，也不分配跨帧固定ID。
    balls.sort(
        key=lambda ball: ball[0],
    )
    balls = balls[:MAX_SEND_BALLS]

    count = len(balls)
    values = [
        str(count),
    ]

    for center_x, center_y, score in balls:
        values.extend(
            [
                str(center_x),
                str(center_y),
                "%.3f" % score,
            ]
        )

    for _ in range(MAX_SEND_BALLS - count):
        values.extend(
            [
                "-1",
                "-1",
                "0",
            ]
        )

    return "balls:" + ",".join(values) + "\n"


def send_vofa_packet(centers_sensor, result):
    packet = build_vofa_packet(
        centers_sensor,
        result,
    )

    # print()通过K230 USB调试串口输出，不配置FPIOA或硬件UART引脚。
    print(
        packet,
        end="",
    )


if __name__ == "__main__":
    pipeline = PipeLine(
        rgb888p_size=RGB888P_SIZE,
        display_mode=DISPLAY_MODE,

        enable_display=True,

        # AI通道中心裁剪
        ai_center_crop=True,

        sensor_source_size=SENSOR_SOURCE_SIZE,
    )

    yolo = None
    frame_id = 0
    clock = time.clock()

    try:
        pipeline.create(
            fps=60,
            to_ide=True,
        )

        display_size = pipeline.get_display_size()
        ai_crop_param = pipeline.get_ai_crop_param()

        if DEBUG_PRINT_ENABLE and not VOFA_OUTPUT_ENABLE:
            print("display size:", display_size)
            print("AI crop:", ai_crop_param)

        yolo = YOLOv5(
            task_type="detect",
            mode="video",
            kmodel_path=KMODEL_PATH,
            labels=LABELS,

            rgb888p_size=RGB888P_SIZE,
            model_input_size=MODEL_INPUT_SIZE,

            # ------------------------------------------------
            # 关键：
            # 让YOLO后处理输出224×224坐标，
            # 不要直接映射到整个LCD。
            # ------------------------------------------------
            display_size=RGB888P_SIZE,

            conf_thresh=CONF_THRESH,
            nms_thresh=NMS_THRESH,
            max_boxes_num=MAX_BOXES_NUM,
            debug_mode=0,
        )

        yolo.config_preprocess()

        while True:
            os.exitpoint()
            clock.tick()

            frame_id += 1

            frame = pipeline.get_frame()

            result = yolo.run(frame)

            centers_sensor = draw_detection_result(
                result,
                pipeline.osd_img,
                yolo,
                ai_crop_param,
                display_size,
            )

            if (
                VOFA_OUTPUT_ENABLE and
                frame_id % VOFA_SEND_EVERY_N_FRAMES == 0
            ):
                send_vofa_packet(
                    centers_sensor,
                    result,
                )

            draw_fps(
                pipeline.osd_img,
                clock.fps(),
            )

            pipeline.show_image()

            if (
                DEBUG_PRINT_ENABLE and
                not VOFA_OUTPUT_ENABLE and
                frame_id % PRINT_EVERY_N_FRAMES == 0
            ):
                if (
                    result is not None and
                    len(result) >= 3
                ):
                    box_count = len(result[0])
                else:
                    box_count = 0

                print(
                    "frame:%d balls:%d centers:%s fps:%.1f"
                    % (
                        frame_id,
                        box_count,
                        str(centers_sensor),
                        clock.fps(),
                    )
                )

                if box_count > 0:
                    print("boxes in AI coords:", result[0])
                    print("scores:", result[2])

            if frame_id % GC_EVERY_N_FRAMES == 0:
                gc.collect()

    finally:
        if yolo is not None:
            yolo.deinit()

        pipeline.destroy()
        gc.collect()
