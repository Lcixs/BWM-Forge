<h1 align="center">BWM-Forge: Agentic World Programming for Embodied Intelligence</h1>

<p align="center">
  <strong>English</strong> | <a href="README_CN.md">简体中文</a>
</p>

<div align="center">

[![Project Page](https://img.shields.io/badge/%F0%9F%8C%90%20Project%20Page-Demo-00bfff)](https://bwm-forge.github.io/)
[![Paper](https://img.shields.io/static/v1?label=Paper&message=PDF&color=red&logo=arxiv)]()
[![License](https://img.shields.io/badge/License-Apache--2.0-green)](LICENSE)

</div>

<p align="center">
  <img src="examples/main.jpg" width="100%" alt="BWM-Forge overview">
</p>

BWM-Forge creates Blender scenes and animations from robot assets, reference images, and action descriptions, then transforms proxy videos into realistic videos. Its GUI connects input processing, Blender modeling, video generation, human review, and experience accumulation in a single workflow, while preserving project files, results from each round, and execution records.

## 📖 Contents

- [Overview](#overview)
- [Demo](#demo)
- [Quick Start](#quick-start)
- [User Guide](#usage)
- [Contributors](#authors)
- [Citation](#citation)
- [License](#license)

---

<a id="overview"></a>

## 🌟 Overview

- **Three paradigms**: Generate motion from spatial semantics, motion priors, or real demonstrations.
- **Robot asset library**: Support robot arms, humanoids, robot dogs, and dexterous hands, with local URDF import including mesh resources.
- **Staged modeling**: Build the scene, robot, and composed animation separately, with preview images and complete proxy videos for review.
- **Video generation and evaluation**: Generate videos from proxy videos and reference images, using local objective metrics and model-based visual evaluation to guide revisions.
- **Round tracking and experience reuse**: Preserve project files, feedback, and logs for each round; retrieve prior experience, then archive results and update the experience store when a task is complete.

### Workflow

`Input` → `Blender: Scene / Robot / Composed` → `Render` → `Harness`

<p align="center">
  <img src="examples/BWM-Forge.jpg" width="100%" alt="BWM-Forge framework">
</p>

<a id="demo"></a>

## 🎬 Demo

https://github.com/user-attachments/assets/419fc098-b061-4e2d-940e-27fd8a4a3cbf

<a id="quick-start"></a>

## 🚀 Quick Start

### Prerequisites

The project is currently intended primarily for local use on Windows, with the interface and application running on the same computer.

| Dependency | Description |
| --- | --- |
| Python | Version 3.11 or later; a separate environment is recommended |
| Blender | Requires the Blender executable and its bundled Python; configure the executable path in `config.py` |
| Python packages | See [requirements.txt](requirements.txt) |
| Model services | A service supporting the Responses API, plus a video generation service compatible with the current integration |
| Cloudflared | Temporarily publishes the proxy video during Render; the application first checks `Render/tools/cloudflared.exe` |

Model requests and video generation require internet access. The full workflow uses a temporary Cloudflare tunnel to make the selected proxy video accessible to the video generation service.

### 1. Install Dependencies

After cloning or downloading the repository, run these commands from its root directory:

```bash
conda create -n bwm python=3.11 -y
conda activate bwm
python -m pip install -r requirements.txt
```

Install Blender and confirm that it launches locally. Blender is a separate application and is not included in the Python dependencies.

### 2. Configure Models and Services

Runtime settings are centralized in [config.py](config.py). Before your first run, configure at least the following:

| Setting | Purpose |
| --- | --- |
| `OPENAI_BASE_URL` | Responses API base URL, such as `https://your-provider.example/v1` |
| `OPENAI_AUTH_FILE` | Path to the credentials file shared by the general model services |
| `INPUT_MODEL` | Model for analyzing the first frame; must support image input |
| `BLENDER_MODEL` | Codex model used for Blender modeling |
| `BLENDER_EXE` | Full path to the local Blender executable |
| `RENDER_MODEL` | Model for visual video evaluation and stage summaries; must support image input |
| `HARNESS_MODEL` | Model for organizing and updating experience |
| `SEEDANCE_BASE_URL` | Video generation service base URL |
| `SEEDANCE_MODEL` | Video generation model name |
| `SEEDANCE_AUTH_FILE` | Path to the video service credentials file |

Use model names supported by your provider. Set the general model URL to the API base URL; the application appends `/responses`, so you do not need to supply the full endpoint.

By default, Input, Blender, Render, and Harness share `OPENAI_BASE_URL` and `OPENAI_AUTH_FILE`. To configure them separately, edit each stage's `*_OPENAI_BASE_URL` and `*_OPENAI_AUTH_FILE`. Restart the application after changing the configuration.

The video service uses a separate request protocol, implemented in [Render/seedance.py](Render/seedance.py). When switching providers, check that task submission, status queries, and media input formats are compatible with this implementation.

### 3. Create Credentials Files

Using the default paths, create the following two files under `assets/api/`. You can also specify alternative paths in `config.py`.

`assets/api/auth.json`:

```json
{
  "OPENAI_API_KEY": "REPLACE_WITH_YOUR_API_KEY"
}
```

`assets/api/seedance_auth.json`:

```json
{
  "SEEDANCE_API_KEY": "REPLACE_WITH_YOUR_API_KEY"
}
```

Replace the placeholders with your own API keys.

### 4. Launch the Interface

Run this command from the repository root:

```bash
python -m gui.app
```

The application will try to open your browser. You can also visit:

```text
http://127.0.0.1:7860
```

The interface listens only on the local machine by default.

<a id="usage"></a>

## 🧭 User Guide

### 🔄 Usage Workflow

#### 1. Prepare Inputs

Select a robot type and asset, enter a task description, and choose an input mode:

| Mode | Required Inputs | Motion Source |
| --- | --- | --- |
| Spatial semantics | First-frame image, robot asset, task description | Motion inferred from the image and description |
| Motion priors | First-frame image, robot asset, task description, NPZ / Parquet trajectory, sampling rate | The supplied trajectory serves as a motion reference |
| Real demonstrations | Robot asset, task description, MP4 / MOV demonstration video | Motion generated from the demonstration video |

First-frame images can be PNG, JPG, or WebP. Click **Create task and run Input** to create a task.

To import an asset, select **Import local robot asset** and enter the URDF path on the computer running the application, along with an asset name. Use an expanded URDF and preserve its relative directory structure for `meshes/`, textures, and other resources. All resource references must resolve locally.

#### 2. Review Blender Results

Blender performs modeling in this order:

1. **Scene**: Build the scene and review its preview image.
2. **Robot**: Prepare the robot model and review its preview image.
3. **Composed**: Combine the scene and robot, generate motion, and review the complete proxy video.

Click **Accept and continue** to accept a result, or enter revision instructions, optionally attach reference images, and click **Submit revision**. Project files, scripts, previews, and feedback from each round are saved in the task directory.

#### 3. Generate and Confirm Videos

After Blender finishes, click **Continue generation** to start video generation manually. The application sends the proxy video and reference first frame to the video generation service, then evaluates the result. If the initial objective score falls below the configured threshold, the workflow automatically generates at most one corrected version before proceeding to full evaluation and human review.

You can reevaluate the video, request revisions, or click **Confirm final video** to confirm the result. Once confirmation and the stage summary are complete, Harness archives the results and updates the experience store.

#### 4. Start from a Historical Task

The interface provides access to historical tasks. **Load as new task** copies completed Blender results into a new task and returns to composed animation review, allowing you to revise an existing result.

A workspace can run only one task at a time. A task awaiting human review also occupies that execution slot.

<a id="trajectory-format"></a>

### 📐 Trajectory Format

The motion priors mode accepts two file formats:

- **NPZ**: A two-dimensional real-valued array named `action`, with shape `(T, D)`.
- **Parquet**: A column named `action`, where each row contains a real-valued vector of length `D`.

All values must be finite; NaN, Inf, and object arrays are not accepted. Enter the sampling rate explicitly in the interface.

| Action Space | Columns | Units and Conventions |
| --- | --- | --- |
| `joint` | Each column corresponds to an explicitly named independent URDF joint, in the order entered in the interface | Absolute joint positions; radians for rotation and meters for translation |
| `eef` | Seven fixed columns: `x, y, z, roll, pitch, yaw, gripper` | Absolute TCP pose in the base frame; position in meters, angles in radians, rotation order `Rz @ Ry @ Rx` |

For `joint` trajectories, robot arms, humanoids, and robot dogs all require joint names in trajectory-column order: the first name corresponds to the first column, and so on. Names must match the independently movable joints in the selected asset's URDF.

`eef` is supported only for robot arms. The gripper channel uses `0` for closed and `1` for open as reference conventions; the actual mapping depends on the asset's structure and calibration.

<a id="outputs"></a>

### 📂 Outputs and Experience Store

#### Task Outputs

Each task is stored under `output/<task_id>/`. Its main outputs include:

| Path | Contents |
| --- | --- |
| `task.json` | Task metadata and stage status |
| `input/input.json` | Input stage handoff data |
| `blender/final.blend` | Final Blender project |
| `blender/white_model.mp4` | Final proxy video |
| `blender/run.json`, `blender/stages/` | Modeling rounds and review records |
| `render/` | Generated videos, version records, and evaluation files |
| `harness/result.json`, `harness/runs/` | Archived results, experience processing records, and reports |

Use the task records and interface to locate the final files for each version. Render revisions use distinct filenames.

#### Experience Store and Prompts

The experience store defaults to `assets/experience-store/`, with JSONL files for general experience and experience applicable to specific contexts. Blender modeling rounds and Render video versions retrieve relevant experience. When a task finishes, Harness uses the stage records to determine how the store should be updated.

<a id="notes"></a>

### 📌 Usage Notes

- Task files contain local paths and status information. Keep input files, assets, and task directories in place while the application is running.
- After modifying a URDF or mesh, delete the asset's cached `blender/robot.blend`, if present, before rebuilding the model.
- Model requests, video generation, and revisions consume your own service quota. Model capabilities, service availability, and generation time depend on your configured providers.

<a id="authors"></a>

## 👥 Contributors

| Role | Contributors |
| :--- | :--- |
| **Project Leader** | Lanyun Zhu |
| **Core Contributors** | Chenming Li, Jianhao Song, Xuanyi Liu, Zhixuan Xu |
| **Contributors** | Yichen An, Youyuan Jiang, Haotian Zhang, Ziming Wei, Enci Xie, Hongye Fang, Yueyang Li, Zixuan Zhang, Xun Jiang, Xuancheng Xu, Zengrong Lin, Baixu Ji, Chengxing Xie, Yi Guan, Zesi Shen |
| **Corresponding Authors** | Lanyun Zhu, Xing Xu, Heng Tao Shen |

<a id="citation"></a>

## 📚 Citation

**arXiv: TODO** (paper URL and identifier to be added)

If this project is useful for your research, please consider citing the associated paper. The BibTeX entry below is a placeholder and will be updated when the paper is released.

```bibtex
@article{bwmforge,
  title   = {TODO},
  author  = {TODO},
  journal = {arXiv preprint arXiv:TODO},
  year    = {TODO},
  url     = {TODO}
}
```

<a id="license"></a>

## 📜 License

Copyright 2026 BWM-Forge contributors.

The project's original code and documentation are licensed under the [Apache License 2.0](LICENSE).
