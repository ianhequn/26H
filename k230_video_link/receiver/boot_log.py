"""
接收端开机诊断日志。

K230 硬件复位时 USB 虚拟串口会重新枚举，普通电脑串口工具可能错过最早的
异常。本模块把少量启动阶段写入 SD 卡，专门用于定位“开机黑屏”。
"""


# [常量] 每次接收端启动时写入的诊断文件。
BOOT_LOG_PATH = "/sdcard/receiver_boot.log"


def reset_boot_log():
    """清空上一轮日志，并记录本轮程序已经进入 main.py。"""

    try:
        with open(BOOT_LOG_PATH, "w") as log_file:
            log_file.write("main.py entered\n")
            log_file.flush()
    except BaseException as error:
        # SD 卡不可写时只能退回串口，不能因为诊断功能阻止主程序启动。
        print("Boot log reset failed:", error)


def append_boot_log(message):
    """向诊断文件追加一个启动阶段或异常信息。"""

    try:
        with open(BOOT_LOG_PATH, "a") as log_file:
            log_file.write(str(message))
            log_file.write("\n")
            log_file.flush()
    except BaseException as error:
        print("Boot log write failed:", error)
