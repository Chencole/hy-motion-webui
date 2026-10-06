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

给已有的 HY-Motion 后端加一个轻量网页。支持单个或批量提示词、重复次数和种子策略，查看逐项状态与日志、取消任务、预览并下载 FBX、NPZ 和原版 HTML。可在本机运行，也可由普通 Web 服务器持久保存队列、调用独立的阿里 FC GPU 函数。当前网页界面为中文；使用文档提供中英文。

**只增加可视化与任务调用层，不修改原有推理、动作数据或后处理，不添加动作矫正。** 本机界面与普通 Web 服务器不需要 Docker；可选的阿里 FC GPU 执行环境使用容器镜像，详见[云部署说明](cloud/fc-hymotion/README.md)。本地模式不会自动安装或替换模型。

## 官方来源与社区版本

| 项目 | 链接 | 与本项目的关系 |
| --- | --- | --- |
| 腾讯官方源码 | [Tencent-Hunyuan/HY-Motion-1.0](https://github.com/Tencent-Hunyuan/HY-Motion-1.0) | 模型与官方推理实现 |
| 腾讯官方权重 | [tencent/HY-Motion-1.0](https://huggingface.co/tencent/HY-Motion-1.0) | 模型下载、模型卡及许可 |
| 本社区可视化封装 | [Chencole/hy-motion-webui](https://github.com/Chencole/hy-motion-webui) | 本仓库，只提供网页与现有 CLI 的调用层 |
| 本社区版界面展示 | [Hugging Face Space](https://huggingface.co/spaces/coooooooai/hy-motion-webui) | 免费静态展示 |

**本机的 CPU CLI 和分阶段调用脚本是本地适配，并非从另一个已公开社区 CPU 仓库下载。它们尚未包含在本仓库，也没有独立公开下载链接。** 本网页不是腾讯官方产品；官方源码、权重和本社区网页封装是不同项目。当前接入依据为官方源码提交 [`4e426f5`](https://github.com/Tencent-Hunyuan/HY-Motion-1.0/tree/4e426f5a1021cbcf7f375458c37b840ee7225229)。

Hugging Face 发布的是免费静态界面展示，不提供在线算力，也不连接访问者本机。可查看单个与批量界面，但不能提交任务。实际生成需要自己的本机服务或已配置的托管服务。

## 本机启动

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和已经可用的对应本机 CPU 命令行安装（见下节）；克隆源码还需要 Git。网页依赖和模型环境分开，网页不会更改模型环境。**选择本地 CPU 模式时，仅克隆本仓库或官方模型仓库还不能运行后端，因为所需的本地 CPU 适配尚未发布。** 可选 FC GPU 模式的依赖与构建步骤见云部署说明。

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

每项任务保存在 `data/<独立 ID>/output`，保留 FBX、NPZ 和 HTML 等原始产物；可用 `MOTION_DATA_DIR` 指定持久数据目录。本地模式的历史按浏览器会话区分，服务重启会将中断任务标记为已停止，不自动重跑。托管模式的历史按登录账号保存。

预览直接使用原版 HTML，可能需要联网加载显示资源。CPU 推理的速度和内存需求取决于现有模型环境，本网页不会让模型推理自动提速。

## 批量生成

切换到“批量生成”，一行填写一个动作，空行会忽略。每条可生成 1–10 份，每批总计最多 100 项；所有项目共享时长、步数和本地 CPU 线程设置。种子可选：

- **依次递增**：从输入种子开始，按任务顺序加 1。
- **使用同一种子**：每项使用相同种子；相同描述和参数的重复项可能得到相同结果。
- **每项随机**：服务端为每项选定种子并保存，任务表会显示实际值。

整批通过校验和容量检查后才进入队列，依次执行。主内容区显示逐项状态、种子、预览/日志与下载，可取消单项或所有未完成项。批次 ZIP 只包含当前已完成项及 `batch.json` 清单，失败或取消项不会被混入；后续又有项目完成时，可重新下载更新后的包。

提交时若网络中断、未收到确认，页面重试会沿用原提交编号，找回同一任务或批次；刷新页面后也保留该编号。修改输入，或成功提交后再次点击生成，会创建新任务。API 客户端可通过 `Idempotency-Key` 请求头使用相同机制。

本地运行项可以停止进程。**已提交给 FC 的运行项无法通过关闭连接保证停止 GPU**：取消会停止后续排队，当前运行项仍会完成并保存结果，界面显示取消已请求。不要通过重复提交来重试仍在运行的云任务。

## 普通服务器托管

托管模式让普通 CPU Web 服务器保存任务和结果，再选择本机后端或远程 FC GPU。**关闭浏览器不取消任务、不停止 Web 服务**；重新打开页面并使用同一账号登录即可查看历史，其他浏览器也可访问同一账号的结果。

在服务管理器或部署平台中持久配置以下环境变量；程序不会自动读取 `.env` 文件：

| 变量 | 用途 |
| --- | --- |
| `MOTION_MODE=hosted` | 启用托管生命周期与 HTTP Basic 登录 |
| `MOTION_AUTH_USER` | 配置一个操作账号 |
| `MOTION_AUTH_PASSWORD_SHA256` | 该账号密码的 SHA-256 十六进制摘要，不是明文密码 |
| `MOTION_DATA_DIR` | Web 服务器上的持久目录，用于队列、历史和结果 |
| `MOTION_BACKEND=local` 或 `fc` | 现有 CPU CLI 或独立的阿里 FC GPU 函数 |

生成密码摘要时可在终端执行下列命令，按提示输入密码，再将输出摘要保存到服务配置：

```sh
uv run python -c "import getpass,hashlib; print(hashlib.sha256(getpass.getpass('Password: ').encode()).hexdigest())"
```

普通 Web 服务继续使用 `uv run python app.py`，通过操作系统服务管理器启动和维护。对外提供访问时由 HTTPS 反向代理转发到默认回环地址，并保留 `Host` 请求头；不要在浏览器或公开前端文件中保存云凭据。这是单个配置账号的托管入口，不包含多账号管理。

若通过 `/motion/` 子路径访问，启动时添加 `--root-path /motion`（或设置 `MOTION_ROOT_PATH=/motion`），反向代理将 `/motion/` 转发到上游根路径，并将 `/motion` 重定向至 `/motion/`。页面中的静态资源、接口、预览和下载均支持该前缀。

托管服务重启后会恢复同一计算后端尚未开始的排队项；重启时正在运行的任务标记为失败，保留“结果状态未确认”的说明，不自动重跑，以免重复执行或计费。计算后端改变时，旧队列不会在新后端上执行。

FC 需要独立的 HY-Motion GPU 函数、服务端访问凭据与结果存储；已有其他模型的函数不能直接代用。GPU 环境的容器构建、缩容设置、结果校验和部署步骤见[阿里 FC 部署说明](cloud/fc-hymotion/README.md)。本仓库的轻量测试不会构建云镜像、部署函数或调用付费推理。

## 常见问题

- **`WinError 10048` / 8770 端口已占用**：先打开上面的本机地址；若 HY-Motion 页面正常显示，说明服务已在运行，无需重复启动。要重启，关闭该服务的所有页面并等约 3 秒，或在它的启动终端按 `Ctrl+C`。若端口由其他程序使用，可运行 `uv run python app.py --port 8772`，然后打开 `http://127.0.0.1:8772`。
- **环境未就绪**：确认后端目录、CPU CLI、模型文件和原有 `.venv` 都存在。网页不自动安装模型，也不提供未发布 CPU 适配的下载。
- **Hugging Face 的生成按钮不可用**：Space 是静态界面展示，实际推理需要自己的本机或托管服务。
- **托管模式显示登录提示或 401**：确认服务配置了账号与密码摘要，并使用该账号登录；本地浏览器 cookie 不能替代托管认证。

## 检查范围

轻量测试覆盖命令参数、批量种子展开和整批提交、队列容量、会话/账号隔离、取消、文件下载、本地关闭停服、托管关闭继续和重启恢复。FC 取消测试使用无害的假执行进程，不调用云服务。另已在托管环境运行每项 1 秒、2 步的真实 GPU 小样，验证双项批次的官方 FBX/NPZ 产物与下载 ZIP 完整性，并在预览路径修复部署后用单项小样确认有效官方 HTML；详情见[云部署记录](cloud/fc-hymotion/README.md)。这属于连通性与文件交付验证，未评估动作质量、默认步数/长动作性能或账单。模型原有能力限制仍然存在。

```sh
uv run python -m unittest discover -s tests -v
```

默认仅监听 `127.0.0.1`。本机模式保留关闭页面停服行为；对外托管请使用上述托管配置与 HTTPS 反向代理。

本仓库原创界面和调用代码为 MIT。模型与既有安装环境仍遵守各自许可，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
