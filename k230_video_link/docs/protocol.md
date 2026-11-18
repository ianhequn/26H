# TCP H.264 帧协议

## 目的

TCP 只保证字节有序，不保留应用层消息边界。发送端每个 H.264 访问单元前
增加一个固定 24 字节头部，接收端才能准确取出完整帧。

## 帧格式

所有多字节整数使用网络字节序。

| 字段 | 大小 | 类型 | 说明 |
|---|---:|---|---|
| magic | 4 B | bytes | 固定为 `K230` |
| version | 1 B | uint8 | 当前为 1 |
| flags | 1 B | uint8 | 参数集、I 帧、P 帧标志 |
| reserved | 2 B | uint16 | 保留 |
| frame_id | 4 B | uint32 | 协议单元递增编号 |
| timestamp_ms | 8 B | uint64 | 视频时间戳，单位毫秒 |
| payload_length | 4 B | uint32 | 后续 H.264 字节数 |
| payload | 可变 | bytes | H.264 Annex-B 数据 |

## Flags

```text
0x01  FLAG_CODEC_HEADER  包含 SPS/PPS
0x02  FLAG_KEY_FRAME     包含 IDR
0x04  FLAG_P_FRAME       包含 P 帧
```

同一访问单元可以同时设置多个标志，例如：

```text
FLAG_CODEC_HEADER | FLAG_KEY_FRAME
```

## 接收约束

- 必须循环读取满 24 字节头部。
- 必须根据 `payload_length` 循环读取完整负载。
- `payload_length` 超过 2 MiB 时立即拒绝。
- 新 TCP 连接必须等待 SPS/PPS 和 IDR 后才开始显示。
- MP4 录像必须从带参数集的 IDR 开始。

协议实现位于 `common/frame_protocol.py`，H.264 参数集解析位于
`common/h264_annexb.py`。
