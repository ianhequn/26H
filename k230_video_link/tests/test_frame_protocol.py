"""在电脑上运行的硬件无关协议单元测试。"""

import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
sys.path.insert(0, PROJECT_ROOT)

from common.frame_protocol import (
    FLAG_CODEC_HEADER,
    FLAG_KEY_FRAME,
    HEADER_SIZE,
    build_header,
    parse_header,
    receive_frame,
    send_frame,
)
from common.h264_annexb import (
    contains_idr,
    extract_parameter_sets,
)


class FragmentedSocket:
    """
    故意把 send()/recv() 拆成小块的内存模拟 socket。

    它用于验证协议代码从不假设“一次 TCP 调用就等于一整帧”。
    """

    def __init__(self, max_chunk_size=3):
        self.buffer = bytearray()
        self.read_offset = 0
        self.max_chunk_size = max_chunk_size

    def send(self, data):
        chunk_size = min(len(data), self.max_chunk_size)
        self.buffer.extend(bytes(data[:chunk_size]))
        return chunk_size

    def recv(self, requested_size):
        available = len(self.buffer) - self.read_offset

        if available <= 0:
            return b""

        chunk_size = min(
            requested_size,
            available,
            self.max_chunk_size,
        )
        start = self.read_offset
        end = start + chunk_size
        self.read_offset = end
        return bytes(self.buffer[start:end])


class FrameProtocolTests(unittest.TestCase):
    """验证帧头、TCP 分片收发和 H.264 参数集解析。"""

    def test_header_round_trip(self):
        """帧头编码后应能无损解析回原字段。"""

        flags = FLAG_CODEC_HEADER | FLAG_KEY_FRAME
        header = build_header(flags, 123, 4567, 890)

        self.assertEqual(len(header), HEADER_SIZE)
        self.assertEqual(
            parse_header(header),
            (flags, 123, 4567, 890),
        )

    def test_fragmented_frame_round_trip(self):
        """即使每次只收发 2 字节，也必须恢复完整 H.264 帧。"""

        sock = FragmentedSocket(max_chunk_size=2)
        payload = b"\x00\x00\x00\x01\x67test-h264-data"
        flags = FLAG_CODEC_HEADER | FLAG_KEY_FRAME

        send_frame(sock, flags, 42, 1500, payload)

        self.assertEqual(
            receive_frame(sock),
            (flags, 42, 1500, payload),
        )

    def test_invalid_magic_is_rejected(self):
        """魔数损坏时必须拒绝该帧，不能继续按错误长度分配内存。"""

        header = bytearray(build_header(0, 1, 2, 3))
        header[0:4] = b"BAD!"

        with self.assertRaises(ValueError):
            parse_header(bytes(header))

    def test_h264_parameter_set_extraction(self):
        """应从混合访问单元中提取 SPS/PPS 并识别 IDR。"""

        sps = b"\x00\x00\x00\x01\x67\x64\x00\x1f"
        pps = b"\x00\x00\x01\x68\xee\x3c\x80"
        idr = b"\x00\x00\x00\x01\x65\x88\x84"
        access_unit = sps + pps + idr

        self.assertEqual(
            extract_parameter_sets(access_unit),
            sps + pps,
        )
        self.assertTrue(contains_idr(access_unit))


if __name__ == "__main__":
    unittest.main()
