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

给已有的本机命令行版本加一个轻量网页。填写提示词、时长、种子、步数，点击生成。查看运行日志、取消任务、预览并下载FBX、NPZ 和原版 HTML 预览。

**只增加可视化调用层。不会安装或替换模型，不修改原有推理、参数含义、动作数据或后处理。** 不含 Docker、模型下载器、重复推理代码或额外动作矫正。

[GitHub 源码](https://github.com/Chencole/hy-motion-webui) · [Hugging Face 界面展示](https://huggingface.co/spaces/coooooooai/hy-motion-webui) · [模型原项目](https://github.com/Tencent-Hunyuan/HY-Motion-1.0)

Hugging Face 发布的是免费静态界面展示，不提供在线算力，也不连接访问者本机。实际生成在你本机运行。

## 启动

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和已经可用的对应本机命令行安装。网页依赖和模型环境分开，网页不会更改模型环境。

Windows CMD：

```cmd
cd /d E:\dev\HYMotion-WebUI
uv run python app.py
```

PowerShell 中第一行使用 `cd E:\dev\HYMotion-WebUI`。

打开 **http://127.0.0.1:8770**。

**关闭最后一个本地界面标签页后，约 3 秒内取消任务并停止服务。** 刷新页面不会立即停止；多个标签页要全部关闭才停止。浏览器异常退出时，约 3 分钟内通过心跳检测停止。终端也可以按 Ctrl+C。

## 已有后端位置

默认查找同级 `HYMotion` 文件夹，可设置 `MOTION_BACKEND_ROOT` 改为自己的目录。这个网页针对既有本机 CPU 命令行入口 `src/hy_motion_cpu/cli.py`，使用该目录 `.venv` 中的 Python。**它不是模型的通用安装包；仅克隆本网页仓库不会获得模型、CPU 适配或权重。** 模型尚未安装时页面会显示环境未就绪，不自动下载任何内容。

网页传入的都是原命令行参数。每次任务保存在 `data/<独立 ID>/output`，保留原始产物，ZIP 不包括上传的源视频。任务历史保存在本机并按浏览器会话区分；重启会将中断任务标记为已停止，不会擅自重跑。

预览直接使用原版输出。HY 的原版 HTML 可能需要联网加载显示资源；GEM-X 原版 MP4 若当前浏览器不支持编码，可下载后播放，不进行额外转码或动作处理。

## 检查范围

页面、命令参数传递、取消任务、文件下载、关闭停服已进行轻量测试。此次没有运行完整模型推理，不声称验证生成速度或动作质量。模型原有能力限制仍然存在。

```sh
uv run python -m unittest discover -s tests -v
```

默认仅监听 `127.0.0.1`，用于本机操作。网页不是公网多用户生产服务。

本仓库原创界面和调用代码为 MIT。模型与既有安装环境仍遵守各自许可，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
