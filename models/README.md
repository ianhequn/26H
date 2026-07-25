# Model Artifacts

Model binaries are not committed to Git history. Download the following files from the repository Releases page:

- `steel_ball_yolov5n_224_distilled_best.pt`
- `steel_ball_yolov5n_224_distilled.onnx`
- `steel_ball_yolov5n_224_roi.kmodel`

Verified KModel interface:

```text
input:  uint8 [1, 3, 224, 224]
output: float32 [1, 3087, 6]
```

The SHA256 hashes are recorded in `results/artifact_manifest.json`.

