"""
接收端 Wi-Fi AP 热点模块。

本模块只负责建立两块 K230 的局域网，不负责 TCP Server 或视频。

当前实现优先兼容 CanMV v0.4：构造 AP 对象后调用 config() 即可创建热点，
不依赖新版固件才支持的 active(True) 写法。
"""

import network
import time

from receiver.config import (
    AP_CHANNEL,
    AP_PASSWORD,
    AP_SSID,
    AP_STARTUP_WAIT_S,
    RECEIVER_IP,
    SUBNET_MASK,
)


def start_access_point():
    """
    创建发送端 K230 要连接的专用 Wi-Fi 热点。

    返回：
        network.WLAN:
            AP 模式网卡对象。

    固定地址：
        接收端始终为 192.168.8.1，发送端无需查询 DHCP 分配结果。
    """

    access_point = network.WLAN(network.AP_IF)

    # CanMV v0.4 官方 AP 例程通过 config() 直接创建热点。显式固定信道 6，
    # 让只扫描 2.4 GHz 常见信道的手机也能快速发现。
    try:
        access_point.config(
            ssid=AP_SSID,
            key=AP_PASSWORD,
            channel=AP_CHANNEL,
        )
    except TypeError:
        # 少数早期固件不接受 channel 关键字，退回官方最小写法。
        access_point.config(
            ssid=AP_SSID,
            key=AP_PASSWORD,
        )

    access_point.ifconfig(
        (
            RECEIVER_IP,
            SUBNET_MASK,
            RECEIVER_IP,
            RECEIVER_IP,
        )
    )

    # AP 创建是异步操作。立即启动 TCP Server 可能早于无线驱动准备完成。
    time.sleep(AP_STARTUP_WAIT_S)

    print("Receiver AP:", access_point.info())
    print("Receiver AP status:", access_point.status())
    print("Receiver AP IP:", access_point.ifconfig())
    return access_point


def stop_access_point(access_point):
    """
    安全停止 AP 模式。

    这里只记录清理异常，不覆盖导致主程序退出的原始错误。
    """

    if access_point is None:
        return

    try:
        access_point.stop()
    except BaseException as error:
        print("AP stop failed:", error)
