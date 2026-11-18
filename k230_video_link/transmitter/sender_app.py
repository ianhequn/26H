"""
发送端顶层监督循环。

它只负责编排各模块：

    连接 Wi-Fi
    -> 连接接收端 TCP
    -> 启动编码器
    -> 持续发送
    -> 断线后释放编码器和 socket
    -> 重新连接
"""

import gc
import os
import time

from common.frame_protocol import (
    FLAG_KEY_FRAME,
    FLAG_P_FRAME,
    send_frame,
)
from transmitter.config import (
    BOOT_DELAY_S,
    LOG_EVERY_N_FRAMES,
    TCP_RETRY_DELAY_S,
)
from transmitter.encoder_source import H264EncoderSource
from transmitter.tcp_client import (
    close_socket,
    connect_receiver_forever,
)
from transmitter.wifi_client import (
    connect_wifi_forever,
    is_wifi_ready,
)


def stream_until_disconnected(sock, source):
    """
    持续读取编码访问单元并发送，直到 TCP 出现异常。

    参数：
        sock:
            已连接接收端的 TCP socket。
        source:
            已启动的 H264EncoderSource。

    正常情况下本函数不会主动返回；断网、接收端重启或用户停止会通过异常
    交给外层监督循环处理。
    """

    protocol_frame_id = 0
    video_frame_count = 0
    start_time = time.time()

    while True:
        os.exitpoint()
        access_unit = source.read_access_unit()

        if access_unit is None:
            continue

        flags, timestamp_ms, payload = access_unit

        send_frame(
            sock,
            flags,
            protocol_frame_id,
            timestamp_ms,
            payload,
        )
        protocol_frame_id = (
            protocol_frame_id + 1
        ) & 0xFFFFFFFF

        if flags & (FLAG_KEY_FRAME | FLAG_P_FRAME):
            video_frame_count += 1

        if (
            video_frame_count > 0
            and video_frame_count % LOG_EVERY_N_FRAMES == 0
        ):
            elapsed_s = max(
                time.time() - start_time,
                0.001,
            )
            print(
                "Sent %d frames, average FPS: %.1f"
                % (
                    video_frame_count,
                    video_frame_count / elapsed_s,
                )
            )


def run():
    """
    发送端开机主流程，并在网络故障后自动恢复。

    为什么 TCP 连接成功后才启动编码器：
        每次新连接都会让编码器从头输出 SPS/PPS 和 IDR。接收端无需继承
        上一次连接的解码状态，重连后更容易重新出现画面。
    """

    os.exitpoint(os.EXITPOINT_ENABLE)
    print("Two-K230 video transmitter.")
    time.sleep(BOOT_DELAY_S)

    wlan = None

    try:
        while True:
            if wlan is None or not is_wifi_ready(wlan):
                wlan = connect_wifi_forever()

            sock = connect_receiver_forever(wlan)

            if sock is None:
                continue

            source = H264EncoderSource()

            try:
                # TCP 成功后才启动 VENC，保证每条新连接都先收到新的
                # SPS/PPS 和 IDR 关键帧。
                source.start()
                stream_until_disconnected(sock, source)

            except KeyboardInterrupt:
                raise

            except BaseException as error:
                print("Streaming connection ended:", error)

            finally:
                source.stop()
                close_socket(sock)
                gc.collect()

            print(
                "Reconnect after %d seconds."
                % TCP_RETRY_DELAY_S
            )
            time.sleep(TCP_RETRY_DELAY_S)

    except KeyboardInterrupt:
        print("User stopped transmitter.")

    finally:
        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
