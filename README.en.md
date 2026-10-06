# HY-Motion Studio

[简体中文](README.md) | **English**

A lightweight web interface for an existing HY-Motion backend. Generate one motion or a batch of prompts with repeats and seed strategies, inspect per-item status and logs, cancel jobs, preview results and download original FBX, NPZ and HTML outputs. Run it locally, or keep the queue on an ordinary Web server that invokes a dedicated Alibaba FC GPU function. The interface is currently in Chinese; documentation is available in Chinese and English.

The UI adds task orchestration without changing model inference, motion data or post-processing, and adds no motion correction. The local UI and ordinary Web server do not need Docker; the optional Alibaba FC GPU execution environment uses a container image. See the [cloud deployment guide](cloud/fc-hymotion/README.md). Local mode does not automatically install or replace models.

## Official sources and community version

| Project | Link | Role |
| --- | --- | --- |
| Official Tencent source | [Tencent-Hunyuan/HY-Motion-1.0](https://github.com/Tencent-Hunyuan/HY-Motion-1.0) | Model and official inference implementation |
| Official Tencent weights | [tencent/HY-Motion-1.0](https://huggingface.co/tencent/HY-Motion-1.0) | Downloads, model card and license |
| This community UI | [Chencole/hy-motion-webui](https://github.com/Chencole/hy-motion-webui) | Web interface and bridge to an existing CLI |
| Community UI showcase | [Hugging Face Space](https://huggingface.co/spaces/coooooooai/hy-motion-webui) | Free static demonstration |

**The required CPU CLI and staged inference scripts were developed locally, not obtained from a separate published community CPU repository. They are not included here and do not yet have a separate public download link.** This UI is not an official Tencent product. The current integration is based on official source commit [`4e426f5`](https://github.com/Tencent-Hunyuan/HY-Motion-1.0/tree/4e426f5a1021cbcf7f375458c37b840ee7225229).

Hugging Face hosts a static interface only. It provides no online inference and does not connect to a visitor's computer. You can inspect both single and batch modes, but cannot submit jobs there. Actual generation requires your own local or configured hosted service.

## Start locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and have the matching local CPU backend already available. Git is also required to clone the source. The UI uses its own environment and leaves the model environment unchanged. **For local CPU mode, a fresh clone of this repository or the official model repository is insufficient: the required CPU adapter has not been published.** Requirements and build steps for the optional FC GPU mode are documented separately.

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

Each job writes to `data/<unique ID>/output`, preserving original FBX, NPZ and HTML artifacts. Set `MOTION_DATA_DIR` to use a persistent data directory elsewhere. Local history is scoped to the browser session; interrupted local jobs are marked stopped on restart and are not rerun automatically. Hosted history is scoped to the configured login account.

The original HTML preview may need an internet connection for display assets. CPU performance and memory use depend on the existing backend; adding this interface does not accelerate model inference.

## Batch generation

Choose batch mode and enter one motion description per line; blank lines are ignored. Generate 1–10 copies per prompt, up to 100 items per batch. Items share duration, sampling steps and local CPU thread settings. Seed options are:

- **Increment:** start with the entered seed and add one for each item.
- **Fixed:** use the same seed for every item; repeated prompts with identical parameters may produce identical results.
- **Random:** the server chooses and saves a seed for each item, then shows the actual seed in the task table.

The entire batch must pass validation and capacity checks before entering the queue. Items run sequentially. The main page shows each item's status, seed, preview/logs and download actions. Cancel individual items or all unfinished items. A batch ZIP includes only currently completed items plus a `batch.json` manifest; failed and cancelled items are excluded. Download again after more items finish to receive an updated archive.

If submission loses its response, a retry reuses the saved submission ID and retrieves the same job or batch, including after a page refresh. Changing the input, or submitting again after confirmed success, creates new work. API clients can use the same mechanism through the `Idempotency-Key` header.

A running local process can be stopped. **Disconnecting an already submitted FC invocation does not guarantee that its GPU work stops.** Cancellation stops queued items while the current cloud item finishes and saves its results; the UI marks cancellation as requested. Avoid resubmitting cloud work that is still running.

## Host on an ordinary server

Hosted mode keeps tasks and results on an ordinary CPU Web server and uses either its existing local backend or remote FC GPU execution. **Closing the browser does not cancel tasks or stop the Web service.** Return later and log in with the same account to see history, including from another browser.

Persist the following environment variables in your service manager or hosting platform. The application does not automatically load `.env` files:

| Variable | Purpose |
| --- | --- |
| `MOTION_MODE=hosted` | Hosted lifecycle and HTTP Basic login |
| `MOTION_AUTH_USER` | One configured operator account |
| `MOTION_AUTH_PASSWORD_SHA256` | Hexadecimal SHA-256 digest of that account's password, not the plaintext password |
| `MOTION_DATA_DIR` | Persistent server directory for the queue, history and results |
| `MOTION_BACKEND=local` or `fc` | Existing CPU CLI or dedicated Alibaba FC GPU function |

Generate a password digest with the following terminal command, enter the password when prompted, then save the printed digest in the service configuration:

```sh
uv run python -c "import getpass,hashlib; print(hashlib.sha256(getpass.getpass('Password: ').encode()).hexdigest())"
```

Start the Web service with `uv run python app.py` and maintain it through the operating system's service manager. For external access, use an HTTPS reverse proxy to the default loopback address and preserve the `Host` header. Keep cloud credentials on the server, outside browser code or public frontend files. This is a single configured account, without multi-account administration.

For a `/motion/` URL prefix, add `--root-path /motion` when starting the service, or set `MOTION_ROOT_PATH=/motion`. Configure the reverse proxy to forward `/motion/` to the upstream root and redirect `/motion` to `/motion/`. Static assets, API calls, previews and downloads support the prefix.

After a hosted-service restart, queued work resumes only on the same execution backend. Items that were running at restart are marked failed with an unconfirmed-result message and are not automatically replayed, avoiding duplicate execution or charges. Changing the execution backend prevents old queued work from running on the new backend.

FC requires a dedicated HY-Motion GPU function, server credentials and result storage; an existing function for another model cannot be reused directly. See the [Alibaba FC deployment guide](cloud/fc-hymotion/README.md) for container builds, scaling settings, artifact verification and deployment. Lightweight repository tests do not build cloud images, deploy functions or invoke paid inference.

## Troubleshooting

- **`WinError 10048` / port 8770 in use:** open the local address first. If the HY-Motion page works, the service is already running. To restart, close all its tabs and wait about 3 seconds, or press `Ctrl+C` in its terminal. If another application owns the port, use `uv run python app.py --port 8772` and open `http://127.0.0.1:8772`.
- **Environment not ready:** check the backend path, CPU CLI, model files and existing `.venv`. The UI does not install models or download the unpublished CPU adapter.
- **Generation disabled on Hugging Face:** the Space is a static showcase. Actual inference requires your own local or hosted service.
- **Hosted login prompt or HTTP 401:** configure the username and password digest in the server environment, then use that account. A local browser cookie does not replace hosted authentication.

## Validation and license

Lightweight checks cover argument forwarding, batch seed expansion and atomic submission, queue capacity, session/account isolation, cancellation, downloads, local shutdown, hosted continuation after browser close, and restart recovery. FC cancellation tests use a harmless fake process without calling the cloud. A real hosted GPU test also ran a two-item batch with 1 second and 2 sampling steps per item, validating official FBX/NPZ outputs and the downloaded ZIP's integrity. After deploying the preview-path fix, one additional sample confirmed valid official HTML. See the [cloud deployment record](cloud/fc-hymotion/README.md) for the evidence and scope. This establishes limited connectivity and file delivery; motion quality, default-step or long-duration performance, and billing totals remain unverified.

```sh
uv run python -m unittest discover -s tests -v
```

The default address is `127.0.0.1`. Local mode retains shutdown when the last UI tab closes; external hosting should use the hosted configuration and HTTPS reverse proxy described above.

Original UI and command-bridge code is MIT-licensed. **Powered by Tencent HY.** Models and existing inference components retain their own licenses. See the [third-party notices](THIRD_PARTY_NOTICES.md) and the linked official sources.
