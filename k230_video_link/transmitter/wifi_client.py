"""
发送端 Wi-Fi STA 连接模块。

职责边界：
    只负责创建网卡、配置静态 IP、连接接收端热点和判断连接状态。
    不负责 TCP、不负责摄像头，也不负责 H.264。
"""

import time
import network

from transmitter.config import (
    RECEIVER_IP,
    SUBNET_MASK,
    TRANSMITTER_IP,
    WIFI_CONNECT_TIMEOUT_S,
    WIFI_PASSWORD,
    WIFI_RETRY_DELAY_S,
    WIFI_SSID,
)


# =========================================================
# [函数] 查询网络状态
# =========================================================

def read_ip_address(wlan):
    """
    安全读取网卡当前 IPv4 地址。

    参数：
        wlan:
            network.WLAN 对象。

    返回：
        str:
            已获得的 IPv4 地址。
        None:
            网卡仍在初始化，ifconfig() 暂时没有有效结果。

    为什么不能直接写 wlan.ifconfig()[0]：
        旧 CanMV 固件初始化期间可能返回 None，直接取下标会触发 TypeError。
    """

    network_config = wlan.ifconfig()

    if network_config is None or len(network_config) == 0:
        return None

    return network_config[0]


def is_wifi_ready(wlan):
    """
    判断 Wi-Fi 是否真的可以用于图传。

    必须同时满足：
        1. STA 已关联接收端热点。
        2. 当前 IP 等于配置的发送端静态 IP。
    """

    try:
        return (
            wlan.isconnected()
            and read_ip_address(wlan) == TRANSMITTER_IP
        )
    except BaseException:
        return False


# =========================================================
# [函数] STA 网卡生命周期
# =========================================================

def create_station():
    """
    创建 STA 模式网卡并写入静态 IPv4 配置。

    STA 表示发送 K230 作为客户端，主动连接接收 K230 建立的热点。

    返回：
        network.WLAN:
            已激活但不一定已经连接的网卡对象。
    """

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.ifconfig(
        (
            TRANSMITTER_IP,
            SUBNET_MASK,
            RECEIVER_IP,
            RECEIVER_IP,
        )
    )
    return wlan


def connect_wifi_forever():
    """
    持续重试，直到成功连接接收端 K230 热点。

    返回：
        network.WLAN:
            已连接且静态 IP 正确的网卡。

    注意：
        单次超时不会抛出 RuntimeError 结束开机自启程序。
        接收端尚未开机时，发送端会一直留在这里等待。
    """

    wlan = create_station()
    attempt = 0

    while True:
        attempt += 1

        if is_wifi_ready(wlan):
            return wlan

        print("Wi-Fi connect attempt:", attempt)

        try:
            try:
                wlan.disconnect()
            except BaseException:
                pass

            wlan.connect(WIFI_SSID, WIFI_PASSWORD)
            start_time = time.time()

            while (
                time.time() - start_time
                < WIFI_CONNECT_TIMEOUT_S
            ):
                if is_wifi_ready(wlan):
                    print("Wi-Fi connected:", wlan.ifconfig())
                    return wlan

                time.sleep(0.25)

            print("Wi-Fi connection timed out.")

        except KeyboardInterrupt:
            raise

        except BaseException as error:
            print("Wi-Fi connection failed:", error)
            wlan = create_station()

        time.sleep(WIFI_RETRY_DELAY_S)
