"""
把接收到的 H.264 直接封装成 MP4。

这里不是“截取屏幕像素再编码”，而是复用网络里已经编码好的 H.264：

    H.264 网络帧
    -> 一路送 VDEC 实时显示
    -> 另一路由本模块写入 MP4

因此不会再次压缩，也不会因为录像降低画质。
"""

import _thread
import os
import uctypes

from mpp.mp4_format import *
from mpp.mp4_format_struct import *

from common.frame_protocol import (
    FLAG_CODEC_HEADER,
    FLAG_KEY_FRAME,
    FLAG_P_FRAME,
)
from common.h264_annexb import extract_parameter_sets
from receiver.config import (
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
)
from receiver.storage import allocate_record_paths


# =========================================================
# [常量] 录像状态机
# =========================================================

# 没有打开录像文件，可以接受新的 REC 命令。
RECORDER_IDLE = "IDLE"

# 已创建 MP4，但尚未取得 SPS/PPS + IDR，暂时不能写第一帧。
RECORDER_WAIT_IDR = "WAIT_IDR"

# 已写入合法第一帧，正在持续保存 I/P 帧。
RECORDER_RECORDING = "RECORDING"

# 正在关闭 Track、写 MP4 文件尾索引并重命名。
RECORDER_FINALIZING = "FINALIZING"

# 创建、写入或关闭文件失败，临时文件不会发布成正式 MP4。
RECORDER_ERROR = "ERROR"


