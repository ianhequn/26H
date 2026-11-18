"""
接收端 ST7701 屏幕独立自检。

用途：
    只验证 Display、LCD 和 OSD0，不启动热点、TCP、VDEC、触摸或录像。

在 CanMV IDE 中打开本文件并点击“运行”。若屏幕持续显示四色彩条，
显示硬件和基础 Display API 正常；若仍然黑屏，就不必先排查网络代码。
"""

import image
import os
import time

from media.display import Display
from media.media import MediaManager


# =========================================================
# [常量] 屏幕参数
# =========================================================

DISPLAY_WIDTH = 800
DISPLAY_HEIGHT = 480

# 冷启动或软复位后给 LCD 驱动留出的稳定时间，单位：秒。
STARTUP_DELAY_S = 3


def create_test_pattern():
    """创建一张不带透明通道的 RGB565 四色测试图。"""

    canvas = image.Image(
        DISPLAY_WIDTH,
        DISPLAY_HEIGHT,
        image.RGB565,
    )
    colors = (
        (220, 45, 55),
        (245, 175, 45),
        (30, 190, 105),
        (45, 125, 220),
    )
    bar_width = DISPLAY_WIDTH // len(colors)

    for index in range(len(colors)):
        canvas.draw_rectangle(
            index * bar_width,
            0,
            bar_width,
            DISPLAY_HEIGHT,
            color=colors[index],
            fill=True,
        )

    canvas.draw_rectangle(
        130,
        185,
        540,
        110,
        color=(18, 24, 31),
        fill=True,
    )
    canvas.draw_string_advanced(
        205,
        215,
        34,
        "K230 DISPLAY OK",
        color=(255, 255, 255),
    )
    return canvas


def run():
    """
    初始化 ST7701 并持续显示测试图。

    finally 在 IDE 停止脚本时释放 Display，避免下一次调试继承旧状态。
    """

    os.exitpoint(os.EXITPOINT_ENABLE)
    time.sleep(STARTUP_DELAY_S)
    display_initialized = False
    media_initialized = False

    try:
        # 庐山派官方示例先创建 Image，再初始化 Display 和 MediaManager。
        print("Creating display test pattern...")
        test_pattern = create_test_pattern()
        print("Display test pattern created.")

        Display.init(
            Display.ST7701,
            width=DISPLAY_WIDTH,
            height=DISPLAY_HEIGHT,
            osd_num=1,
            to_ide=True,
        )
        display_initialized = True

        # 庐山派固件要求 Display 先初始化，再初始化全局媒体缓冲区。
        MediaManager.init()
        media_initialized = True

        print("Display smoke test is showing.")

        while True:
            os.exitpoint()
            # 连续刷新，避免把启动时丢失的单次 show_image 误判成屏幕故障。
            Display.show_image(test_pattern)
            time.sleep_ms(100)

    except KeyboardInterrupt:
        print("Display smoke test stopped.")

    finally:
        if display_initialized:
            Display.deinit()

        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
        time.sleep_ms(100)

        if media_initialized:
            MediaManager.deinit()


if __name__ == "__main__":
    run()
