"""
接收端 H.264 VDEC 硬件解码和 LCD 视频层。

【本模块在链路中的位置】

    TCP 收到 H.264
    -> 本模块等待 SPS/PPS + IDR
    -> K230 VDEC 硬件解码
    -> Display Video Layer 显示

触摸按钮不画进视频，而是由 touch_ui.py 放在独立 OSD Layer。
"""

import image
import time

from media.display import Display
from media.media import MediaManager
import media.vdecoder as vdecoder
from mpp.payload_struct import K_PT_H264

from common.frame_protocol import (
    FLAG_CODEC_HEADER,
    FLAG_KEY_FRAME,
    FLAG_P_FRAME,
)
from common.h264_annexb import extract_parameter_sets
from receiver.boot_log import append_boot_log
from receiver.config import (
    DISPLAY_DRIVER,
    DISPLAY_HEIGHT,
    DISPLAY_TEST_PATTERN_HOLD_MS,
    DISPLAY_TO_IDE,
    DISPLAY_WIDTH,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    VIDEO_X,
    VIDEO_Y,
)


class LiveVideoDecoder:
    """
    解码收到的 H.264，并把 VDEC 输出绑定到 LCD 视频层。

    重要状态：
        codec_header:
            最近收到的 SPS/PPS。
        synchronized:
            是否已经从有效 IDR 开始解码。

    新 TCP 连接不能直接从 P 帧开始显示，因为 P 帧依赖之前的参考帧。
    """

    def __init__(self):
        self.decoder = None
        self.started = False
        self.display_initialized = False
        self.media_initialized = False
        self.decoder_created = False
        self.decoder_started = False
        self.layer_bound = False
        self.synchronized = False
        self.codec_header = b""

    def _resolve_display_type(self):
        """
        把配置文件中的显示驱动名称映射成 CanMV Display 常量。

        用字符串做配置可以避免 receiver/config.py 导入硬件模块。
        """

        if DISPLAY_DRIVER == "ST7701":
            return Display.ST7701

        if DISPLAY_DRIVER == "LT9611":
            return Display.LT9611

        if DISPLAY_DRIVER == "VIRT":
            return Display.VIRT

        raise ValueError(
            "Unsupported DISPLAY_DRIVER: %s"
            % DISPLAY_DRIVER
        )

    def _create_display_test_pattern(self):
        """
        创建一张不依赖网络和 VDEC 的启动彩条。

        能看到彩条：
            LCD、Display 驱动和 OSD0 正常，后续黑屏应继续检查 UI 或 VDEC。
        完全看不到彩条：
            问题发生在 Display 初始化、冷启动时序或屏幕硬件层。
        """

        append_boot_log("creating display test pattern")
        canvas = image.Image(
            DISPLAY_WIDTH,
            DISPLAY_HEIGHT,
            image.RGB565,
        )
        append_boot_log("display test pattern canvas created")

        # 使用明显不同的颜色，避免把“显示了纯黑画面”误判为没有输出。
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
        append_boot_log("display test pattern drawing finished")
        return canvas

    def _show_display_test_pattern(self, canvas):
        """把已经创建好的彩条连续刷新到 OSD0。"""

        # 部分固件在 Display/MediaManager 刚初始化时可能丢掉第一帧，因此在
        # 自检时连续刷新。这里只发生在开机阶段，不影响之后的视频帧率。
        start_ms = time.ticks_ms()
        first_frame_shown = False

        while (
            time.ticks_diff(time.ticks_ms(), start_ms)
            < DISPLAY_TEST_PATTERN_HOLD_MS
        ):
            if not first_frame_shown:
                append_boot_log("calling first Display.show_image")

            Display.show_image(canvas)

            if not first_frame_shown:
                first_frame_shown = True
                append_boot_log("first Display.show_image returned")

            time.sleep_ms(100)

        append_boot_log("display test pattern finished")

    def start(self):
        """
        初始化 Display 和 H.264 VDEC。

        初始化顺序：
            Display.init()
            -> MediaManager.init()
            -> 显示启动彩条
            -> Decoder.create()
            -> VDEC 输出绑定到 Video Layer
            -> Decoder.start()

        这里采用庐山派当前文档的专用顺序。Display 必须在
        MediaManager.init() 之前初始化，VDEC 必须先 bind 再 start。
        """

        if self.started:
            return

        # 庐山派官方示例先创建 Image，再初始化 Display 和 MediaManager。
        test_pattern = self._create_display_test_pattern()

        display_type = self._resolve_display_type()
        Display.init(
            display_type,
            width=DISPLAY_WIDTH,
            height=DISPLAY_HEIGHT,
            # 当前 UI 只使用一个 OSD 层。该层对应 LAYER_OSD0。
            osd_num=1,
            to_ide=DISPLAY_TO_IDE,
        )
        self.display_initialized = True
        append_boot_log("Display initialized")

        MediaManager.init()
        self.media_initialized = True
        append_boot_log("MediaManager initialized")

        self._show_display_test_pattern(test_pattern)

        self.decoder = vdecoder.Decoder(K_PT_H264)
        self.decoder.create()
        self.decoder_created = True
        append_boot_log("VDEC created")

        bind_info = self.decoder.bind_info(
            x=VIDEO_X,
            y=VIDEO_Y,
            width=VIDEO_WIDTH,
            height=VIDEO_HEIGHT,
            chn=self.decoder.get_vdec_channel(),
        )
        Display.bind_layer(
            **bind_info,
            layer=Display.LAYER_VIDEO1
        )
        self.layer_bound = True
        append_boot_log("VDEC layer bound")

        self.decoder.start()
        self.decoder_started = True
        append_boot_log("VDEC started")

        self.started = True
        self.reset_stream_sync()
        print("Live decoder started.")

    def reset_stream_sync(self):
        """
        清除旧连接解码状态，要求新连接重新提供 SPS/PPS 和 IDR。

        发送端重启或 TCP 重连后，旧参考帧不再可信，必须重新同步。
        """

        self.synchronized = False
        self.codec_header = b""

    def decode_frame(self, flags, payload):
        """
        在解码条件满足时，把一个协议访问单元送入 VDEC。

        参数：
            flags:
                公共协议中的 FLAG_CODEC_HEADER、FLAG_KEY_FRAME、
                FLAG_P_FRAME 组合。
            payload:
                H.264 Annex-B bytes。

        首次同步规则：
            1. 收到参数集时先缓存 SPS/PPS。
            2. 同步前丢弃所有 P 帧。
            3. 收到 IDR 后，把缓存参数集放在 IDR 前一次送入 VDEC。
            4. 同步完成后才正常送入后续 I/P 帧。
        """

        if not self.started:
            return

        if flags & FLAG_CODEC_HEADER:
            extracted_header = extract_parameter_sets(payload)

            if extracted_header:
                self.codec_header = extracted_header

        if not self.synchronized:
            if not (flags & FLAG_KEY_FRAME):
                return

            if flags & FLAG_CODEC_HEADER:
                first_access_unit = payload
            elif self.codec_header:
                # 参数集和 IDR 可能由发送端分成两个协议单元。
                first_access_unit = self.codec_header + payload
            else:
                return

            self.decoder.decode(first_access_unit)
            self.synchronized = True
            print("Live decoder synchronized at IDR.")
            return

        if flags & (FLAG_KEY_FRAME | FLAG_P_FRAME):
            self.decoder.decode(payload)

    def stop(self):
        """
        按依赖关系反向释放 VDEC、Display 和媒体缓冲区。

        即使 start() 只执行到一半，本函数也会根据状态标志安全清理。
        """

        if (
            not self.started
            and not self.display_initialized
            and not self.media_initialized
            and self.decoder is None
        ):
            return

        if self.layer_bound:
            try:
                Display.unbind_layer(layer=Display.LAYER_VIDEO1)
            except BaseException as error:
                print("Display unbind failed:", error)
            self.layer_bound = False

        if self.decoder is not None:
            if self.decoder_started:
                try:
                    self.decoder.stop()
                except BaseException as error:
                    print("Decoder stop failed:", error)
                self.decoder_started = False

            if self.decoder_created:
                try:
                    self.decoder.destroy()
                except BaseException as error:
                    print("Decoder destroy failed:", error)
                self.decoder_created = False

        if self.display_initialized:
            try:
                Display.deinit()
            except BaseException as error:
                print("Display deinit failed:", error)
            self.display_initialized = False

        if self.media_initialized:
            # 庐山派官方清理顺序要求 Display.deinit() 后留出短暂时间，再
            # 释放全局媒体缓冲区。
            time.sleep_ms(100)

            try:
                MediaManager.deinit()
            except BaseException as error:
                print("MediaManager deinit failed:", error)
            self.media_initialized = False

        self.decoder = None
        self.started = False
        self.reset_stream_sync()
