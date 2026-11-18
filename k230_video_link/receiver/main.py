"""
接收端 K230 的开机入口。

部署方式：
    1. 把本文件复制成接收板的 /sdcard/main.py。
    2. 同时上传 /sdcard/common 和 /sdcard/receiver。

本文件只负责进入 receiver_app.run()，实际模块装配和生命周期在
receiver_app.py 中。
"""

import time

from receiver.boot_log import append_boot_log, reset_boot_log
from receiver.config import (
    RECEIVER_BOOT_DELAY_S,
    RECEIVER_BUILD_ID,
)


# main.py 是固件约定的开机脚本，因此这里不依赖 __name__ 判断。这样即使某版
# CanMV 以导入模块而不是普通脚本的方式执行 main.py，也一定会进入接收程序。
reset_boot_log()
append_boot_log("build: %s" % RECEIVER_BUILD_ID)

try:
    append_boot_log(
        "cold boot delay: %d s"
        % RECEIVER_BOOT_DELAY_S
    )
    time.sleep(RECEIVER_BOOT_DELAY_S)
    append_boot_log("cold boot delay finished")

    append_boot_log("importing receiver_app")
    from receiver.receiver_app import run

    append_boot_log("receiver_app imported")
    run()

except BaseException as error:
    append_boot_log("main.py fatal error: %r" % error)
    raise
