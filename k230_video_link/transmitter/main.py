"""
发送端 K230 的开机入口。

部署方式：
    1. 把本文件复制成发送板的 /sdcard/main.py。
    2. 同时上传 /sdcard/common 和 /sdcard/transmitter。

本文件故意只保留入口调用。所有业务逻辑在 sender_app.py，方便单独理解、
测试和替换，避免 main.py 重新变成一个几百行的大文件。
"""

from transmitter.sender_app import run


if __name__ == "__main__":
    run()
