# YOLOv5n 224×224 ROI 分支

这是 H 题视觉方案中的另一条实验分支：以 800×480 图像中心区域裁成 480×480，再缩放到 224×224，在 K230/CanMV 上尝试 YOLOv5n 钢球检测。

它和报告中的最终 LAB 传统视觉方案不是同一个推理链路，因此这里标为“可复用实验分支”，不能把这组模型指标直接写成 H 题最终整机成绩。

## 当前保留

- `deploy/`：K230/CanMV 板端 224×224 推理脚本。
- `reports/EVAL_REPORT.source.md`：PT、ONNX、KModel 离线一致性记录。
- `reports/DISTILLATION_REPORT.source.md`：蒸馏和 PTQ 结果记录。

## 当前不保留

- `.pt`、`.onnx`、`.kmodel` 权重。
- 训练数据、完整运行目录、失败图片和大日志。
- 依赖本机路径的训练脚本和环境目录。

## 证据边界

- 报告记录了 PT/ONNX/KModel 的离线对比，其中 KModel simulator 时间不能当作 K230 实机 FPS。
- 报告明确写出尚未完成 KModel 到 K230 的同图回放，因此板端速度和连续视频稳定性仍是 `HARDWARE-TODO`。
- 板端脚本依赖 CanMV 的 `libs.PipeLine` 和 `libs.YOLO`，脱离对应固件不能在普通 Python 环境运行。

