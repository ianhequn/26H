# PT、ONNX、KModel精度一致性评测报告

## 结论

PT与ONNX在严格统一的224×224输入下几乎逐元素一致；KModel量化后出现轻微精度下降，但不是灾难性退化。

- PT → ONNX：没有发现可测量的精度损失。
- ONNX → KModel：mAP50下降约1.89个百分点，Precision下降约4.38个百分点。
- KModel → K230实际部署：本次没有板端同图回放，不能伪造板端结论。
- 当前最明显的转换风险是50张校准图全部为无球负样本。

## 严格统一224×224评测

| model | P | R | mAP50 | mAP50-95 | infer ms | post ms | FP | FN | center error px |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| pt | 0.810415 | 0.856502 | 0.847643 | 0.359366 | 19.487 | 0.383 | 40 | 31 | 1.546 |
| onnx | 0.810415 | 0.856502 | 0.847643 | 0.359376 | 5.536 | 0.330 | 40 | 31 | 1.546 |
| kmodel | 0.766579 | 0.878924 | 0.828708 | 0.352594 | 8770.754 | 0.535 | 42 | 33 | 1.529 |

KModel的Simulator时间约8.77秒/张，仅代表Windows软件模拟器，不能当作K230板端推理时间。板端实际速度应使用你现有的CanMV计时脚本。

## 官方YOLOv5 val.py结果

| model | 实际输入 | P | R | mAP50 | mAP50-95 | inference ms | NMS ms |
|---|---|---:|---:|---:|---:|---:|---:|
| PT | 256×256（rect padding） | 0.851 | 0.879 | 0.877 | 0.367 | 21.9 | 5.1 |
| ONNX | 224×224 | 0.860 | 0.851 | 0.864 | 0.361 | 8.7 | 9.8 |

这两行用于复核原生YOLOv5工具链，但因为实际输入不同，不作为PT与ONNX精度损失的最终判断。

## 输出一致性

- PT与ONNX检测数量相同：80/80张。
- PT与KModel检测数量相同：78/80张。
- PT与ONNX匹配框平均IoU约0.9999996，中心差约0.000002px。
- PT与KModel匹配框平均IoU约0.9748，中心差约0.082px。
- KModel相对PT的匹配框置信度平均下降约0.0093。
- PT正确但KModel出现漏检：2张。
- PT无误检但KModel出现误检：3张。

## 失败案例

- 有失败分类记录的图片：33张。
- 量化回退：3张。
- 背景误检：0张。
- 多球漏检：24张。
- 主要问题集中在4球图的额外框和漏框，以及少量量化后阈值边缘变化。

## 精度下降位置

当前属于轻微的情况A：PT与ONNX严格统一输入后完全一致，下降主要发生在ONNX → KModel量化阶段。校准集全为负样本是首要嫌疑。

## 推荐下一步

1. 重建50至100张有代表性的校准集，至少覆盖无球、单球、多球、远近、强反光、暗光和运动模糊。
2. 保持当前PT和ONNX不变，仅重新转换KModel做A/B对照。
3. 代表性校准集完成后，再比较ptq_option 0、2、3；当前下降较轻，本轮没有额外生成ptq0和ptq3，避免在错误校准集上浪费时间。
4. 在K230上用同一批静态图片回放，补齐KModel → K230实际部署层验证。

## 文件索引

- 环境审计：`environment_inspection.md`
- 三模型汇总：`results/all_models_summary.csv`
- 官方val汇总：`results/official_yolov5_val_summary.csv`
- 每图结果：`results/per_image_results.csv`
- 模型差异：`results/model_output_differences.csv`
- 校准分析：`results/calibration_analysis.json`
- 模型血缘：`results/lineage_manifest.json`
- 失败图：`failures/`
- 图表：`plots/`
