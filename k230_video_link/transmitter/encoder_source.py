"""
发送端 Camera -> H.264 VENC 媒体链路。

这是发送端唯一直接接触 Sensor、VENC 和 MediaManager 的模块。
网络层只会从本类取得 (帧类型, 时间戳, H.264 bytes)，从而与硬件解耦。
"""

import uctypes

from media.media import MediaManager
from media.sensor import Sensor
from media.vencoder import *

from common.frame_protocol import (
    FLAG_CODEC_HEADER,
    FLAG_KEY_FRAME,
    FLAG_P_FRAME,
)
from transmitter.config import (
    VENC_CHANNEL,
    VENC_OUTPUT_BUFFER_COUNT,
    VIDEO_BITRATE_KBPS,
    VIDEO_FPS,
    VIDEO_GOP_FRAMES,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
)


class H264EncoderSource:
    """
    管理完整的 Camera -> VENC 媒体管线。

    生命周期：
        start()
        -> 多次 read_access_unit()
        -> stop()

    网络代码只能读取编码结果，不能绕过本类操作 Sensor、VENC 或
    MediaManager，防止多个模块争抢同一硬件资源。
    """

    def __init__(self):
        self.sensor = None
        self.encoder = None
        self.media_link = None
        self.media_initialized = False
        self.started = False
        self.video_frame_index = 0

    def start(self):
        """
        初始化并启动摄像头采集和 H.264 硬件编码。

        初始化顺序：
            Sensor 配置
            -> VENC 输出缓冲区配置
            -> 建立 Sensor 到 VENC 的媒体绑定
            -> MediaManager.init()
            -> 创建并启动 VENC
            -> 启动 Sensor
        """

        if self.started:
            return

        aligned_width = ALIGN_UP(VIDEO_WIDTH, 16)

        self.sensor = Sensor()
        self.sensor.reset()
        self.sensor.set_framesize(
            width=aligned_width,
            height=VIDEO_HEIGHT,
            alignment=12,
        )
        self.sensor.set_pixformat(Sensor.YUV420SP)

        self.encoder = Encoder()
        self.encoder.SetOutBufs(
            VENC_CHANNEL,
            VENC_OUTPUT_BUFFER_COUNT,
            aligned_width,
            VIDEO_HEIGHT,
        )

        self.media_link = MediaManager.link(
            self.sensor.bind_info()["src"],
            (
                VIDEO_ENCODE_MOD_ID,
                VENC_DEV_ID,
                VENC_CHANNEL,
            ),
        )

        MediaManager.init()
        self.media_initialized = True

        encoder_attributes = ChnAttrStr(
            self.encoder.PAYLOAD_TYPE_H264,
            self.encoder.H264_PROFILE_BASELINE,
            aligned_width,
            VIDEO_HEIGHT,
            bit_rate=VIDEO_BITRATE_KBPS,
            gopLen=VIDEO_GOP_FRAMES,
            src_frame_rate=VIDEO_FPS,
            dst_frame_rate=VIDEO_FPS,
        )

        self.encoder.Create(
            VENC_CHANNEL,
            encoder_attributes,
        )
        self.encoder.Start(VENC_CHANNEL)
        self.sensor.run()

        self.video_frame_index = 0
        self.started = True

        print(
            "Encoder started: %dx%d %d FPS %d kbit/s"
            % (
                VIDEO_WIDTH,
                VIDEO_HEIGHT,
                VIDEO_FPS,
                VIDEO_BITRATE_KBPS,
            )
        )

    def _read_payload(self, stream_data):
        """
        复制并合并一次 GetStream() 返回的全部 pack。

        一次 GetStream() 在不同固件中可能返回一个或多个 pack。协议层发送的
        单位必须是完整访问单元，不能把 SPS、PPS 和图像数据随意拆开。

        返回：
            bytes:
                可脱离 VENC 缓冲区继续使用的 H.264 数据副本。
        """

        pack_list = []

        for pack_index in range(stream_data.pack_cnt):
            pack_list.append(
                bytes(
                    uctypes.bytearray_at(
                        stream_data.data[pack_index],
                        stream_data.data_size[pack_index],
                    )
                )
            )

        if len(pack_list) == 1:
            return pack_list[0]

        return b"".join(pack_list)

    def _read_flags(self, stream_data):
        """
        把 K230 VENC 帧类型转换成公共协议 FLAG_* 标志。

        使用公共标志后，接收端不需要导入发送端 VENC 常量；未来树莓派也只需
        生成相同的 FLAG_*，不必模拟 K230 API。
        """

        flags = 0

        for pack_index in range(stream_data.pack_cnt):
            stream_type = stream_data.stream_type[pack_index]

            if stream_type == self.encoder.STREAM_TYPE_HEADER:
                flags |= FLAG_CODEC_HEADER
            elif stream_type == self.encoder.STREAM_TYPE_I:
                flags |= FLAG_KEY_FRAME
            elif stream_type == self.encoder.STREAM_TYPE_P:
                flags |= FLAG_P_FRAME

        return flags

    def read_access_unit(self):
        """
        阻塞等待并读取一个编码访问单元。

        返回：
            tuple:
                (flags, timestamp_ms, payload)
            None:
                本次 VENC 暂时没有取得有效数据。

        资源规则：
            GetStream() 成功后，无论复制或解析是否发生异常，finally 都必须
            调用 ReleaseStream()，否则 VENC 缓冲区最终会耗尽。
        """

        if not self.started:
            raise RuntimeError("Encoder source is not started.")

        stream_data = StreamData()
        get_result = self.encoder.GetStream(
            VENC_CHANNEL,
            stream_data,
        )

        if get_result != 0:
            return None

        try:
            if stream_data.pack_cnt <= 0:
                return None

            flags = self._read_flags(stream_data)
            payload = self._read_payload(stream_data)

            # 使用帧序号生成毫秒时间戳，避免不同固件中 VENC PTS 单位不一致。
            # 20 FPS 时依次为 0、50、100、150... ms。
            timestamp_ms = (
                self.video_frame_index * 1000
            ) // VIDEO_FPS

            if flags & (FLAG_KEY_FRAME | FLAG_P_FRAME):
                self.video_frame_index += 1

            return flags, timestamp_ms, payload

        finally:
            self.encoder.ReleaseStream(
                VENC_CHANNEL,
                stream_data,
            )

    def stop(self):
        """
        按初始化的反方向停止并释放媒体资源。

        每一步都独立捕获异常，保证前一个资源释放失败时，后续资源仍有机会
        释放。断线重连会反复调用 start()/stop()，所以这里不能留下占用。
        """

        if self.sensor is not None:
            try:
                self.sensor.stop()
            except BaseException as error:
                print("Sensor stop failed:", error)

        if self.media_link is not None:
            try:
                del self.media_link
            except BaseException:
                pass
            self.media_link = None

        if self.encoder is not None:
            try:
                self.encoder.Stop(VENC_CHANNEL)
            except BaseException as error:
                print("Encoder stop failed:", error)

            try:
                self.encoder.Destroy(VENC_CHANNEL)
            except BaseException as error:
                print("Encoder destroy failed:", error)

        if self.media_initialized:
            try:
                MediaManager.deinit()
            except BaseException as error:
                print("MediaManager deinit failed:", error)

        self.sensor = None
        self.encoder = None
        self.media_initialized = False
        self.started = False
