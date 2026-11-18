"""
H.264 Annex-B 码流的最小解析工具。

这里只识别接收端真正需要的 NAL 类型，不实现完整 H.264 解码：

- SPS：序列参数集，描述分辨率、Profile 等。
- PPS：图像参数集，描述图像解码参数。
- IDR：可以独立开始解码的关键帧。

真正的画面解码仍由 K230 VDEC 硬件完成。
"""


# [常量] 接收端需要识别的 H.264 NAL 单元类型。
NAL_TYPE_IDR = 5
NAL_TYPE_SPS = 7
NAL_TYPE_PPS = 8


def _find_start_codes(data):
    """
    查找 Annex-B 中所有 3 字节或 4 字节起始码。

    H.264 Annex-B 使用下面两种标记分隔 NAL 单元：

        00 00 01
        00 00 00 01

    返回：
        list:
            每项为 (起始偏移, 起始码长度)。
    """

    result = []
    index = 0
    data_length = len(data)

    while index + 3 <= data_length:
        if (
            index + 4 <= data_length
            and data[index:index + 4] == b"\x00\x00\x00\x01"
        ):
            result.append((index, 4))
            index += 4
            continue

        if data[index:index + 3] == b"\x00\x00\x01":
            result.append((index, 3))
            index += 3
            continue

        index += 1

    return result


def iter_nal_units(data):
    """
    逐个产生一个访问单元中的 NAL 单元。

    参数：
        data:
            一段 H.264 Annex-B 数据。

    产生：
        tuple:
            (nal_type, complete_nal_bytes)

    complete_nal_bytes 保留原起始码，因此多个结果可以直接拼接后交给
    VDEC 或 MP4 Muxer，不需要重新生成起始码。
    """

    start_codes = _find_start_codes(data)

    for item_index in range(len(start_codes)):
        start_offset, start_code_length = start_codes[item_index]

        if item_index + 1 < len(start_codes):
            end_offset = start_codes[item_index + 1][0]
        else:
            end_offset = len(data)

        nal_header_offset = start_offset + start_code_length

        if nal_header_offset >= end_offset:
            continue

        nal_type = data[nal_header_offset] & 0x1F
        yield nal_type, data[start_offset:end_offset]


def extract_parameter_sets(data):
    """
    只提取访问单元中的 SPS 和 PPS。

    返回：
        bytes:
            SPS 与 PPS 按原顺序拼接的 Annex-B 数据；找不到时返回 b""。

    用途：
        接收端会缓存该结果。用户中途点击录像时，可以把缓存参数集放到
        第一帧 IDR 前面，保证新 MP4 能够从头解码。
    """

    parameter_sets = []

    for nal_type, nal_data in iter_nal_units(data):
        if nal_type in (NAL_TYPE_SPS, NAL_TYPE_PPS):
            parameter_sets.append(nal_data)

    return b"".join(parameter_sets)


def contains_idr(data):
    """判断访问单元中是否包含 IDR 关键帧。"""

    for nal_type, _nal_data in iter_nal_units(data):
        if nal_type == NAL_TYPE_IDR:
            return True

    return False
