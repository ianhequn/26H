# K230 OpenCV 绿色槽体与小球像素误差

## 结论

当前最小方案不使用神经网络：

1. 先硬裁固定的中间长条 ROI，外围像素不参加运算；
2. HSV 阈值分割绿色槽体；
3. 用绿色二值图的图像矩直接求槽体角度和中点，不再检测槽体轮廓；
4. 只接受槽体轴线附近、四周有绿色包围的非绿色圆斑；
5. 用上一帧位置拒绝螺丝和反光造成的坐标大跳；
6. 把小球与槽体中点的像素差投影到槽体坐标系；
7. K230 通过 UART2 发送固定格式 ASCII 数据。

## 目录

```text
K230_OpenCV_Green_Ball_Center/
├─ detector.py          # 公共 OpenCV 检测算法
├─ pc_validate.py       # PC 图片验证，生成调试图
├─ test_detector.py     # 两张样图的最小回归测试
├─ main.py              # K230 CanMV 实时程序
├─ requirements.txt
├─ test_images/         # 原始样图
└─ output/              # 标注图、掩膜、JSON 结果
```

## PC 先验证

在 PowerShell 中运行：

```powershell
cd "$env:USERPROFILE\Desktop\K230_OpenCV_Green_Ball_Center"
python -m pip install -r requirements.txt
python pc_validate.py
python test_detector.py
```

正确现象：

- 蓝框：绿色槽体的外接矩形；
- 蓝点：槽体中点；
- 红框/红点：小球及其中点；
- 黄线：槽体中点指向小球中点；
- 控制台出现 `GB,1,...`；
- `output/results.json` 中两张图的 `valid` 都为 `true`。

## 放到 K230

当前 `main.py` 已经内嵌全部检测逻辑。只需复制一个文件到 K230：

```text
main.py
```

在 CanMV IDE 中运行 `main.py`。程序使用：

- 图像：稳定测试版使用 `320 × 240`、`Sensor.RGB888`；
- 算法：K230 自带 `cv2`；
- 检测ROI：整帧中一个横向长条 `(x=0, y=65, w=320, h=110)`；
- 调试显示：`Display.VIRT`、320×110；
- 串口：UART2；
- 引脚：IO11 为 TX，IO12 为 RX；
- 波特率：115200，8N1。

当前默认：

- `BINARY_DEBUG_MODE = False`；
- 运行角度映射、小球检测和 UART 输出；
- 不画框、不画点、不画文字；
- HSV、图像矩、小球轮廓和帧缓冲都只处理 320×110 长条 ROI；
- IDE 日志显示数据包、槽体角度、FPS 和剩余内存。

需要重新观察二值图时，将下面参数临时改为 `True`：

```python
BINARY_DEBUG_MODE = True
```

当前为观察ROI，正常检测模式已临时打开IDE彩色图像回传：

```python
NORMAL_COLOR_DISPLAY_ENABLE = True
```

确认画面后改回 `False`，可以省去虚拟帧缓冲和JPEG回传时间。

如果你只需要 K230 向 STM32 发数据，至少连接：

```text
K230 IO11 (UART2_TX) -> STM32 UART_RX
K230 GND             -> STM32 GND
```

不要把 5V 串口电平直接接入 K230。先确认板卡 IO 电压域是 1.8V 还是
3.3V，并与接收端匹配。

## 串口协议

有效目标：

```text
GB,1,dx,dy,ball_x,ball_y,rail_x,rail_y\n
```

示例：

```text
GB,1,-332,12,296,315,628,303
```

含义：

- `1`：本帧有效；
- `dx < 0`：小球沿槽体轴线位于中点左侧；
- `dx > 0`：小球沿槽体轴线位于中点右侧；
- `dy`：小球偏离槽体轴线的垂直误差；
- 后四项用于现场排错，控制器只用 `dx/dy` 也可以。

未检测到槽体或小球：

```text
GB,0,0,0,-1,-1,-1,-1
```

控制器收到 `valid=0` 时必须保持上一控制量或进入安全状态，不能把
`dx=0` 当作真的居中。

## 参数怎么调

K230参数集中在 `main.py` 顶部。

### 绿色漏检

优先逐步放宽：

```python
S_MAX = 255
S_MIN = 50
V_MAX = 255
V_MIN = 25
```

如果背景中的其他绿色也被识别，再收紧：

```python
H_MIN = 40
H_MAX = 90
```

### 小球漏检

- 球太小：降低 `BALL_MIN_DIAMETER`；
- 球太大：提高 `BALL_MAX_DIAMETER`；
- 球轮廓被光斑切碎：降低 `BALL_MIN_CIRCULARITY`；
- 球边缘绿色不足：降低 `BALL_MIN_GREEN_SURROUND_RATIO`；
- 快速运动被拒绝：适当提高 `BALL_MAX_FRAME_JUMP`。

## 当前方案的边界

这套算法依赖固定机位、绿色槽体、金属球和稳定光照，适合先把控制闭环跑通。

当前把 ROI 水平中点 `x=160` 作为槽体物理中点，并用实时角度计算对应的
`y` 坐标。正式固定相机后仍要把球放在真实机械中点，检查输出 `dx` 是否接近
0；若存在固定偏差，再调整 `RAIL_TARGET_X`。

后续升级顺序建议：

1. 固定曝光、白平衡和相机位置；
2. 采集 50~100 帧，统计 `dx` 抖动；
3. 再加 3 帧中值滤波；
4. 最后做像素误差到电机控制量的 PID/PD 映射。
