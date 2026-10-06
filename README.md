---
title: HY-Motion Studio
emoji: 🌀
colorFrom: purple
colorTo: gray
sdk: static
app_file: index.html
pinned: false
license: mit
short_description: Visual controls for an existing local motion backend.
---

# HY-Motion Studio

**简体中文** | [English](README.en.md)

给已有的本机命令行版本加一个轻量网页。填写提示词、时长、种子、步数，点击生成。查看运行日志、取消任务、预览并下载 FBX、NPZ 和原版 HTML 预览。当前网页界面为中文；使用文档提供中英文。

**只增加可视化调用层。不会安装或替换模型，不修改原有推理、参数含义、动作数据或后处理。** 不含 Docker、模型下载器、重复推理代码或额外动作矫正。

## 官方来源与社区版本

| 项目 | 链接 | 与本项目的关系 |
| --- | --- | --- |
| 腾讯官方源码 | [Tencent-Hunyuan/HY-Motion-1.0](https://github.com/Tencent-Hunyuan/HY-Motion-1.0) | 模型与官方推理实现 |
| 腾讯官方权重 | [tencent/HY-Motion-1.0](https://huggingface.co/tencent/HY-Motion-1.0) | 模型下载、模型卡及许可 |
| 本社区可视化封装 | [Chencole/hy-motion-webui](https://github.com/Chencole/hy-motion-webui) | 本仓库，只提供网页与现有 CLI 的调用层 |
| 本社区版界面展示 | [Hugging Face Space](https://huggingface.co/spaces/coooooooai/hy-motion-webui) | 免费静态展示 |

**本机的 CPU CLI 和分阶段调用脚本是本地适配，并非从另一个已公开社区 CPU 仓库下载。它们尚未包含在本仓库，也没有独立公开下载链接。** 本网页不是腾讯官方产品；官方源码、权重和本社区网页封装是不同项目。当前接入依据为官方源码提交 [`4e426f5`](https://github.com/Tencent-Hunyuan/HY-Motion-1.0/tree/4e426f5a1021cbcf7f375458c37b840ee7225229)。

Hugging Face 发布的是免费静态界面展示，不提供在线算力，也不连接访问者本机。实际生成在你本机运行。

## 启动

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和已经可用的对应本机 CPU 命令行安装（见下节）；克隆源码还需要 Git。网页依赖和模型环境分开，网页不会更改模型环境。**从零安装的用户，仅克隆本仓库或官方模型仓库还不能运行当前网页后端，因为所需的本地 CPU 适配尚未发布。**

已有匹配后端、但还没有网页源码时，先在选定的项目目录执行：

```sh
git clone https://github.com/Chencole/hy-motion-webui.git HYMotion-WebUI
cd HYMotion-WebUI
uv run python app.py
```

已经安装在 `E:\dev` 的 Windows CMD 用户直接执行：

```cmd
cd /d E:\dev\HYMotion-WebUI
uv run python app.py
```

PowerShell 中第一行使用 `cd E:\dev\HYMotion-WebUI`。

打开 **http://127.0.0.1:8770**。

**关闭最后一个本地界面标签页后，约 3 秒内取消任务并停止服务。** 刷新页面不会立即停止；多个标签页要全部关闭才停止。浏览器异常退出时，约 3 分钟内通过心跳检测停止。终端也可以按 Ctrl+C。

## 已有后端位置

默认查找同级 `HYMotion` 文件夹，可设置 `MOTION_BACKEND_ROOT` 改为自己的目录。这个网页针对既有本机 CPU 命令行入口 `src/hy_motion_cpu/cli.py`，使用该目录 `.venv` 中的 Python。**它不是模型的通用安装包；仅克隆本网页仓库不会获得模型、CPU 适配或权重。** 模型尚未安装时页面会显示环境未就绪，不自动下载任何内容。

自定义已有后端位置，在启动网页前设置（路径改为实际位置；此设置只作用于当前终端）：

```cmd
set "MOTION_BACKEND_ROOT=D:\models\HYMotion"
uv run python app.py
```

PowerShell 对应命令：

```powershell
$env:MOTION_BACKEND_ROOT = 'D:\models\HYMotion'
uv run python app.py
```

该目录必须包含原有 CLI、`scripts/staged_infer.py`、`repo` 源码及权重和兼容的 `.venv`；仅改变路径不会补齐依赖。

## 使用与输出

填写简短英文动作描述，调整时长、随机种子、步数和 CPU 线程数，然后点击生成。界面提供日志、取消任务和原始结果下载，不自动改写提示词或矫正动作。

网页传入的都是原命令行参数。每次任务保存在网页项目的 `data/<独立 ID>/output`，保留 FBX、NPZ 和 HTML 等原始产物。任务历史保存在本机并按浏览器会话区分；重启会将中断任务标记为已停止，不会擅自重跑。

预览直接使用原版 HTML，可能需要联网加载显示资源。CPU 推理的速度和内存需求取决于现有模型环境，本网页不会让模型推理自动提速。

## 常见问题

- **`WinError 10048` / 8770 端口已占用**：先打开上面的本机地址；若 HY-Motion 页面正常显示，说明服务已在运行，无需重复启动。要重启，关闭该服务的所有页面并等约 3 秒，或在它的启动终端按 `Ctrl+C`。若端口由其他程序使用，可运行 `uv run python app.py --port 8772`，然后打开 `http://127.0.0.1:8772`。
- **环境未就绪**：确认后端目录、CPU CLI、模型文件和原有 `.venv` 都存在。网页不自动安装模型，也不提供未发布 CPU 适配的下载。
- **Hugging Face 的生成按钮不可用**：Space 是静态界面展示，实际推理需要本机服务。

## 检查范围

页面、命令参数传递、取消任务、文件下载、关闭停服已进行轻量测试。此次没有运行完整模型推理，不声称验证生成速度或动作质量。模型原有能力限制仍然存在。

```sh
uv run python -m unittest discover -s tests -v
```

默认仅监听 `127.0.0.1`，用于本机操作。网页不是公网多用户生产服务。

本仓库原创界面和调用代码为 MIT。模型与既有安装环境仍遵守各自许可，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
