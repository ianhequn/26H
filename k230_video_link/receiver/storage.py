"""
接收端 SD 卡文件管理。

本模块只处理目录、文件名和剩余空间，不导入 MP4、VDEC 或 Display。
"""

import os

from receiver.config import (
    RECORD_DIRECTORY,
    RECORD_FILE_PREFIX,
)


def ensure_record_directory():
    """
    确保录像目录存在。

    os.mkdir() 在目录已存在时会抛 OSError，这种情况属于正常状态。
    """

    try:
        os.mkdir(RECORD_DIRECTORY)
    except OSError:
        # 第二次及以后启动时目录本来就存在，直接继续。
        pass


def list_recordings():
    """
    返回按文件名排序的正式 MP4 路径。

    只列出 REC_*.mp4，带 .tmp.mp4 的未完成文件不会进入播放列表。
    """

    ensure_record_directory()
    result = []

    for file_name in os.listdir(RECORD_DIRECTORY):
        if (
            file_name.startswith(RECORD_FILE_PREFIX + "_")
            and file_name.endswith(".mp4")
            and not file_name.endswith(".tmp.mp4")
        ):
            result.append(
                RECORD_DIRECTORY + "/" + file_name
            )

    result.sort()
    return result


def allocate_record_paths():
    """
    分配不会与旧录像冲突的新文件名。

    返回：
        tuple:
            (临时路径, 正式路径)

    示例：
        /sdcard/records/REC_001.tmp.mp4
        /sdcard/records/REC_001.mp4

    为什么先写临时文件：
        只有 MP4 正确关闭后才改成正式扩展名，避免文件列表把损坏或未完成文件
        当作可播放录像。
    """

    ensure_record_directory()
    existing_names = set(os.listdir(RECORD_DIRECTORY))
    sequence = 1

    while True:
        base_name = "%s_%03d" % (
            RECORD_FILE_PREFIX,
            sequence,
        )
        final_name = base_name + ".mp4"
        temporary_name = base_name + ".tmp.mp4"

        if (
            final_name not in existing_names
            and temporary_name not in existing_names
        ):
            return (
                RECORD_DIRECTORY + "/" + temporary_name,
                RECORD_DIRECTORY + "/" + final_name,
            )

        sequence += 1


def get_free_space_mb():
    """
    读取 SD 卡近似剩余空间。

    返回：
        int:
            剩余空间，单位：MiB。
        -1:
            当前固件不支持 statvfs() 或 SD 卡不可用。
    """

    try:
        stats = os.statvfs(RECORD_DIRECTORY)
        free_bytes = stats[0] * stats[3]
        return free_bytes // (1024 * 1024)
    except BaseException:
        return -1
