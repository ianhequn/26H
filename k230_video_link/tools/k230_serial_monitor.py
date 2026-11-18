"""
K230 USB 串口自动重连监视器。

普通 pyserial miniterm 在 K230 硬件复位时会因为 COM 设备短暂消失而退出。
本脚本捕获断线异常，持续等待同名 COM 口重新出现，然后继续读取开机日志。

运行示例：
    python tools/k230_serial_monitor.py COM12

依赖：
    python -m pip install pyserial
"""

import argparse
import sys
import time

import serial
from serial.tools import list_ports


# [常量] K230 USB 虚拟串口的默认通信参数。
DEFAULT_BAUDRATE = 115200

# [常量] COM 口不存在或暂时打不开时的重试周期，单位：秒。
RECONNECT_INTERVAL_S = 0.1

# [常量] 单次串口读取超时，单位：秒。
READ_TIMEOUT_S = 0.1


def port_exists(port_name: str) -> bool:
    """检查指定 COM 口当前是否已被 Windows 枚举出来。"""

    target = port_name.upper()
    return any(
        port.device.upper() == target
        for port in list_ports.comports()
    )


def open_port_forever(
    port_name: str,
    baudrate: int,
) -> serial.Serial:
    """
    持续等待并打开串口。

    K230 复位时 COM 口会先消失再重新出现，因此“暂时找不到设备”属于正常
    恢复过程，不应该让监视器退出。
    """

    waiting_message_printed = False

    while True:
        if not port_exists(port_name):
            if not waiting_message_printed:
                print(
                    "[monitor] waiting for %s..." % port_name,
                    flush=True,
                )
                waiting_message_printed = True

            time.sleep(RECONNECT_INTERVAL_S)
            continue

        try:
            connection = serial.Serial(
                port=port_name,
                baudrate=baudrate,
                timeout=READ_TIMEOUT_S,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            print(
                "[monitor] connected to %s at %d baud."
                % (port_name, baudrate),
                flush=True,
            )
            return connection

        except (OSError, serial.SerialException) as error:
            if not waiting_message_printed:
                print(
                    "[monitor] %s is not ready: %s"
                    % (port_name, error),
                    flush=True,
                )
                waiting_message_printed = True

            time.sleep(RECONNECT_INTERVAL_S)


def monitor_forever(port_name: str, baudrate: int) -> None:
    """读取串口；设备复位掉线后自动关闭旧句柄并重新连接。"""

    connection = None

    try:
        while True:
            if connection is None:
                connection = open_port_forever(
                    port_name,
                    baudrate,
                )

            try:
                data = connection.read(
                    connection.in_waiting or 1
                )

                if data:
                    # 直接写原始字节，避免一行日志被拆成多次读取时重复换行。
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

            except (OSError, serial.SerialException) as error:
                print(
                    "\n[monitor] device disconnected: %s" % error,
                    flush=True,
                )

                try:
                    connection.close()
                except BaseException:
                    pass

                connection = None

    except KeyboardInterrupt:
        print("\n[monitor] stopped by user.", flush=True)

    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass


def parse_arguments() -> argparse.Namespace:
    """读取命令行中的 COM 口和可选波特率。"""

    parser = argparse.ArgumentParser(
        description="Monitor a K230 USB serial port with auto-reconnect.",
    )
    parser.add_argument(
        "port",
        help="Windows COM port, for example COM12.",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=DEFAULT_BAUDRATE,
        help="Serial baud rate. Default: 115200.",
    )
    return parser.parse_args()


def main() -> None:
    """命令行入口。"""

    arguments = parse_arguments()
    monitor_forever(
        arguments.port,
        arguments.baudrate,
    )


if __name__ == "__main__":
    main()
