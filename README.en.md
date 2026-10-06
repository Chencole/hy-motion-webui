# HY-Motion Studio

[简体中文](README.md) | **English**

A lightweight web interface for an existing local HY-Motion command-line installation. Enter a prompt, duration, seed and sampling steps; generate, inspect logs, cancel jobs, preview results and download the original FBX, NPZ and HTML outputs. The interface is currently in Chinese; documentation is available in Chinese and English.

The UI only calls the existing backend. It does not install or replace models, change inference or post-processing, or add motion correction. No Docker or model downloader is included.

## Official sources and community version

| Project | Link | Role |
| --- | --- | --- |
| Official Tencent source | [Tencent-Hunyuan/HY-Motion-1.0](https://github.com/Tencent-Hunyuan/HY-Motion-1.0) | Model and official inference implementation |
| Official Tencent weights | [tencent/HY-Motion-1.0](https://huggingface.co/tencent/HY-Motion-1.0) | Downloads, model card and license |
| This community UI | [Chencole/hy-motion-webui](https://github.com/Chencole/hy-motion-webui) | Web interface and bridge to an existing CLI |
| Community UI showcase | [Hugging Face Space](https://huggingface.co/spaces/coooooooai/hy-motion-webui) | Free static demonstration |

**The required CPU CLI and staged inference scripts were developed locally, not obtained from a separate published community CPU repository. They are not included here and do not yet have a separate public download link.** This UI is not an official Tencent product. The current integration is based on official source commit [`4e426f5`](https://github.com/Tencent-Hunyuan/HY-Motion-1.0/tree/4e426f5a1021cbcf7f375458c37b840ee7225229).

Hugging Face hosts a static interface only. It provides no online inference and does not connect to a visitor's computer. Generation runs locally.

## Start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and have the matching local CPU backend already available. Git is also required to clone the source. The UI uses its own environment and leaves the model environment unchanged. **A fresh clone of this repository or the official model repository is insufficient: the required local CPU adapter has not been published.**

With a matching backend already installed, clone the UI into your chosen project directory:

```sh
git clone https://github.com/Chencole/hy-motion-webui.git HYMotion-WebUI
cd HYMotion-WebUI
uv run python app.py
```

For an existing installation under `E:\dev`, use Windows CMD:

```cmd
cd /d E:\dev\HYMotion-WebUI
uv run python app.py
```

In PowerShell, use `cd E:\dev\HYMotion-WebUI` for the first line.

Open **http://127.0.0.1:8770**.

Closing the last local UI tab cancels jobs and stops the service after about 3 seconds. Refreshing has a short grace period; if multiple tabs are open, close all of them. A lost browser connection also triggers shutdown, with a heartbeat fallback of about 3 minutes. You can stop the service with `Ctrl+C` in its terminal.

## Existing backend location

By default, the UI looks for a sibling `HYMotion` directory. It calls `src/hy_motion_cpu/cli.py` with that directory's `.venv` Python. This is not a general-purpose model installer. Missing model files produce an environment-not-ready status rather than an automatic download.

To use a different existing backend, set its real path before starting the UI. These examples affect only the current terminal.

Windows CMD:

```cmd
set "MOTION_BACKEND_ROOT=D:\models\HYMotion"
uv run python app.py
```

PowerShell:

```powershell
$env:MOTION_BACKEND_ROOT = 'D:\models\HYMotion'
uv run python app.py
```

The backend directory must already contain the CLI, `scripts/staged_infer.py`, the `repo` source and weights, and a compatible `.venv`. Setting a path does not install these components.

## Usage and outputs

Enter a short English motion description and choose duration, seed, sampling steps and CPU threads. Click generate to start. Logs, cancellation and original output downloads are available. The UI does not rewrite prompts or correct generated motion.

Each job writes to `data/<unique ID>/output` inside the UI project, preserving original FBX, NPZ and HTML artifacts. History is stored locally and scoped to the browser session. Interrupted jobs are marked stopped on restart and are not rerun automatically.

The original HTML preview may need an internet connection for display assets. CPU performance and memory use depend on the existing backend; adding this interface does not accelerate model inference.

## Troubleshooting

- **`WinError 10048` / port 8770 in use:** open the local address first. If the HY-Motion page works, the service is already running. To restart, close all its tabs and wait about 3 seconds, or press `Ctrl+C` in its terminal. If another application owns the port, use `uv run python app.py --port 8772` and open `http://127.0.0.1:8772`.
- **Environment not ready:** check the backend path, CPU CLI, model files and existing `.venv`. The UI does not install models or download the unpublished CPU adapter.
- **Generation disabled on Hugging Face:** the Space is a static showcase. Actual inference requires the local service.

## Validation and license

Lightweight checks cover UI behavior, argument forwarding, cancellation, downloads and shutdown when the UI closes. Full model inference has not been tested for this release; generation speed and motion quality remain unverified.

```sh
uv run python -m unittest discover -s tests -v
```

The default address is `127.0.0.1`, for local use. This is not a public multi-user production service.

Original UI and command-bridge code is MIT-licensed. **Powered by Tencent HY.** Models and existing inference components retain their own licenses. See the [third-party notices](THIRD_PARTY_NOTICES.md) and the linked official sources.
