# Two K230 Video Link

两块 K230 组成的独立图传系统：

```text
发送 K230
Camera -> H.264 VENC -> TCP

接收 K230
Wi-Fi AP -> TCP -> VDEC -> ST7701 LCD
                    -> MP4 -> SD card
```

系统不依赖手机热点、赛场 Wi-Fi、PAD 或笔记本。传输协议与发送硬件解耦，
以后可以用树莓派实现同一协议替换发送 K230。

## 先看代码

第一次阅读不要直接钻进 `main.py`。请按
[代码导读](docs/code_walkthrough.md) 的顺序理解协议、发送端和接收端。
源码中的常量、函数、状态机和异常处理均已补充中文注释。

## 目录结构

```text
two_k230_video_link/
├── common/          # 硬件无关协议和 H.264 解析
├── transmitter/     # 发送端网络、VENC 和监督循环
├── receiver/        # AP、TCP、VDEC、录像和触摸 UI
├── tests/           # 可在电脑运行的协议测试
├── tools/           # 生成 SD 卡上传目录
├── docs/            # 协议和实机检查清单
└── dist/            # 运行构建脚本后生成
```

## 默认硬件和固件

- 发送端：K230 + 摄像头。
- 接收端：K230 + ST7701 800x480 触摸屏 + SD 卡。
- 代码针对旧版 CanMV 通道号 API：
  `Encoder.GetStream(venc_chn, stream_data)`。
- 默认视频：640x360、20 FPS、H.264 Baseline、1200 kbit/s。

若屏幕不是 ST7701，修改 `receiver/config.py` 中的显示配置，其他模块不动。

## 电脑测试

```powershell
cd path\to\two_k230_video_link
python -m unittest discover -s tests -v
```

## 生成部署目录

```powershell
python tools\build_deploy.py
```

生成：

```text
dist/transmitter_sdcard/
dist/receiver_sdcard/
```

## 上传发送端

把 `dist/transmitter_sdcard/` 里面的内容保持目录结构上传到发送板：

```text
/sdcard/main.py
/sdcard/common/
/sdcard/transmitter/
```

## 上传接收端

把 `dist/receiver_sdcard/` 里面的内容保持目录结构上传到接收板：

```text
/sdcard/main.py
/sdcard/common/
/sdcard/receiver/
```

## 首次运行

1. 先启动接收 K230。
2. 冷启动约 5 秒后，确认屏幕先出现约 3 秒四色彩条。
3. 确认屏幕随后出现 `WAIT`，串口显示 TCP 监听地址。
4. 再启动发送 K230。
5. 等待接收端显示 `LIVE` 和实时画面。
6. 点击 `REC`，录制 10 秒。
7. 点击 `STOP`，等待串口打印保存完成。

若接收端仍然黑屏，在 CanMV IDE 中单独运行
`receiver/display_smoke_test.py`。该脚本不启动网络和 VDEC，可用于判断问题
是在 LCD/Display 层，还是在完整接收应用中。

详细检查点见 [硬件测试清单](docs/hardware_test_checklist.md)。

## 当前边界

第一版完成实时图传和触摸录像。文件列表、触摸回放和进度条属于接收端第二
阶段，不会侵入当前协议、发送端或录像模块。
