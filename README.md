
## 2026 电赛 H 题小车案例分享

来自广东工业大学信息工程学院；报告为提交的原报告，最终评测接近满分。

本仓库的 K230/视觉部分服务于 2026 年电赛 H 题“车载平衡滚球运动控制系统”。小车主控的软件架构采用 [SZ Embedded Code Framework](https://github.com/ianhequn/sz-embedded-code-framework) 的分层方式：平台适配、驱动模型、共享数据、PID/算法和任务状态机彼此分开；本仓库负责 Orange Pi/K230 视觉、模型部署、图传和报告材料。

整机报告候选版见 [`report/H题_提交版候选.pdf`](report/H题_提交版候选.pdf)，架构映射见 [`docs/sz-framework-integration.md`](docs/sz-framework-integration.md)。




