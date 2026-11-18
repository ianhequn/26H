"""
接收端最小触摸 UI。

视频由 VDEC 显示在 Video Layer，本模块只在上层 OSD Layer 绘制：

- 网络状态和接收 FPS。
- SD 卡剩余空间。
- 录像状态。
- REC 和 STOP 两个触摸按钮。

UI 只产生“开始录像/停止录像”命令，不直接操作 MP4 或网络。
"""

import image
import time

from machine import TOUCH
from media.display import Display

from receiver.config import (
    DISPLAY_HEIGHT,
    DISPLAY_WIDTH,
    TOUCH_DEBOUNCE_MS,
)


# =========================================================
# [常量] UI 固定布局，单位：像素
# =========================================================

# 顶部状态栏高度。
TOP_BAR_HEIGHT = 38

# 底部工具栏的 y 坐标和高度。
BOTTOM_BAR_Y = 400
BOTTOM_BAR_HEIGHT = 80

# 两个命令按钮共用的尺寸。
BUTTON_Y = 412
BUTTON_WIDTH = 132
BUTTON_HEIGHT = 52

# REC 和 STOP 按钮的左上角 x 坐标。
REC_BUTTON_X = 24
STOP_BUTTON_X = 172

# [常量] image 绘图使用的 RGB 颜色。
COLOR_WHITE = (255, 255, 255)
COLOR_MUTED = (170, 180, 190)
COLOR_BAR = (18, 24, 31)
COLOR_BORDER = (90, 105, 120)
COLOR_RECORD = (210, 45, 55)
COLOR_STOP = (55, 70, 85)
COLOR_CONNECTED = (30, 200, 110)
COLOR_WAITING = (245, 175, 45)


class TouchUi:
    """
    绘制状态，并把触摸坐标映射成高层回调。

    构造参数：
        on_record:
            点击 REC 时调用的无参数函数。
        on_stop:
            点击 STOP 时调用的无参数函数。
        status_provider:
            返回连接状态、录像状态、FPS 和剩余空间的函数。

    本类不知道网络、解码和录像内部如何实现。
    """

    def __init__(
        self,
        on_record,
        on_stop,
        status_provider,
    ):
        self.on_record = on_record
        self.on_stop = on_stop
        self.status_provider = status_provider

        self.touch = TOUCH(0)
        self.canvas = image.Image(
            DISPLAY_WIDTH,
            DISPLAY_HEIGHT,
            image.ARGB8888,
        )
        self.last_touch_ms = 0

    def _inside(self, x, y, left, top, width, height):
        """判断触摸坐标是否落在指定矩形内部。"""

        return (
            left <= x < left + width
            and top <= y < top + height
        )

    def _draw_button(self, x, label, color):
        """
        绘制固定尺寸命令按钮。

        固定尺寸可以保证文字和按压状态变化时，工具栏不会发生布局跳动。
        """

        self.canvas.draw_rectangle(
            x,
            BUTTON_Y,
            BUTTON_WIDTH,
            BUTTON_HEIGHT,
            color=COLOR_BORDER,
            fill=True,
        )
        self.canvas.draw_rectangle(
            x + 2,
            BUTTON_Y + 2,
            BUTTON_WIDTH - 4,
            BUTTON_HEIGHT - 4,
            color=color,
            fill=True,
        )
        self.canvas.draw_string_advanced(
            x + 34,
            BUTTON_Y + 13,
            24,
            label,
            color=COLOR_WHITE,
        )

    def redraw(self):
        """
        根据应用最新状态快照重绘 OSD。

        canvas.clear() 会清空非工具栏区域，使中间视频仍可从透明 OSD 下看到。
        Display.show_image() 只更新 OSD，不影响 VDEC 视频层。
        """

        status = self.status_provider()
        connection_text = status[0]
        recorder_text = status[1]
        fps = status[2]
        free_space_mb = status[3]

        self.canvas.clear()

        self.canvas.draw_rectangle(
            0,
            0,
            DISPLAY_WIDTH,
            TOP_BAR_HEIGHT,
            color=COLOR_BAR,
            fill=True,
        )
        self.canvas.draw_rectangle(
            0,
            BOTTOM_BAR_Y,
            DISPLAY_WIDTH,
            BOTTOM_BAR_HEIGHT,
            color=COLOR_BAR,
            fill=True,
        )

        if connection_text == "LIVE":
            status_color = COLOR_CONNECTED
        else:
            status_color = COLOR_WAITING

        self.canvas.draw_circle(
            18,
            19,
            7,
            color=status_color,
            fill=True,
        )
        self.canvas.draw_string_advanced(
            34,
            7,
            22,
            connection_text,
            color=COLOR_WHITE,
        )

        self.canvas.draw_string_advanced(
            195,
            7,
            22,
            "FPS %.1f" % fps,
            color=COLOR_MUTED,
        )
        self.canvas.draw_string_advanced(
            360,
            7,
            22,
            "SD %d MB" % free_space_mb,
            color=COLOR_MUTED,
        )
        self.canvas.draw_string_advanced(
            600,
            7,
            22,
            recorder_text,
            color=COLOR_WHITE,
        )

        self._draw_button(
            REC_BUTTON_X,
            "REC",
            COLOR_RECORD,
        )
        self._draw_button(
            STOP_BUTTON_X,
            "STOP",
            COLOR_STOP,
        )

        self.canvas.draw_string_advanced(
            340,
            423,
            20,
            "Two-K230 Receiver",
            color=COLOR_MUTED,
        )

        Display.show_image(
            self.canvas,
            # Display.init(osd_num=1) 创建的是第一个 OSD 层，即 OSD0。
            # 使用 OSD1 在部分固件中不会报错，但图像不会真正显示。
            layer=Display.LAYER_OSD0,
            alpha=255,
        )

    def poll(self):
        """
        读取一次触摸事件，并触发对应命令回调。

        只响应按下事件，并使用 TOUCH_DEBOUNCE_MS 防抖，避免手指按住按钮时
        在主循环中连续创建或关闭多个录像文件。
        """

        points = self.touch.read(1)

        if not points:
            return

        point = points[0]
        current_ms = time.ticks_ms()

        if (
            time.ticks_diff(
                current_ms,
                self.last_touch_ms,
            )
            < TOUCH_DEBOUNCE_MS
        ):
            return

        if (
            point.event != 0
            and point.event != TOUCH.EVENT_DOWN
        ):
            return

        if self._inside(
            point.x,
            point.y,
            REC_BUTTON_X,
            BUTTON_Y,
            BUTTON_WIDTH,
            BUTTON_HEIGHT,
        ):
            self.last_touch_ms = current_ms
            self.on_record()
            return

        if self._inside(
            point.x,
            point.y,
            STOP_BUTTON_X,
            BUTTON_Y,
            BUTTON_WIDTH,
            BUTTON_HEIGHT,
        ):
            self.last_touch_ms = current_ms
            self.on_stop()

    def deinit(self):
        """固件提供 deinit() 时释放触摸设备资源。"""

        try:
            self.touch.deinit()
        except BaseException:
            pass
