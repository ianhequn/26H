"""
发送端到接收端的 TCP 连接模块。

本模块只建立和关闭 TCP socket，不读取摄像头，也不知道视频帧结构。
"""

import socket
import time

from transmitter.config import (
    RECEIVER_IP,
    TCP_PORT,
    TCP_RETRY_DELAY_S,
)
from transmitter.wifi_client import is_wifi_ready


def close_socket(sock):
    """
    安全关闭 socket。

    清理阶段的 close() 异常不应覆盖真正导致服务退出的原始异常。
    """

    if sock is None:
        return

    try:
        sock.close()
    except BaseException:
        pass


def connect_receiver_forever(wlan):
    """
    持续尝试连接接收端 TCP Server。

    参数：
        wlan:
            已连接热点的 WLAN 对象，用于判断底层 Wi-Fi 是否仍然正常。

    返回：
        socket:
            成功连接的 TCP socket。
        None:
            重试过程中 Wi-Fi 已断开，应回到 Wi-Fi 模块重新连接。
    """

    attempt = 0

    while is_wifi_ready(wlan):
        attempt += 1
        sock = None

        try:
            print("TCP connect attempt:", attempt)
            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            )

            # 尽量关闭 Nagle 算法，减少小帧等待合并造成的额外延迟。
            # 某些旧固件没有 TCP_NODELAY，异常时直接继续使用默认配置。
            try:
                sock.setsockopt(
                    socket.IPPROTO_TCP,
                    socket.TCP_NODELAY,
                    1,
                )
            except BaseException:
                pass

            sock.connect((RECEIVER_IP, TCP_PORT))
            print(
                "TCP connected to %s:%d"
                % (RECEIVER_IP, TCP_PORT)
            )
            return sock

        except KeyboardInterrupt:
            close_socket(sock)
            raise

        except BaseException as error:
            print("TCP connection failed:", error)
            close_socket(sock)
            time.sleep(TCP_RETRY_DELAY_S)

    return None
