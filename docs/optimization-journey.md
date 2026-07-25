# Optimization Journey

## 1. Starting Point

The first working deployment used YOLOv5s with `320×320` input:

```text
Inference time: about 34 ms
End-to-end speed: about 29-30 FPS
```

Its visual accuracy was good, but the target was a faster vision module suitable for real-time vehicle control.

## 2. Engineering Goal

The target was not simply “use a smaller model”. The constraints were:

- K230 CanMV deployment
- central camera region is the useful working area
- up to five steel balls
- coordinates sent through USB serial
- approximately 60 FPS camera loop
- acceptable multi-ball recall

This led to:

```text
1920×1080 sensor
-> center 1080×1080 ROI
-> resize to 224×224
-> YOLOv5n
```

The ROI removes pixels that the controller does not use, allowing a smaller network input without changing the physical working region.

## 3. Dataset Work

The initial ROI dataset contained:

```text
Train: 320 images, 949 instances, 79 backgrounds
Val:    80 images, 223 instances, 23 backgrounds
```

Hard negative images were deliberately included because reflections and circular patterns can resemble steel balls.

The validation split is imbalanced and mainly contains four-ball images. This limitation is documented rather than hidden.

## 4. Baseline YOLOv5n-224

The first student model reached:

```text
Strict mAP50:    0.8476
Strict mAP50-95: 0.3594
```

This established that a 224 input was viable, but it did not explain whether deployment losses came from training, ONNX export, quantization, or board preprocessing.

## 5. Unified PT / ONNX / KModel Evaluation

A single evaluator was built so all backends used the same:

- validation images
- RGB preprocessing
- input size
- NMS implementation
- confidence and IoU thresholds
- maximum detections
- AP calculation
- center-error matching

The result was decisive:

```text
PT and ONNX: effectively identical
KModel: measurable accuracy loss
```

Therefore, ONNX export was not the main problem.

## 6. Root Cause: Calibration Bias

The old export script selected calibration images with:

```powershell
Get-ChildItem $datasetImages -File | Select-Object -First 50
```

Because filenames beginning with `negative_hard_` came first, all 50 calibration images were backgrounds:

```text
Positive calibration images: 0
Negative calibration images: 50
```

PTQ calibration estimates activation ranges. A set containing no target objects cannot represent target-related activations, confidence peaks, close multi-ball layouts, or reflections on the balls.

## 7. Teacher Validation

The available YOLOv5s-320 checkpoint was evaluated at the same `224×224` input used by the student:

```text
Precision: 0.860
Recall:    0.919
mAP50:     0.904
mAP50-95:  0.389
```

Its three Detect feature maps matched the student shapes:

```text
[B, 3, 28, 28, 6]
[B, 3, 14, 14, 6]
[B, 3,  7,  7, 6]
```

This made direct detection-head distillation possible.

## 8. Distillation Experiment

The total loss was:

```text
L_total = L_YOLO_ground_truth + lambda * L_distillation
```

The distillation term compared:

- objectness soft targets
- box regression outputs
- class outputs

The first smoke test used:

```text
kd_weight = 0.15
```

The weighted distillation loss became larger than the supervised loss and validation accuracy dropped. This was not discarded as noise; the loss scales were inspected.

The final setting was:

```text
kd_weight = 0.04
temperature = 2.0
warmup = 5 epochs
```

The supervised labels remained the primary signal, while teacher guidance acted as a regularizer.

## 9. Balanced Quantization Calibration

The new 100-image calibration set contained:

```text
0 balls: 20
1 ball:   4
3 balls:  4
4 balls: 71
5 balls:  1
```

All rare positive-count images were retained. Common images were selected to cover brightness, contrast, and Laplacian sharpness.

This does not make the dataset perfectly balanced; it makes the calibration set representative of the available training distribution while ensuring that positive activations are present.

## 10. Final Outcome

```text
Baseline KModel mAP50:      0.8287
Distilled KModel mAP50:     0.8520

Baseline deployment FP/FN:  42 / 33
Final deployment FP/FN:     35 / 23
```

The final KModel preserved the PT/ONNX result far better than the old conversion.

## 11. Board Integration

The K230 program:

- captures the center ROI
- runs YOLOv5n-224
- maps centers back to `1920×1080`
- limits output to five balls
- sorts detections from left to right per frame
- sends a fixed 16-value FireWater packet

The recorded CanMV demo showed approximately 60 FPS while coordinate packets were continuously transmitted.

## 12. What Remains

The cleanest next experiment is an ablation:

```text
A: baseline PT + old calibration
B: baseline PT + balanced calibration
C: distilled PT + old calibration
D: distilled PT + balanced calibration
```

The current repository contains result D and the historical result A. B and C would quantify how much gain came from distillation and how much came from calibration.

