# 两块 K230 图传代码导读

这份导读的目标不是让你一次记住所有 API，而是先建立完整链路：

```text
发送端摄像头
    -> VENC 编码成 H.264
    -> 加 24 字节协议头
    -> TCP 发送
    -> 接收端拆协议头
    -> 一路送 VDEC 显示
    -> 另一路按需封装成 MP4
```

## 一、先区分三类名字

### 1. 常量

常量是运行期间不应该随意改变的配置或协议规则，名称全部使用大写字母。

```python
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 360
FLAG_KEY_FRAME = 0x02
```

- `VIDEO_WIDTH`、`VIDEO_HEIGHT` 是可以根据项目需求调整的配置常量。
- `FLAG_KEY_FRAME` 属于收发两端共同遵守的协议常量，不能只修改一端。
- 带单位的常量会在名称或注释中标明，例如 `_MS` 表示毫秒。

### 2. 函数

函数完成一个明确动作，名称通常是“动词 + 对象”：

```python
connect_wifi_forever()
send_frame(...)
receive_frame(...)
```

函数参数是它完成动作所需要的输入，`return` 是它交给调用者的结果。

### 3. 类

类用来管理一组有关联的状态和动作。例如 `H264EncoderSource` 同时保存
Sensor、VENC 通道和启动状态，避免这些资源散落在主程序中。

```python
source = H264EncoderSource()
source.start()
frame = source.read_access_unit()
source.stop()
```

## 二、推荐阅读顺序

不要先读两个 `main.py`。它们只是程序入口，真正的逻辑在下面这些模块里。

### 第 1 步：公共传输协议

先读 `common/frame_protocol.py`。

重点理解：

1. TCP 是连续字节流，本身不知道“一帧从哪里开始、在哪里结束”。
2. `build_header()` 给每个 H.264 数据单元增加固定 24 字节头。
3. `send_all()` 解决一次 `send()` 不一定发送完全部数据的问题。
4. `recv_exact()` 解决一次 `recv()` 不一定收到完整数据的问题。
5. `send_frame()` 和 `receive_frame()` 是收发两端真正调用的对称接口。

协议帧结构：

```text
24 字节固定头部                         可变长度负载
+-------+---------+-------+----------+------------------+
| magic | version | flags | 长度等信息 | H.264 Annex-B 数据 |
+-------+---------+-------+----------+------------------+
```

其中：

- `magic` 用来确认收到的是本项目协议。
- `flags` 标记参数集、关键帧或普通 P 帧。
- `frame_id` 用来观察是否丢帧。
- `timestamp_ms` 是录像和以后进度条定位的时间基础。
- `payload_length` 告诉接收端还要读取多少字节。

### 第 2 步：认识 H.264 数据

再读 `common/h264_annexb.py`。

摄像头原始图像很大，VENC 把它压缩成 H.264。H.264 Annex-B 数据由多个
NAL Unit 组成，常见类型如下：

```text
SPS/PPS -> 告诉解码器视频尺寸、编码参数等
IDR     -> 可以独立开始解码的关键帧
P 帧    -> 依赖前面的图像，只记录变化
```

`extract_parameter_sets()` 提取 SPS/PPS，`contains_idr()` 判断当前数据是否
包含 IDR。接收端新连接后必须先等参数集和 IDR，不能随便从 P 帧开始解码。

### 第 3 步：发送端网络

依次读：

```text
transmitter/config.py
transmitter/wifi_client.py
transmitter/tcp_client.py
```

网络关系是：

```text
接收 K230：建立热点 K230_VIDEO，固定 IP 192.168.8.1
发送 K230：连接该热点，使用固定 IP 192.168.8.2
发送 K230：主动连接 192.168.8.1:9000
```

这里没有 RTSP，也不需要输入会变化的 URL。接收板既是热点，也是 TCP
服务器；发送板是 Wi-Fi 客户端和 TCP 客户端。

`connect_wifi_forever()` 和 `connect_receiver_forever()` 名称中的
`forever` 表示失败后会等待并重试，不会因为一次连接失败就结束程序。

### 第 4 步：发送端编码

读 `transmitter/encoder_source.py`。

`H264EncoderSource.start()` 完成四件事：

1. 初始化摄像头 Sensor。
2. 设置输出尺寸和像素格式。
3. 创建 H.264 VENC 编码通道。
4. 建立 Sensor 到 VENC 的媒体链路并开始采集。

`read_access_unit()` 每次取出一个编码数据单元，返回：

```python
(flags, timestamp_ms, payload)
```

- `flags`：这份数据是参数集、IDR 还是 P 帧。
- `timestamp_ms`：从启动编码开始计算的视频时间。
- `payload`：真正要传输的 H.264 字节。

注意 `GetStream()` 和 `ReleaseStream()` 必须成对出现。VENC 输出缓冲区是
有限资源，只取不还会导致后续编码停止。

### 第 5 步：发送端总流程

读 `transmitter/sender_app.py`，最后再看 `transmitter/main.py`。

核心循环可以缩写为：

```python
连接 Wi-Fi

while True:
    连接接收板 TCP
    启动 VENC

    while TCP 正常:
        从 VENC 取得一帧
        加协议头并发送

    停止 VENC
    关闭 TCP
    等待后重连
```

