# PT to ONNX to KModel

## 1. PT Checkpoint

The PyTorch checkpoint contains the YOLOv5 structure, weights, class metadata, strides, and training state. K230 cannot directly execute it.

## 2. ONNX Export

The export command uses:

```text
input:  float32 [1, 3, 224, 224]
output: float32 [1, 3087, 6]
opset:  12
batch:  1
```

`tools/export_yolov5_compat.py` patches two compatibility changes:

```python
torch.load(..., weights_only=False)
torch.onnx.export(..., dynamo=False)
```

These allow a recent PyTorch installation to load the legacy YOLOv5 checkpoint and use the tested legacy exporter.

PT and ONNX produced effectively identical validation results, so export was not the source of the original accuracy drop.

## 3. ONNX Simplification

Before nncase import, the converter runs ONNX shape inference and `onnxsim` simplification with a fixed input:

```text
[1, 3, 224, 224]
```

224 is already divisible by the YOLO stride of 32.

## 4. K230 Compile Configuration

```text
target:       k230
preprocess:   true
input type:   uint8
input layout: NCHW
input range:  [0, 1]
swapRB:       false
mean:         [0, 0, 0]
std:          [1, 1, 1]
```

The board sends uint8 RGB input. KModel preprocessing converts it to the floating-point range expected by the exported YOLOv5 graph.

## 5. PTQ Calibration

Post-training quantization observes representative images and estimates tensor distributions. Those distributions determine scale, zero point, and clipping behavior.

The old conversion used 50 background-only images. It could not observe target-related activation peaks.

The new conversion uses:

```text
calibration method: NoClip
PTQ option:         2
activation type:    int16
samples:            100
```

The calibration images include both targets and hard negative backgrounds.

## 6. Evidence

Old conversion:

```text
PT mAP50:     0.8476
KModel mAP50: 0.8287
drop:         0.0189
```

New conversion:

```text
PT mAP50:     0.8538
KModel mAP50: 0.8520
drop:         0.0018
```

This shows that the new calibration process preserved the model much more effectively.

## 7. Preprocessing Must Match

Calibration, offline evaluation, and K230 deployment must agree on:

- RGB vs BGR
- NCHW vs NHWC
- resize vs letterbox
- input range
- mean and standard deviation
- ROI crop

A high-quality PT model cannot compensate for a deployment preprocessing mismatch.

