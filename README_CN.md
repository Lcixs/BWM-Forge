<h1 align="center">BWM-Forge: Agentic World Programming for Embodied Intelligence</h1>

<p align="center">
  <a href="README.md">English</a> | <strong>简体中文</strong>
</p>

<div align="center">

[![Project Page](https://img.shields.io/badge/%F0%9F%8C%90%20Project%20Page-Demo-00bfff)](https://bwm-forge.github.io/)
[![Paper](https://img.shields.io/static/v1?label=Paper&message=PDF&color=red&logo=arxiv)](https://arxiv.org/abs/2611.16221)
[![License](https://img.shields.io/badge/License-Apache--2.0-green)](LICENSE)

</div>

<p align="center">
  <img src="examples/main.jpg" width="100%" alt="BWM-Forge overview">
</p>

**BWM-Forge** 通过*对世界进行编程*生成可控的机器人视频，而不是端到端地预测像素。编程智能体编写可编辑的 Blender 场景程序，明确指定物体、机器人运动和相机；Blender 将其渲染为**白模视频（proxy video）**；视频生成模型再基于这些结构与运动添加真实感外观。

几何、运动和视角都显式保留在代码中，因此你可以移动某个物体、更换机器人，或用新的相机视角拍摄同一段交互，而无需从头开始。本仓库提供完整流程与本地 Gradio 工作区，将输入处理、Blender 建模、视频生成、人工审核和可复用的经验库整合在一起。

## 📖 目录

- [项目亮点](#highlights)
- [框架](#framework)
- [Demo](#demo)
- [快速开始](#quick-start)
- [使用指南](#user-guide)
- [贡献者](#contributors)
- [引用](#citation)
- [许可证](#license)

---

<a id="highlights"></a>

## 🌟 项目亮点

- **三种范式**：根据空间语义、运动先验或真实示范生成动作。
- **多种机器人形态**：支持机械臂、人形机器人、机械狗与灵巧手，可导入包含网格资源的本地 URDF。
- **分阶段、可编辑的建模**：场景、机器人和组合动画分别在独立的 Blender 阶段中构建。每轮保留 `.blend` 工程、脚本和预览，便于在已有结果上修改，无需重新生成。
- **视角控制**：通过修改指令更换相机，同时保持场景布局、机器人姿态和动画不变；也可以重新加载已完成的任务，从其他视角拍摄。
- **先评估，再修改**：使用白模视频和参考图像生成视频，结合本地客观指标与模型视觉评估指导修改。
- **轮次追踪与经验复用**：从已完成任务中提炼建模与渲染规则，供后续任务检索使用。

<a id="framework"></a>

## 🧩 框架

应用按顺序运行四个阶段：**Input → Blender（Scene → Robot → Composed）→ Render → Harness**。

<p align="center">
  <img src="examples/BWM-Forge.jpg" width="100%" alt="BWM-Forge framework">
</p>

<a id="demo"></a>

## 🎬 Demo

https://github.com/user-attachments/assets/419fc098-b061-4e2d-940e-27fd8a4a3cbf

<a id="quick-start"></a>

## 🚀 快速开始

### 环境准备

当前以 Windows 本地使用为主，界面与程序运行在同一台计算机上。

| 依赖 | 说明 |
| --- | --- |
| Python | 3.11 或更高版本，建议使用独立环境 |
| Blender | 需要可执行文件及其内置 Python，路径在 `config.py` 中配置 |
| Python 依赖 | 见 [requirements.txt](requirements.txt) |
| 模型服务 | 支持 Responses API 的模型服务，以及与当前接口实现兼容的视频生成服务 |
| Cloudflared | Render 阶段用于临时发布白模视频。从[官方发布页](https://github.com/cloudflare/cloudflared/releases)下载 `cloudflared.exe`，放入 `Render/tools/` |

模型请求和视频生成需要联网。完整流程会通过 Cloudflare 临时隧道提供所选白模视频，供视频服务读取。

### 1. 安装依赖

克隆或下载仓库后，在仓库根目录执行：

```bash
conda create -n bwm python=3.11 -y
conda activate bwm
python -m pip install -r requirements.txt
```

安装 Blender，并确认可以在本机启动。Blender 是独立安装的软件，不包含在 Python 依赖中。

### 2. 配置模型与服务

运行参数集中在 [config.py](config.py)。首次使用时至少修改以下配置：

| 配置 | 用途 |
| --- | --- |
| `OPENAI_BASE_URL` | Responses API 的根地址，例如 `https://your-provider.example/v1` |
| `OPENAI_AUTH_FILE` | 通用模型服务共用的认证文件路径 |
| `INPUT_MODEL` | 首帧分析模型，需要支持图像输入 |
| `BLENDER_MODEL` | Blender 建模使用的 Codex 模型 |
| `BLENDER_EXE` | 本机 Blender 可执行文件的完整路径 |
| `RENDER_MODEL` | 视频视觉评估与阶段总结使用的模型，需要支持图像输入 |
| `HARNESS_MODEL` | 经验整理与更新使用的模型 |
| `SEEDANCE_BASE_URL` | 视频生成服务根地址 |
| `SEEDANCE_MODEL` | 视频生成模型名称 |
| `SEEDANCE_AUTH_FILE` | 视频服务认证文件路径 |

模型名称按服务商实际支持的名称填写。通用模型地址填写 API 根地址，程序会追加 `/responses`，无需填写完整请求端点。

默认情况下，Input、Blender、Render 和 Harness 共用 `OPENAI_BASE_URL` 与 `OPENAI_AUTH_FILE`。如需分别配置，可修改各阶段的 `*_OPENAI_BASE_URL` 和 `*_OPENAI_AUTH_FILE`。配置修改后需要重启程序。

视频服务使用独立的请求协议，具体接口见 [Render/seedance.py](Render/seedance.py)。更换服务商时，需要确认其提交、任务查询和媒体输入格式与当前实现兼容。

### 3. 填写 API 密钥

创建 `assets/api/` 文件夹，并在其中添加以下两个文件；也可以将 `config.py` 中的 `OPENAI_AUTH_FILE` 和 `SEEDANCE_AUTH_FILE` 指向其他位置。

`assets/api/auth.json`：

```json
{ "OPENAI_API_KEY": "REPLACE_WITH_YOUR_API_KEY" }
```

`assets/api/seedance_auth.json`：

```json
{ "SEEDANCE_API_KEY": "REPLACE_WITH_YOUR_API_KEY" }
```

### 4. 启动

在仓库根目录执行：

```bash
python -m gui.app
```

浏览器会自动打开；若未打开，请访问 <http://127.0.0.1:7860>。服务仅监听本机地址。

<a id="user-guide"></a>

## 🧭 使用指南

### 使用流程

![workflow](examples/GUI_workflow.gif)

#### 1. 准备输入

选择机器人类型和资产，填写任务描述，再选择输入模式：

| 模式 | 所需输入 | 动作来源 |
| --- | --- | --- |
| 空间语义驱动 | 首帧图像、机器人资产、任务描述 | 根据图像与描述生成动作 |
| 运动先验驱动 | 首帧图像、机器人资产、任务描述、NPZ / Parquet 轨迹、采样率 | 使用提供的动作轨迹作为参考 |
| 真实示范驱动 | 机器人资产、任务描述、MP4 / MOV 演示视频 | 根据演示视频生成动画 |

首帧图像支持 PNG、JPG 和 WebP。点击 **Create task and run Input** 创建任务。

导入新资产时，选择 **Import local robot asset**，填写运行程序这台计算机上的 URDF 路径和资产名称。请使用已展开的 URDF，并保留其与 `meshes/`、纹理等资源的相对目录关系；资源引用应能在本地解析。

#### 2. 审核 Blender 结果

Blender 按以下顺序完成建模：

1. **Scene**：生成场景，审核预览图。
2. **Robot**：准备机器人模型，审核预览图。
3. **Composed**：组合场景与机器人，生成动作并审核完整白模视频。

点击 **Accept and continue** 接受当前结果，或填写修改意见、按需附加参考图像后点击 **Submit revision**。每轮工程、脚本、预览和反馈会保存在任务目录中。

#### 3. 生成并确认视频

Blender 完成后，点击 **Continue generation** 手动启动视频生成。程序将白模视频与参考首帧发送给视频生成服务，再对结果进行评估。初版客观评分低于配置阈值时，流程最多自动生成一次修正版，之后进入完整评估与人工审核。

可以在界面中重新评估、提交修改，或点击 **Confirm final video** 确认最终视频。确认和阶段总结完成后，Harness 会归档结果并更新经验库。

#### 4. 从历史任务继续

点击 **Load historical task**，选择一个任务，再点击 **Load as new task**。这会将已完成任务的 Blender 结果复制到新任务中，并重新打开 Composed 审核，便于继续修改或更换相机视角拍摄场景。

单个工作区一次运行一个任务，等待人工审核时也会占用执行名额。

<a id="trajectory-format"></a>

### 轨迹格式

运动先验驱动模式接受以下两种文件格式：

- **NPZ**：包含名为 `action` 的二维实数数组，形状为 `(T, D)`。
- **Parquet**：包含名为 `action` 的列，每行是长度为 `D` 的实数向量。

数据必须为有限数值，不能包含 NaN、Inf 或对象数组。采样率在界面中显式填写。

| 动作空间 | 列定义 | 单位与约定 |
| --- | --- | --- |
| `joint` | 每列对应一个明确填写的 URDF 独立关节，按界面中的关节名称顺序排列 | 绝对关节位置；旋转关节为 rad，移动关节为 m |
| `eef` | 固定七列：`x, y, z, roll, pitch, yaw, gripper` | 基座坐标系下的绝对 TCP 位姿；位置为 m，角度为 rad，旋转顺序为 `Rz @ Ry @ Rx` |

使用 `joint` 轨迹时，机械臂、人形机器人和机器狗都要在界面中按轨迹数据的列顺序填写关节名：第 1 个名称对应第 1 列，以此类推。名称必须与所选资产 URDF 中可独立运动的关节名一致。

`eef` 仅用于机械臂。夹爪列以 `0` 闭合、`1` 张开作为约定参考，实际驱动映射取决于资产结构与标定信息。

<a id="outputs-and-experience-store"></a>

### 输出与经验库

每个任务保存在 `output/<task_id>/` 下，主要产物包括：

| 路径 | 内容 |
| --- | --- |
| `task.json` | 任务元数据与阶段状态 |
| `input/input.json` | 输入阶段交接数据 |
| `blender/final.blend` | 最终 Blender 工程 |
| `blender/white_model.mp4` | 最终白模视频 |
| `blender/run.json`、`blender/stages/` | 建模轮次与审核记录 |
| `render/` | 生成视频、版本记录及评估文件 |
| `harness/result.json`、`harness/runs/` | 归档结果、经验处理记录及报告 |

建议通过任务记录和界面查看各版本的最终路径；Render 的修改版本会使用独立文件名。

经验库默认位于 `assets/experience-store/`，以 JSONL 文件保存通用经验和适用于特定情境的经验。Blender 建模轮次与 Render 视频版本会检索相关经验，Harness 在任务结束后根据阶段记录决定如何更新经验库。

<a id="notes"></a>

### 使用说明

- 任务文件包含本机路径和状态信息，运行中请保留输入、资产与任务目录的位置。
- 修改 URDF 或网格后，如资产目录中已有 `blender/robot.blend`，请删除对应缓存后重新建模。
- 模型请求、视频生成和修改会使用用户自己的服务额度。具体模型能力、服务可用性及生成耗时取决于所配置的服务商。

<a id="contributors"></a>

## 👥 贡献者

| Role | Contributors |
| :--- | :--- |
| **Project Leader** | Lanyun Zhu |
| **Core Contributors** | Chenming Li, Jianhao Song, Xuanyi Liu, Zhixuan Xu |
| **Contributors** | Yichen An, Youyuan Jiang, Haotian Zhang, Ziming Wei, Enci Xie, Hongye Fang, Yueyang Li, Zixuan Zhang, Xun Jiang, Xuancheng Xu, Zengrong Lin, Baixu Ji, Chengxing Xie, Yi Guan, Zesi Shen |
| **Corresponding Authors** | Lanyun Zhu, Xing Xu, Heng Tao Shen |

<a id="citation"></a>

## 📚 引用

如果本项目对你的研究有所帮助，欢迎引用我们的论文：

```bibtex
@misc{bwmforge,
  title         = {{BWM-Forge}: Agentic World Programming for Embodied Intelligence},
  author        = {{BWM Team}},
  year          = {2026},
  eprint        = {2611.16221},
  archivePrefix = {arXiv},
  primaryClass  = {cs.RO},
  url           = {https://arxiv.org/abs/2611.16221}
}
```

<a id="license"></a>

## 📜 许可证

Copyright 2026 BWM-Forge contributors.

本项目的原创代码与文档采用 [Apache License 2.0](LICENSE)。
