"""
接收端顶层应用和模块装配点。

只有本文件知道所有模块：

    Wi-Fi AP
    TCP Server
    H.264 VDEC
    MP4 Recorder
    Touch UI

各底层模块之间不互相导入，通过回调和简单数据传递完成解耦。
"""

import gc
import os
import time

from common.frame_protocol import (
    FLAG_KEY_FRAME,
    FLAG_P_FRAME,
)
from receiver.config import (
    AUTO_RECORD_ON_CONNECT,
    UI_REFRESH_INTERVAL_MS,
)
from receiver.boot_log import append_boot_log
from receiver.live_decoder import LiveVideoDecoder
from receiver.mp4_recorder import Mp4Recorder
from receiver.storage import get_free_space_mb
from receiver.tcp_server import VideoFrameServer
from receiver.touch_ui import TouchUi
from receiver.wifi_ap import (
    start_access_point,
    stop_access_point,
)


class ReceiverApplication:
    """
    组合彼此独立的网络、解码、录像和 UI 模块。

    主线程：
        轮询触摸并刷新 OSD。

    网络线程：
        accept/recv
        -> _on_frame()
        -> 同一份 H.264 分发给 VDEC 和 MP4 Recorder。
    """

    def __init__(self):
        self.access_point = None
        self.decoder = LiveVideoDecoder()
        self.recorder = Mp4Recorder()
        self.server = None
        self.ui = None

        self.connection_state = "WAIT"
        self.received_video_frames = 0
        self.fps_window_frames = 0
        self.fps_window_start_ms = time.ticks_ms()
        self.measured_fps = 0.0

    def _on_connected(self, client_address):
        """
        TCP 发送端建立连接后的回调。

        新连接必须清除旧 H.264 参考帧状态，并重新统计 FPS。
        """

        self.connection_state = "LIVE"
        self.decoder.reset_stream_sync()
        self.fps_window_frames = 0
        self.fps_window_start_ms = time.ticks_ms()
        print("Video source online:", client_address)

        if AUTO_RECORD_ON_CONNECT:
            self.recorder.start()

    def _on_disconnected(self):
        """
        当前发送端断开后的回调。

        若正在录像，立即按正常流程关闭 MP4，尽量保住断线前已经收到的内容；
        然后 UI 回到 WAIT，VDEC 等待新连接的 SPS/PPS + IDR。
        """

        if self.connection_state == "LIVE":
            print("Video source offline.")

        self.connection_state = "WAIT"
        self.decoder.reset_stream_sync()

        recorder_state = self.recorder.get_status()[0]

        if recorder_state not in ("IDLE", "ERROR"):
            self.recorder.stop()

    def _on_network_error(self, error):
        """
        记录网络错误。

        重新 accept 由 VideoFrameServer 自己负责，应用层不重复创建 socket。
        """

        print("Network receive error:", error)

    def _update_fps(self, flags):
        """
        只使用实际 I/P 视频帧统计接收 FPS。

        单独 SPS/PPS 是协议单元，但不是一幅视频画面，不能计入 FPS。
        """

        if not (flags & (FLAG_KEY_FRAME | FLAG_P_FRAME)):
            return

        self.received_video_frames += 1
        self.fps_window_frames += 1
        current_ms = time.ticks_ms()
        elapsed_ms = time.ticks_diff(
            current_ms,
            self.fps_window_start_ms,
        )

        if elapsed_ms >= 1000:
            self.measured_fps = (
                self.fps_window_frames * 1000.0
            ) / elapsed_ms
            self.fps_window_frames = 0
            self.fps_window_start_ms = current_ms

    def _on_frame(
        self,
        flags,
        frame_id,
        timestamp_ms,
        payload,
    ):
        """
        把一份压缩帧分发到显示分支和录像分支。

        参数：
            flags:
                公共协议帧类型。
            frame_id:
                发送端递增编号，当前用于诊断，后续可统计丢帧。
            timestamp_ms:
                发送端生成的显示时间戳，录像模块使用。
            payload:
                完整 H.264 Annex-B 访问单元。

        注意：
            这里没有重新编码。decoder 和 recorder 消费的是同一份 payload。
        """

        self._update_fps(flags)
        self.decoder.decode_frame(flags, payload)
        self.recorder.process_frame(
            flags,
            timestamp_ms,
            payload,
        )

    def _start_recording(self):
        """
        REC 触摸命令。

        发送端不在线时拒绝创建空录像；在线时让 Recorder 进入 WAIT_IDR。
        """

        if self.connection_state != "LIVE":
            print("Cannot record: transmitter is offline.")
            return

        self.recorder.start()

    def _stop_recording(self):
        """STOP 触摸命令：正确关闭并发布当前录像。"""

        self.recorder.stop()

    def _status_snapshot(self):
        """
        生成 OSD 读取的小型不可变状态快照。

        返回：
            tuple:
                (连接状态, 录像状态, FPS, SD 剩余 MiB)
        """

        recorder_state = self.recorder.get_status()[0]
        return (
            self.connection_state,
            recorder_state,
            self.measured_fps,
            get_free_space_mb(),
        )

    def start(self):
        """
        按依赖顺序初始化接收端全部子系统。

        顺序：
            建立热点
            -> 初始化显示和 VDEC
            -> 创建触摸 UI
            -> 启动 TCP 接收线程
        """

        append_boot_log("starting Wi-Fi AP")
        self.access_point = start_access_point()
        append_boot_log("Wi-Fi AP started")

        append_boot_log("starting VDEC and Display")
        self.decoder.start()
        append_boot_log("VDEC and Display started")

        append_boot_log("starting touch UI")
        self.ui = TouchUi(
            on_record=self._start_recording,
            on_stop=self._stop_recording,
            status_provider=self._status_snapshot,
        )
        self.ui.redraw()
        append_boot_log("touch UI started")

        append_boot_log("starting TCP server")
        self.server = VideoFrameServer(
            on_frame=self._on_frame,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_error=self._on_network_error,
        )
        self.server.start()
        append_boot_log("TCP server started")

    def run_forever(self):
        """
        主线程持续轮询触摸并定期刷新 OSD。

        网络接收和 VDEC 送帧在后台线程执行，所以这里每 20 ms 休眠一次不会
        把视频帧率限制为 UI 刷新率。
        """

        last_ui_refresh_ms = 0

        while True:
            os.exitpoint()
            self.ui.poll()

            current_ms = time.ticks_ms()

            if (
                time.ticks_diff(
                    current_ms,
                    last_ui_refresh_ms,
                )
                >= UI_REFRESH_INTERVAL_MS
            ):
                self.ui.redraw()
                last_ui_refresh_ms = current_ms

            time.sleep_ms(20)

    def stop(self):
        """
        按依赖关系反向停止所有子系统。

        顺序：
            停 TCP 线程
            -> 关闭录像
            -> 释放触摸
            -> 释放 VDEC/Display
            -> 关闭热点
        """

        if self.server is not None:
            self.server.stop()

        recorder_state = self.recorder.get_status()[0]

        if recorder_state != "IDLE":
            self.recorder.stop()

        if self.ui is not None:
            self.ui.deinit()

        self.decoder.stop()
        stop_access_point(self.access_point)
        gc.collect()


def run():
    """
    接收端开机入口。

    finally 保证用户从 IDE 停止或运行时异常时，仍会执行统一资源清理。
    """

    os.exitpoint(os.EXITPOINT_ENABLE)
    print("Two-K230 touch receiver.")
    application = ReceiverApplication()

    try:
        application.start()
        application.run_forever()

    except KeyboardInterrupt:
        print("User stopped receiver.")

    except BaseException as error:
        print("Receiver failed:", error)
        append_boot_log("receiver failed: %r" % error)

    finally:
        append_boot_log("receiver cleanup started")
        application.stop()
        append_boot_log("receiver cleanup finished")
        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
