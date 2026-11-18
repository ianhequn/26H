from libs.PipeLine import PipeLine
from libs.YOLO import YOLOv5
import gc
import os
import time


LABELS = ["steel_ball"]

KMODEL_PATH = "/data/steel_ball_yolov5s_320.kmodel"
MODEL_INPUT_SIZE = [320, 320]

DISPLAY_MODE = "lcd"
RGB888P_SIZE = [640, 360]

CONF_THRESH = 0.25
NMS_THRESH = 0.45
MAX_BOXES_NUM = 20

PRINT_EVERY_N_FRAMES = 10
PRINT_RAW_RESULT = True


def draw_fps(osd_img, fps):
    text = "FPS: %.1f" % fps

    try:
        osd_img.draw_string_advanced(8, 8, 24, text, color=(255, 255, 0))
        return
    except Exception:
        pass

    try:
        osd_img.draw_string(8, 8, text, color=(255, 255, 0), scale=2)
    except Exception:
        pass


def _flat_numbers(det):
    values = []
    try:
        for item in det:
            if isinstance(item, (list, tuple)):
                for sub_item in item:
                    values.append(float(sub_item))
            else:
                values.append(float(item))
    except Exception:
        return None
    return values


def _valid_box(x1, y1, x2, y2):
    return (x2 > x1 and y2 > y1 and
            x1 > -80 and y1 > -80 and
            x2 < RGB888P_SIZE[0] + 80 and y2 < RGB888P_SIZE[1] + 80)


def _center_from_box(x1, y1, x2, y2):
    return (int((x1 + x2) / 2), int((y1 + y2) / 2))


def det_center(det):
    """
    输入:
        det: YOLO 返回的单个检测框。

    输出:
        (center_x, center_y) 或 None。

    说明:
        不同 CanMV YOLO 示例返回格式可能不同。这里同时兼容:
        [class_id, score, x1, y1, x2, y2]
        [x1, y1, x2, y2, score, class_id]
        [x, y, w, h, score, class_id]
        [class_id, score, x, y, w, h]
    """
    if isinstance(det, dict):
        for names in (("x1", "y1", "x2", "y2"),
                      ("xmin", "ymin", "xmax", "ymax")):
            try:
                x1 = float(det[names[0]])
                y1 = float(det[names[1]])
                x2 = float(det[names[2]])
                y2 = float(det[names[3]])
                if _valid_box(x1, y1, x2, y2):
                    return _center_from_box(x1, y1, x2, y2)
            except Exception:
                pass
        return None

    values = _flat_numbers(det)
    if values is None:
        return None

    if len(values) < 6:
        return None

    candidates = (
        # [class_id, score, x1, y1, x2, y2]
        (values[2], values[3], values[4], values[5]),
        # [x1, y1, x2, y2, score, class_id]
        (values[0], values[1], values[2], values[3]),
        # [x, y, w, h, score, class_id]
        (values[0], values[1], values[0] + values[2], values[1] + values[3]),
        # [class_id, score, x, y, w, h]
        (values[2], values[3], values[2] + values[4], values[3] + values[5]),
    )

    for x1, y1, x2, y2 in candidates:
        if _valid_box(x1, y1, x2, y2):
            return _center_from_box(x1, y1, x2, y2)

    return None


def print_result(frame_id, result):
    if result is None or len(result) == 0:
        print("frame:%d raw:0 balls:0 centers:[]" % frame_id)
        return

    centers = []
    for det in result:
        center = det_center(det)
        if center is not None:
            centers.append(center)

    print("frame:%d raw:%d balls:%d centers:%s" %
          (frame_id, len(result), len(centers), str(centers)))
    if PRINT_RAW_RESULT:
        print("raw:", result)


if __name__ == "__main__":
    if "MODEL_INPUT_SIZE" not in globals():
        MODEL_INPUT_SIZE = [320, 320]

    pipeline = PipeLine(rgb888p_size=RGB888P_SIZE, display_mode=DISPLAY_MODE)
    yolo = None
    frame_id = 0
    clock = time.clock()

    try:
        pipeline.create()
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
            frame = pipeline.get_frame()
            result = yolo.run(frame)
            yolo.draw_result(result, pipeline.osd_img)
            draw_fps(pipeline.osd_img, clock.fps())
            pipeline.show_image()

            if frame_id % PRINT_EVERY_N_FRAMES == 0:
                print_result(frame_id, result)

            gc.collect()
    finally:
        if yolo is not None:
            yolo.deinit()
        pipeline.destroy()
        gc.collect()
