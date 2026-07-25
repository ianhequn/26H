# v1.0.0 - Distilled YOLOv5n-224 for K230

This release contains the verified artifacts used by the repository results:

- `steel_ball_yolov5n_224_distilled_best.pt`: distilled PyTorch checkpoint
- `steel_ball_yolov5n_224_distilled.onnx`: fixed-shape ONNX export
- `steel_ball_yolov5n_224_distilled_balanced_ptq2.kmodel`: balanced-calibration KModel
- `steel_ball_yolov5n_224_roi.kmodel`: deployment alias of the same KModel
- `artifact_manifest.json`: file sizes, SHA256 hashes, and KModel tensor interface

Verified KModel interface:

```text
input:  uint8 [1, 3, 224, 224]
output: float32 [1, 3087, 6]
```

The public repository excludes the original training dataset. Metrics in the
repository are real outputs from the 80-image validation set, not estimates.
