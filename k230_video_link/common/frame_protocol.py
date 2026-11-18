"""
K230 图传的 TCP 视频帧协议。

【本模块在整条链路中的位置】

    H.264 编码数据
    -> 本模块加帧头
    -> TCP 发送
    -> 本模块按帧头收取完整数据
    -> VDEC 解码或 MP4 保存

本模块不导入任何 K230 硬件 API，所以可以同时用于：

1. K230 发送端。
2. K230 接收端。
3. 以后替换的树莓派发送端。
4. 电脑上的单元测试和诊断脚本。

【为什么需要自定义帧头】

TCP 是连续的“字节流”，不保留消息边界。发送端一次 send() 发出的一帧，
接收端可能需要多次 recv() 才能收全；也可能一次 recv() 收到多帧的一部分。
因此每个 H.264 访问单元前都要增加固定长度的帧头，明确告诉接收端负载长度。
"""

try:
    import ustruct as struct
except ImportError:
    import struct


# =========================================================
# [常量] 协议身份和帧头格式
# =========================================================

# 每个协议帧开头的 4 字节魔数，用来判断是否发生数据错位。
FRAME_MAGIC = b"K230"

# 协议版本。只有二进制帧头结构发生不兼容变化时才递增。
PROTOCOL_VERSION = 1

# 帧头采用网络字节序，也就是大端序。
# 字段依次为：
# magic、version、flags、reserved、frame_id、timestamp_ms、payload_length。
HEADER_FORMAT = "!4sBBHIQI"

# 固定帧头大小，当前格式计算结果为 24 字节。
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

# 单帧 H.264 数据允许的最大长度，单位：字节。
# 先限制长度再分配内存，避免损坏帧头让接收端申请异常大的 bytearray。
MAX_PAYLOAD_SIZE = 2 * 1024 * 1024


# =========================================================
# [常量] 视频帧类型标志
# =========================================================

# 负载中包含 H.264 SPS/PPS 编解码参数。
FLAG_CODEC_HEADER = 0x01

# 负载中包含 H.264 IDR 关键帧。
FLAG_KEY_FRAME = 0x02

# 负载中包含普通 P 帧。
FLAG_P_FRAME = 0x04


# =========================================================
# [函数] 帧头编码和校验
# =========================================================

def build_header(flags, frame_id, timestamp_ms, payload_length):
    """
    把一帧视频的元信息编码成固定长度帧头。

    参数：
        flags:
            FLAG_* 常量按位或的结果，表示参数集、I 帧或 P 帧。
        frame_id:
            32 位无符号递增编号，用来观察是否丢帧或乱序。
        timestamp_ms:
            64 位无符号显示时间戳，单位：毫秒。
        payload_length:
            紧随帧头之后的 H.264 数据长度，单位：字节。

    返回：
        bytes:
            长度严格等于 HEADER_SIZE 的二进制帧头。

    异常：
        ValueError:
            负载长度小于 0 或超过 MAX_PAYLOAD_SIZE。
    """

    if payload_length < 0 or payload_length > MAX_PAYLOAD_SIZE:
        raise ValueError("Invalid payload length: %d" % payload_length)

    return struct.pack(
        HEADER_FORMAT,
        FRAME_MAGIC,
        PROTOCOL_VERSION,
        flags,
        0,
        frame_id & 0xFFFFFFFF,
        timestamp_ms,
        payload_length,
    )


def parse_header(header_data):
    """
    解析并校验一个固定长度帧头。

    参数：
        header_data:
            从 TCP 中完整读取的 HEADER_SIZE 字节。

    返回：
        tuple:
            (flags, frame_id, timestamp_ms, payload_length)

    异常：
        ValueError:
            帧头长度、魔数、协议版本或负载长度不合法。
    """

    if len(header_data) != HEADER_SIZE:
        raise ValueError(
            "Header size must be %d bytes, got %d."
            % (HEADER_SIZE, len(header_data))
        )

    (
        magic,
        version,
        flags,
        _reserved,
        frame_id,
        timestamp_ms,
        payload_length,
    ) = struct.unpack(HEADER_FORMAT, header_data)

    if magic != FRAME_MAGIC:
        raise ValueError("Frame magic mismatch.")

    if version != PROTOCOL_VERSION:
        raise ValueError(
            "Unsupported protocol version: %d" % version
        )

    if payload_length > MAX_PAYLOAD_SIZE:
        raise ValueError(
            "Payload is too large: %d" % payload_length
        )

    return (
        flags,
        frame_id,
        timestamp_ms,
        payload_length,
    )


# =========================================================
# [函数] TCP 完整字节收发
# =========================================================

def send_all(sock, data):
    """
    循环调用 socket.send()，直到 data 中的字节全部发出。

    为什么不能只调用一次 send()：
        send() 的返回值表示“本次实际发送了多少字节”，它可能小于数据总长度。
        TCP 可靠只代表最终按顺序到达，不代表一次函数调用就能发送完整。

    异常：
        OSError:
            发送完成前连接已经关闭。
    """

    view = memoryview(data)
    sent_size = 0

    while sent_size < len(view):
        current_size = sock.send(view[sent_size:])

        if current_size is None:
            # 少数 MicroPython 端口的 send() 没有返回长度，
            # 而是表现得像 sendall()；此时视为已经完成。
            return

        if current_size <= 0:
            raise OSError("TCP connection closed while sending.")

        sent_size += current_size


def recv_exact(sock, expected_size):
    """
    从 TCP 字节流中循环读取，直到正好得到 expected_size 字节。

    参数：
        sock:
            已连接的 TCP socket。
        expected_size:
            本次必须收满的字节数。

    返回：
        bytes:
            长度严格等于 expected_size。

    异常：
        OSError:
            数据尚未收满时，对端已经关闭连接。
    """

    data = bytearray()

    while len(data) < expected_size:
        chunk = sock.recv(expected_size - len(data))

        if not chunk:
            raise OSError("TCP connection closed while receiving.")

        data.extend(chunk)

    return bytes(data)


def send_frame(
    sock,
    flags,
    frame_id,
    timestamp_ms,
    payload,
):
    """
    发送一个完整 H.264 访问单元。

    操作顺序：
        计算 payload 长度
        -> 生成 24 字节帧头
        -> 发完帧头
        -> 发完 H.264 负载
    """

    header = build_header(
        flags,
        frame_id,
        timestamp_ms,
        len(payload),
    )

    send_all(sock, header)
    send_all(sock, payload)


def receive_frame(sock):
    """
    从 TCP 中接收一个完整 H.264 访问单元。

    操作顺序：
        先收满固定帧头
        -> 解析 payload_length
        -> 再收满指定长度负载

    返回：
        tuple:
            (flags, frame_id, timestamp_ms, payload)
    """

    header_data = recv_exact(sock, HEADER_SIZE)
    (
        flags,
        frame_id,
        timestamp_ms,
        payload_length,
    ) = parse_header(header_data)

    payload = recv_exact(sock, payload_length)

    return (
        flags,
        frame_id,
        timestamp_ms,
        payload,
    )
