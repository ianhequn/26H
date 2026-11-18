# 双 K230 实机测试清单

## 0. 当前第一版范围

已经实现：

- 接收 K230 创建独立 Wi-Fi 热点。
- 发送 K230 使用固定 IP 自动连接。
- 摄像头 H.264 Baseline 硬件编码。
- TCP 完整帧传输和断线重连。
- 接收端 VDEC 硬件解码。
- ST7701 800x480 实时显示。
- 触摸 `REC`、`STOP`。
- 接收端 SD 卡 MP4 保存。

暂未集成到主界面：

- 录像文件列表。
- 触摸播放、暂停。
- 进度条定位。

必须先让本清单中的直播和录像全部通过，再开发回放页面。

## 1. 上电前

- 两块板都使用兼容旧通道号 API 的 CanMV 固件。
- 接收端连接 ST7701 800x480 触摸屏。
- 接收端插入可写 SD 卡。
- 发送端连接摄像头。
- 两块板暂时都连接 USB 串口，方便分别查看日志。

## 2. 接收端先启动

接通电源后先等待约 5 秒。屏幕应先持续约 3 秒显示四色彩条和：

```text
K230 DISPLAY OK
```

看到彩条说明 LCD、Display 和 OSD0 已经正常初始化。彩条结束后才进入完整
接收界面。

预期串口顺序：

```text
Two-K230 touch receiver.
Receiver AP: ...
Receiver AP IP: ('192.168.8.1', ...)
Live decoder started.
TCP server listening on 192.168.8.1:9000
```

屏幕预期显示：

```text
黄色状态点
WAIT
REC
STOP
```

检查手机或电脑的 Wi-Fi 列表，应能看到：

```text
K230_VIDEO
```

## 3. 再启动发送端

预期发送端串口：

```text
Two-K230 video transmitter.
Wi-Fi connected: ('192.168.8.2', ...)
TCP connected to 192.168.8.1:9000
Encoder started: 640x360 20 FPS 1200 kbit/s
```

预期接收端串口：

```text
Transmitter connected: ...
Live decoder synchronized at IDR.
```

屏幕状态应从 `WAIT` 变成绿色 `LIVE`，FPS 接近 20。

## 4. 录像测试

1. 点击 `REC`。
2. 顶部状态先显示 `WAIT_IDR`。
3. 最迟约 0.5 秒后显示 `RECORDING`。
4. 录制 10 秒。
5. 点击 `STOP`。
6. 等待串口出现 `Recording saved:`。

录像目录：

```text
/sdcard/records/
```

正式文件：

```text
REC_001.mp4
REC_002.mp4
```

只有 `.tmp.mp4` 表示录像没有正确完成，不要把它当作正式结果。

## 5. 稳定性测试

- 连续直播 5 分钟不黑屏。
- 连续录像 10 次，每次 10 秒。
- 每个 MP4 在电脑 VLC 中能从头播放到尾。
- 录像中途关闭发送端，接收端能保存已经收到的内容。
- 发送端重新上电后，接收端重新出现画面。
- 接收端重启后，发送端能够自动重连。

## 6. 常见定位

### 看不到热点

先单独运行官方 `network_wlan_ap.py`。若官方例程也失败，说明是固件或无线
模块问题，不要先修改视频代码。

### TCP 已连接但屏幕黑

检查接收端是否打印：

```text
Live decoder synchronized at IDR.
```

没有这行表示没有收到 SPS/PPS 或 IDR，应先检查发送端 VENC 日志。

### 开机后从未出现四色彩条

先在 CanMV IDE 中单独运行：

```text
/sdcard/receiver/display_smoke_test.py
```

- 独立测试也黑屏：检查 ST7701 接线、屏幕供电、固件和 Display 驱动。
- 独立测试有彩条：显示硬件正常，继续查看 `/sdcard/receiver_boot.log`。
- 开机有彩条但随后黑屏：重点检查 `touch_ui.py` 的 OSD 刷新。

### 有画面但触摸位置不对

屏幕旋转方向和触摸坐标方向不一致。只调整接收端显示、触摸旋转配置，不改
网络、VDEC 或录像模块。

### MP4 不能播放

确认点击 `STOP` 后出现 `Recording saved:`。未停止就断电时 MP4 索引可能
尚未写完。
