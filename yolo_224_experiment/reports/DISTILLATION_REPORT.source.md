# YOLOv5s-320 -> YOLOv5n-224 蒸馏与 KModel 报告

生成日期：2026-07-25

## 结论

已完成检测头软目标蒸馏、ONNX 导出、平衡校准集 PTQ 转换和统一离线评测。
最终推荐模型：

`steel_ball_yolov5n_224_distilled_balanced_ptq2.kmodel`

它没有覆盖原始部署 KModel。

## 教师与蒸馏方法

- Teacher：YOLOv5s，原训练输入 320×320。
- Student：YOLOv5n，部署输入 224×224。
- Teacher 在相同 224 验证输入下实测：P=0.860、R=0.919、mAP50=0.904、mAP50-95=0.389。
- 训练损失：人工标签 YOLOv5 loss + Detect 三尺度 objectness/box/class 软目标 loss。
- `kd_weight=0.04`，temperature=2.0，蒸馏权重前 5 epoch 线性增加。
- Student 从原 YOLOv5n-224 `best.pt` 开始微调 40 epoch。

## 平衡校准集

- 总数：100
- 标签数量分布：{0: 20, 1: 4, 3: 4, 4: 71, 5: 1}
- 数据来源：仅训练集，不包含验证集。
- 选择依据：钢球数量、亮度、对比度、Laplacian 清晰度覆盖。
- nncase：target=k230，input=224×224，PTQ option=2，100 calibration samples。

## 统一评测

| 模型 | Precision | Recall | mAP50 | mAP50-95 | 部署P | 部署R | FP | FN | 多球漏检图 | 中心误差(px) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline_pt | 0.8104 | 0.8565 | 0.8476 | 0.3594 | 0.8276 | 0.8610 | 40 | 31 | 22 | 1.546 |
| baseline_kmodel | 0.7666 | 0.8789 | 0.8287 | 0.3526 | 0.8190 | 0.8520 | 42 | 33 | 24 | 1.529 |
| distilled_pt | 0.7986 | 0.8700 | 0.8538 | 0.3740 | 0.8553 | 0.9013 | 34 | 22 | 17 | 1.585 |
| distilled_onnx | 0.7986 | 0.8700 | 0.8538 | 0.3740 | 0.8553 | 0.9013 | 34 | 22 | 17 | 1.585 |
| distilled_kmodel | 0.8110 | 0.8465 | 0.8520 | 0.3726 | 0.8511 | 0.8969 | 35 | 23 | 18 | 1.582 |

## 新旧 KModel 差异

- mAP50：0.8287 -> 0.8520（+2.33 个百分点）
- mAP50-95：0.3526 -> 0.3726（+2.00 个百分点）
- 部署 Precision：0.8190 -> 0.8511
- 部署 Recall：0.8520 -> 0.8969
- 误检：42 -> 35
- 漏检：33 -> 23
- 多球漏检图片：24 -> 18
- 背景误检图片：0 -> 0

## 模型接口

- KModel input：uint8 [1, 3, 224, 224]
- KModel output：['float32'] [[1, 3087, 6]]
- 输出语义：YOLOv5 `[batch, 3087, 6]`，1 类 `steel_ball`。

## 注意

- Windows nncase Simulator 平均耗时不是 K230 板端 FPS，不能用于估算实时帧率。
- 新模型仍需在 K230 上用现有 224 ROI 主程序测试真实 `infer ms`、曝光变化和连续视频稳定性。
- 当前验证集以 4 球图片为主，后续仍应补充更多 1/2/3/5 球独立视频验证数据。

## 复现顺序

```powershell
cd path\to\steel_ball_yolov5n_224_roi_workspace
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\train_distilled_yolov5n_224.ps1"
& "path\to\python.exe" `
  ".\scripts\create_balanced_calibration.py" `
  --dataset ".\yolo_dataset_roi_224" `
  --output ".\distillation\calibration_images" `
  --count 100 `
  --negative-count 20
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\export_distilled_yolov5n_224.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\convert_distilled_yolov5n_224_kmodel.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\eval_distilled_models.ps1"
& "path\to\python.exe" `
  ".\scripts\finalize_distillation.py"
```
