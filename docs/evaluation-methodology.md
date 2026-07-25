# Evaluation Methodology

The evaluator compares PT, ONNX, and KModel with one shared pipeline.

## Standard AP Settings

```text
input size:      224
batch size:      1
confidence:      0.001
NMS IoU:         0.60
max detections:  5
AP IoU range:    0.50:0.05:0.95
```

Metrics:

- Precision
- Recall
- mAP50
- mAP50-95
- inference time
- postprocessing time

## Deployment Settings

```text
confidence: 0.25
NMS IoU:    0.45
max det:    5
```

Statistics:

- predicted count per image
- false positives
- false negatives
- background false-positive rate
- multi-ball miss rate
- matched center error

## Center Error

Predictions and labels are matched one-to-one when IoU is at least 0.5:

```text
error = sqrt((pred_x - gt_x)^2 + (pred_y - gt_y)^2)
```

Unmatched predictions are not included in center-error averaging.

## Dataset Limitation

The validation set contains:

```text
0 balls: 23 images
1 ball:   1 image
3 balls:  2 images
4 balls: 54 images
```

It is useful for comparing model versions but is not a balanced estimate of all possible scenes. Independent video-based test sets are still needed.

## Simulator Timing

nncase Simulator executes KModel in Windows software simulation and is extremely slow. Its timing must never be interpreted as K230 FPS.

