"""
接收端 Wi-Fi 热点最小自检。

当完整图传程序可以显示 UI、但手机找不到热点时，单独运行本文件。它不初始化
Display、VDEC、触摸或 TCP，因此可以判断问题是在 Wi-Fi AP 层，还是其他
模块共同运行造成的。
"""

import time

from receiver.wifi_ap import start_access_point, stop_access_point


def run():
    """启动热点，并每 2 秒打印一次 AP 信息和连接状态。"""

    access_point = None

    try:
        print("Starting receiver AP smoke test...")
        access_point = start_access_point()
        print("Search Wi-Fi SSID: K230_VIDEO")
        print("Wi-Fi password: <configure locally>")

        while True:
            # AP 模式下 status() 用于观察热点状态；手机连接后，info() 中
            # 通常还会出现关联设备信息。不同固件的显示格式可能不同。
            print("AP info:", access_point.info())
            print("AP status:", access_point.status())
            time.sleep(2)

    except KeyboardInterrupt:
        print("AP smoke test stopped by user.")

    finally:
        stop_access_point(access_point)


if __name__ == "__main__":
    run()
