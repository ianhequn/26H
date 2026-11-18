from libs.PipeLine import PipeLine
from libs.YOLO import YOLOv5

from yolov5_steel_ball_video import (
    LABELS,
    CONF_THRESH,
    NMS_THRESH,
)

import gc
import os
import time


KMODEL_PATH = "/data/steel_ball_yolov5n_224_roi.kmodel"

MODEL_INPUT_SIZE = [224, 224]
RGB888P_SIZE = [224, 224]
DISPLAY_MODE = "lcd"

MAX_BOXES_NUM = 6
PRINT_EVERY_N_FRAMES = 60
GC_EVERY_N_FRAMES = 300


if __name__ == "__main__":
    pipeline = PipeLine(
        rgb888p_size=RGB888P_SIZE,
        display_mode=DISPLAY_MODE,

        # 开启LCD
        enable_display=True,

        # AI通道中心裁剪
        ai_center_crop=True,

        # GC2093当前模式
        sensor_source_size=[1920, 1080],
    )

    yolo = None
    frame_id = 0
    clock = time.clock()

    try:
        pipeline.create(
            fps=60,
            to_ide=False,
        )

        yolo = YOLOv5(
            task_type="detect",
            mode="video",
            kmodel_path=KMODEL_PATH,
            labels=LABELS,
            rgb888p_size=RGB888P_SIZE,
            model_input_size=MODEL_INPUT_SIZE,
            display_size=pipeline.get_display_size(),
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

            # 获取AI通道224×224图像
            frame = pipeline.get_frame()

            # 模型推理
            result = yolo.run(frame)

            # 清除上一帧的框和文字
            pipeline.osd_img.clear()

            # 将检测结果映射到LCD坐标并绘制
            yolo.draw_result(
                result,
                pipeline.osd_img,
            )

            # 显示OSD图层
            pipeline.show_image()

            if frame_id % PRINT_EVERY_N_FRAMES == 0:
                print("frame:%d" % frame_id)
                print("fps:%.1f" % clock.fps())
                print("result:", result)
                print(
                    "AI crop:",
                    pipeline.get_ai_crop_param(),
                )

            if frame_id % GC_EVERY_N_FRAMES == 0:
                gc.collect()

    finally:
        if yolo is not None:
            yolo.deinit()

        pipeline.destroy()
        gc.collect()
