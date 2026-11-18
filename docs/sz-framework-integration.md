# SZ 嵌入式框架与 H 题小车架构

本仓库是 2026 年电赛 H 题“车载平衡滚球运动控制系统”的视觉与边缘端部分，GitHub 展示名为“26电赛H满分省一技术方案分享”。小车主控的软件工程组织采用 SZ Embedded Code Framework 的分层方式，框架仓库见：[sz-embedded-code-framework](https://github.com/ianhequn/sz-embedded-code-framework)。

## 系统分工

```text
SZ MCU framework
  ├─ MSPM0 平台适配
  ├─ 灰度/编码器/IMU/OLED/电机驱动
  ├─ 共享数据与 PID
  └─ 循迹、摆杆平衡、通信和状态机任务

This repository
  ├─ Orange Pi/K230 视觉方案
  ├─ LAB 与 YOLO 钢球检测实验
  ├─ K230 RTSP/TCP 图传
  └─ 报告与离线评测
```

## 为什么分成两个仓库

- SZ 仓库保留可跨项目复用的 MCU 框架，不绑定某一道比赛题。
- 本仓库保留 H 题的视觉、K230、模型和报告证据。
- 两者通过钢球位置/通信接口协作，不把 CanMV、Orange Pi 和 MCU SDK 混成一个不可复用的大工程。

## 复用入口

如果要复刻小车主控，先读 SZ 框架的架构说明和 `SZ_PAL → SZ_RTE → SZ_data → Algorithm → Tasks` 路径；如果要复刻视觉链路，先读本仓库的 [K230 部署说明](k230-deployment.md)、[评测报告](../results/EVAL_REPORT.md) 和 `k230/` 入口。