class Mp4Recorder:
    """
    不重新编码，直接把 H.264 访问单元写入 MP4。

    线程关系：
        start()/stop():
            由主线程的触摸 UI 调用。
        process_frame():
            由 TCP 接收线程调用。

    两个线程会同时访问状态和 MP4 Handle，因此所有状态转换都由同一把
    _thread lock 保护，避免 STOP 正在关闭文件时网络线程仍继续写帧。
    """

    def __init__(self):
        self.lock = _thread.allocate_lock()

        self.state = RECORDER_IDLE
        self.codec_header = b""

        self.mp4_handle = None
        self.video_track_handle = None
        self.temporary_path = None
        self.final_path = None

        self.start_timestamp_ms = 0
        self.last_timestamp_ms = -1
        self.written_frame_count = 0
        self.last_completed_file = None
        self.last_error = None

    def _create_muxer(self, temporary_path):
        """
        创建只包含 H.264 视频轨道的 MP4 Muxer。

        参数：
            temporary_path:
                以 .tmp.mp4 结尾的临时文件路径。

        返回：
            tuple:
                (mp4_handle, video_track_handle)

        异常：
            OSError:
                MP4 容器或视频 Track 创建失败。
        """

        mp4_config = k_mp4_config_s()
        mp4_config.config_type = K_MP4_CONFIG_MUXER
        mp4_config.muxer_config.file_name[:] = bytes(
            temporary_path,
            "utf-8",
        )
        mp4_config.muxer_config.fmp4_flag = 0

        mp4_handle_pointer = k_u64_ptr()
        create_result = kd_mp4_create(
            mp4_handle_pointer,
            mp4_config,
        )

        if create_result:
            raise OSError("kd_mp4_create failed.")

        track_info = k_mp4_track_info_s()
        track_info.track_type = K_MP4_STREAM_VIDEO

        # time_scale=1000 表示轨道以毫秒为基本时间单位。
        track_info.time_scale = 1000
        track_info.video_info.width = VIDEO_WIDTH
        track_info.video_info.height = VIDEO_HEIGHT
        track_info.video_info.codec_id = K_MP4_CODEC_ID_H264

        track_handle_pointer = k_u64_ptr()
        track_result = kd_mp4_create_track(
            mp4_handle_pointer.value,
            track_handle_pointer,
            track_info,
        )

        if track_result:
            kd_mp4_destroy(mp4_handle_pointer.value)
            raise OSError("kd_mp4_create_track failed.")

        return (
            mp4_handle_pointer.value,
            track_handle_pointer.value,
        )

    def _write_payload(self, payload, timestamp_ms):
        """
        向当前 MP4 写入一个完整编码视频样本。

        MP4 内的第一帧从 0 ms 开始，所以网络绝对时间戳要减去
        start_timestamp_ms。若收到重复或倒退时间戳，就至少递增 1 ms，
        防止生成非单调 DTS/PTS。
        """

        relative_timestamp_ms = (
            timestamp_ms - self.start_timestamp_ms
        )

        if relative_timestamp_ms < 0:
            relative_timestamp_ms = 0

        if (
            self.last_timestamp_ms >= 0
            and relative_timestamp_ms
            <= self.last_timestamp_ms
        ):
            relative_timestamp_ms = (
                self.last_timestamp_ms + 1
            )

        # kd_mp4_write_frame() 接收内存地址。bytearray 在函数调用期间提供
        # 稳定地址，并由局部变量 frame_buffer 保持生命周期。
        frame_buffer = bytearray(payload)
        frame_data = k_mp4_frame_data_s()
        frame_data.codec_id = K_MP4_CODEC_ID_H264
        frame_data.data = uctypes.addressof(frame_buffer)
        frame_data.data_length = len(frame_buffer)
        frame_data.time_stamp = relative_timestamp_ms

        write_result = kd_mp4_write_frame(
            self.mp4_handle,
            self.video_track_handle,
            frame_data,
        )

        if write_result:
            raise OSError("kd_mp4_write_frame failed.")

        self.last_timestamp_ms = relative_timestamp_ms
        self.written_frame_count += 1

    def start(self):
        """
        响应 REC 命令并进入“等待关键帧”状态。

        本函数会先创建临时 MP4，但不会立即写收到的任意帧。只有下一个带
        参数集的 IDR 到来时，process_frame() 才真正开始录像。

        返回：
            True:
                成功进入 RECORDER_WAIT_IDR。
            False:
                当前不在 IDLE，或创建 MP4 失败。
        """

        self.lock.acquire()

        try:
            if self.state != RECORDER_IDLE:
                return False

            (
                temporary_path,
                final_path,
            ) = allocate_record_paths()

            (
                self.mp4_handle,
                self.video_track_handle,
            ) = self._create_muxer(temporary_path)

            self.temporary_path = temporary_path
            self.final_path = final_path
            self.start_timestamp_ms = 0
            self.last_timestamp_ms = -1
            self.written_frame_count = 0
            self.last_error = None
            self.state = RECORDER_WAIT_IDR

            print("Recording armed:", final_path)
            return True

        except BaseException as error:
            self.last_error = str(error)
            self.state = RECORDER_ERROR
            print("Recorder start failed:", error)
            return False

        finally:
            self.lock.release()

    def process_frame(self, flags, timestamp_ms, payload):
        """
        观察每个网络帧，并在录像状态允许时写入 MP4。

        无论是否正在录像，本函数都会缓存最新 SPS/PPS。这样用户在直播中途
        点击 REC 后，也能在下一 IDR 前补上参数集。

        第一帧规则：
            SPS/PPS + IDR

        后续帧规则：
            只写 I 帧和 P 帧，绝不把单独的 SPS/PPS 当作普通 MP4 视频帧。
        """

        self.lock.acquire()

        try:
            if flags & FLAG_CODEC_HEADER:
                parameter_sets = extract_parameter_sets(payload)

                if parameter_sets:
                    self.codec_header = parameter_sets

            if self.state == RECORDER_WAIT_IDR:
                if not (flags & FLAG_KEY_FRAME):
                    return

                if flags & FLAG_CODEC_HEADER:
                    first_payload = payload
                elif self.codec_header:
                    first_payload = self.codec_header + payload
                else:
                    # 没有 SPS/PPS 的 MP4 无法描述解码参数，继续等待。
                    return

                self.start_timestamp_ms = timestamp_ms
                self._write_payload(
                    first_payload,
                    timestamp_ms,
                )
                self.state = RECORDER_RECORDING
                print("Recording started at IDR.")
                return

            if self.state != RECORDER_RECORDING:
                return

            if flags & (FLAG_KEY_FRAME | FLAG_P_FRAME):
                self._write_payload(payload, timestamp_ms)

        except BaseException as error:
            self.last_error = str(error)
            self.state = RECORDER_ERROR
            print("Recorder write failed:", error)

        finally:
            self.lock.release()

    def _finalize_unlocked(self, publish_file):
        """
        关闭 MP4 Track 和容器。调用者必须已经持有 self.lock。

        参数：
            publish_file:
                True：把临时文件重命名为正式 .mp4。
                False：保留 .tmp.mp4 后缀，供诊断，不进入播放列表。

        为什么必须调用 kd_mp4_destroy_tracks() 和 kd_mp4_destroy()：
            MP4 文件尾的索引通常在关闭阶段才写入。直接断电或只关闭 Python
            文件对象，都可能得到“有大小但播放器打不开”的文件。
        """

        if self.mp4_handle is None:
            return

        destroy_tracks_result = kd_mp4_destroy_tracks(
            self.mp4_handle
        )
        destroy_result = kd_mp4_destroy(self.mp4_handle)

        self.mp4_handle = None
        self.video_track_handle = None

        if destroy_tracks_result or destroy_result:
            raise OSError("Failed to finalize MP4.")

        if (
            publish_file
            and self.temporary_path is not None
            and self.final_path is not None
        ):
            os.rename(
                self.temporary_path,
                self.final_path,
            )
            self.last_completed_file = self.final_path

    def stop(self):
        """
        响应 STOP 命令，正确关闭当前录像并回到 IDLE。

        只有满足以下条件才发布正式 MP4：
            1. 之前没有进入 RECORDER_ERROR。
            2. 至少成功写入一帧。
        """

        self.lock.acquire()

        try:
            if self.state == RECORDER_IDLE:
                return False

            previous_state = self.state
            self.state = RECORDER_FINALIZING
            publish_file = (
                previous_state != RECORDER_ERROR
                and self.written_frame_count > 0
            )
            self._finalize_unlocked(publish_file)

            if publish_file:
                print(
                    "Recording saved:",
                    self.last_completed_file,
                )
            else:
                print(
                    "Recording not published; temporary file kept:",
                    self.temporary_path,
                )

            self.state = RECORDER_IDLE
            self.temporary_path = None
            self.final_path = None
            return True

        except BaseException as error:
            self.last_error = str(error)
            self.state = RECORDER_ERROR
            print("Recorder finalize failed:", error)
            return False

        finally:
            self.lock.release()

    def get_status(self):
        """
        返回供 UI 读取的不可变状态快照。

        返回：
            tuple:
                (state, written_frame_count, last_completed_file, last_error)
        """

        self.lock.acquire()

        try:
            return (
                self.state,
                self.written_frame_count,
                self.last_completed_file,
                self.last_error,
            )
        finally:
            self.lock.release()
