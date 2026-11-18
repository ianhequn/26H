"""
生成发送端和接收端两个独立的 /sdcard 上传目录。

可以从任意工作目录执行：
    python tools/build_deploy.py

本脚本只做部署打包，不修改源代码。
"""

import os
import shutil


PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
DIST_ROOT = os.path.join(PROJECT_ROOT, "dist")


def copy_python_package(package_name, destination_root):
    """
    复制一个 Python 包，并排除电脑生成的 __pycache__ 和 .pyc。
    """

    source_directory = os.path.join(
        PROJECT_ROOT,
        package_name,
    )
    destination_directory = os.path.join(
        destination_root,
        package_name,
    )

    os.makedirs(destination_directory, exist_ok=True)

    for file_name in os.listdir(source_directory):
        source_path = os.path.join(
            source_directory,
            file_name,
        )

        if (
            not os.path.isfile(source_path)
            or not file_name.endswith(".py")
        ):
            continue

        shutil.copy2(
            source_path,
            os.path.join(destination_directory, file_name),
        )


def build_board_bundle(board_package):
    """
    生成单块板的部署包。

    每块板只获得：
        1. 公共 common 包。
        2. 自己的 transmitter 或 receiver 包。
        3. 放在 /sdcard 根目录的 main.py。
    """

    destination_root = os.path.join(
        DIST_ROOT,
        board_package + "_sdcard",
    )

    # dist 是可重复生成的输出目录。每次先删除旧目录，避免残留文件和电脑
    # 字节码被误上传到 MicroPython。
    if os.path.isdir(destination_root):
        shutil.rmtree(destination_root)

    os.makedirs(destination_root, exist_ok=True)

    copy_python_package("common", destination_root)
    copy_python_package(board_package, destination_root)

    shutil.copy2(
        os.path.join(
            PROJECT_ROOT,
            board_package,
            "main.py",
        ),
        os.path.join(destination_root, "main.py"),
    )

    return destination_root


def main():
    """同时生成发送端和接收端上传目录。"""

    transmitter_output = build_board_bundle("transmitter")
    receiver_output = build_board_bundle("receiver")

    print("Built transmitter bundle:")
    print(transmitter_output)
    print("Built receiver bundle:")
    print(receiver_output)


if __name__ == "__main__":
    main()