TCP 连接成功后才重新启动 VENC，是为了让每次新连接都能尽快收到新的
SPS/PPS 和 IDR，而不是从无法独立解码的 P 帧中间接入。

## 三、接收端阅读顺序

### 第 6 步：热点、存储和 TCP 服务器

依次读：

```text
receiver/config.py
receiver/wifi_ap.py
receiver/storage.py
receiver/tcp_server.py
```

`start_access_point()` 创建热点并把接收板地址设为 `192.168.8.1`。

`VideoFrameServer` 在后台线程中完成：

```text
监听 9000 端口
    -> 接受一个发送端连接
    -> 不断 receive_frame()
    -> 调用 on_frame 回调
    -> 断开后回到监听状态
```

这里的“回调”就是：TCP 模块只负责收数据，至于收到数据后显示还是录像，
由 `ReceiverApplication` 提供的函数决定。这就是模块解耦。

`storage.py` 只负责录像目录、文件名和剩余空间，不关心 H.264 或屏幕。

### 第 7 步：实时解码显示

读 `receiver/live_decoder.py`。

`LiveVideoDecoder.start()` 初始化 VDEC、显示设备和媒体链路。

`decode_frame()` 不会把收到的每份数据直接塞给 VDEC，而是维护同步状态：

```text
刚连接
  -> 等 SPS/PPS
  -> 等 IDR
  -> 已同步，可以连续送入后续 P 帧
```

如果断线，`reset_stream_sync()` 会清除同步状态。重连后重新等待参数集和
关键帧，避免屏幕出现长时间花屏。

### 第 8 步：录像状态机

读 `receiver/mp4_recorder.py`。

录像不是“把网络字节原样写进 `.mp4` 文件”。MP4 是容器，需要轨道信息、
时间戳和正确收尾。`Mp4Recorder` 的状态变化如下：

```text
IDLE
  -> 点击 REC
WAITING_KEYFRAME
  -> 收到 SPS/PPS 和 IDR
RECORDING
  -> 点击 STOP，写完 MP4 尾部
IDLE
```

开始录像时先创建 `.tmp.mp4`。只有 MP4 正常关闭并且确实写入过视频帧，
才改名成最终 `.mp4`。突然断电时留下临时文件，不会把损坏文件伪装成一份
完整录像。

### 第 9 步：触摸界面

读 `receiver/touch_ui.py`。

这个模块只做三类事：

1. 绘制状态文字和 `REC`、`STOP` 按钮。
2. 轮询触摸坐标。
3. 命中按钮后调用 `on_record` 或 `on_stop` 回调。

它不直接操作 MP4，因此以后增加“文件列表、播放、进度条”时，可以替换或
扩展 UI，不需要修改发送端和传输协议。

### 第 10 步：接收端总调度

读 `receiver/receiver_app.py`，最后看 `receiver/main.py`。

`ReceiverApplication` 是接收端总调度层，把独立模块接起来：

```text
TCP on_frame 回调
    -> LiveVideoDecoder.decode_frame()  实时显示
    -> Mp4Recorder.process_frame()      录制开启时写 MP4

触摸 REC 回调
    -> Mp4Recorder.start()

触摸 STOP 回调
    -> Mp4Recorder.stop()
```

`run_forever()` 的主循环主要负责触摸轮询和 UI 刷新。网络接收放在后台线程，
这样等待 TCP 数据时不会让触摸按钮失去响应。

## 四、异常处理怎么读

### `raise RuntimeError(...)`

`raise` 表示当前函数发现无法继续的错误，主动把错误交给上层处理：

```python
if len(header_data) != HEADER_SIZE:
    raise RuntimeError("协议头长度错误")
```

它与 `return None` 的区别是：

- `return None` 常表示“没有结果，但属于允许情况”。
- `raise RuntimeError` 表示“程序状态不符合要求，继续运行可能产生错误”。

上层可以使用 `try / except / finally`：

```python
try:
    启动并运行模块
except Exception as error:
    打印错误并准备重试
finally:
    无论成功或失败都释放资源
```

### `wait_for` 和超时常量

本工程没有使用 `asyncio.wait_for()`。代码里的“等待”主要有三种：

```text
socket.settimeout(x)  -> 网络操作最多阻塞 x 秒
time.sleep(x)         -> 主动暂停 x 秒再重试
线程 join             -> 等待后台线程退出
```

相关常量会标明单位，例如：

```python
WIFI_CONNECT_TIMEOUT_S = 15
SOCKET_TIMEOUT_S = 5
RECONNECT_DELAY_S = 1
```

超时不是“程序必定失败”，而是防止程序无限卡在一次网络操作中。上层捕获异常
后可以关闭旧连接并重新建立链路。

## 五、第一遍阅读检查点

读完后先确认自己能回答下面六个问题：

1. 为什么 TCP 前面还要自己增加 24 字节协议头？
2. 为什么 `send()` 和 `recv()` 外面都要写循环？
3. SPS/PPS、IDR、P 帧分别解决什么问题？
4. 为什么接收端断线重连后不能直接显示第一份 P 帧？
5. 为什么录像先保存成 `.tmp.mp4`，停止后才改名？
6. 为什么触摸 UI 不直接调用底层 MP4 API？

这六个问题能讲清楚，就已经理解了第一版系统的骨架。之后再逐行学习 CanMV
的 Sensor、VENC、VDEC、Display 和 MP4 API，会容易很多。
