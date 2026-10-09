# Windows NVIDIA GPU 部署

这个社区适配保留现有可视化、批量任务及 FBX、NPZ、HTML 下载。文本编码在 CPU 上运行 Qwen3-8B BF16 和 CLIP；编码进程退出后，动作进程才将官方标准 1B 模型和文本特征放到 CUDA。默认动作精度仍为 FP32，官方模型、50 步采样、CFG 5 和解码平滑不变。

这不是官方的全 GPU 运行方式。腾讯官方说明标准/Lite 常规运行分别需要至少 26/24 GB 显存。分阶段方式供 8 GB 显卡实测使用，不能仅凭界面显示环境就绪认定推理成功。文本阶段仍会较慢，需要充足内存；8 GB 卡一次只运行一个模型。

## 安装目录与来源

- 网页：`E:\dev\HYMotion-WebUI`
- 模型后端、独立 `.venv`、缓存、权重：`E:\dev\HYMotion`
- Python：由正规安装的 uv 集中管理，本部署使用 Python 3.11.14。
- 官方源码：<https://github.com/Tencent-Hunyuan/HY-Motion-1.0>，提交 `4e426f5a1021cbcf7f375458c37b840ee7225229`。
- PyTorch 2.5.1+cu124 / torchvision 0.20.1+cu124 使用官方 CUDA 12.4 wheel 索引，不依赖复制其他项目的环境，不要求单独安装 CUDA 编译工具包。
- 精确直接依赖和完整传递依赖分别在 `backend/pyproject.toml` 和 `backend/uv.lock`，WebUI 使用其自己的项目锁文件。

在全新目标上克隆网页及固定版本的官方源码：

```powershell
git clone https://github.com/Chencole/hy-motion-webui.git E:\dev\HYMotion-WebUI
git clone https://github.com/Tencent-Hunyuan/HY-Motion-1.0.git E:\dev\HYMotion\repo
git -C E:\dev\HYMotion\repo checkout 4e426f5a1021cbcf7f375458c37b840ee7225229
git -C E:\dev\HYMotion\repo lfs pull
```

首次安装有两种权重来源：

1. 按审核过的模型清单，将已经校验的官方权重从本机单向上传到同名路径。不要复制 `.venv`、`python`、`cache`、令牌、日志、生成结果或用户素材。
2. 直接从官方 Hugging Face 固定版本下载，安装命令添加 `-DownloadModels`。下载脚本核对文件尺寸和 LFS SHA-256，生成 `official-model-manifest.json`。

在目标 PowerShell 执行：

```powershell
cd E:\dev\HYMotion-WebUI
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows-gpu\Install.ps1
# 若尚未上传权重，上一行末尾添加 -DownloadModels。
```

脚本拒绝覆盖已有的其他后端虚拟环境；它只同步本适配源码及该项目依赖，不更改驱动、系统默认 Python 或开机启动。安装后重新打开普通终端使用 uv；项目启动器明确选用 uv 管理的 Python 3.11。

可用 `-BackendRoot D:\models\HYMotion` 选择专用后端目录。安装成功后路径保存在网页项目的 `.gpu-settings.json`，双击启动器也会读取；该文件不进入 Git。显式设置的 `MOTION_BACKEND_ROOT` 优先于此设置，不更改全局环境变量。

## 真实 GPU 验证

关闭其他显存占用较大的模型，先运行安装检查，再运行一个 1 秒、2 步的管线小样：

```powershell
cd E:\dev\HYMotion
uv run --python 3.11 --frozen hymotion --check --device cuda:0
uv run --python 3.11 --frozen hymotion --prompt "A person walks forward." --seconds 1 --steps 2 --seed 42 --device cuda:0 --output outputs\gpu-smoke
```

输出目录必须是新的。通过条件：进程退出码 0；`run_metadata.json` 的 `status=completed`、`model_device=cuda:0`、三个 `sampling_features` 都在 CUDA，且记录 `cuda_peak_allocated_bytes`；存在非空 FBX、NPZ 和 HTML，NPZ 动作帧数正确且数值有限。`requested_motion_device` 记录请求，`device` 和 `model_device` 在实际成功迁移后才记录。安装检查报告会在每次检查时更新，失败不会留下旧的成功结论。2 步只验证管线，不能评价动作质量。继续以 4 秒、50 步生成检查日常默认参数，不应将短测试性能称为完整性能。

双击 `Start-GPU.cmd` 打开服务，浏览器访问 `http://127.0.0.1:8770`。网页显示“GPU 动作 · CPU 文本”，原批量、预览和下载界面保留。现有本机关闭最后页面即停服行为也保留。

需要局域网访问时，部署者可给启动器添加 `--host 192.168.1.8`，并在目标防火墙只允许受信任客户端访问 TCP 8770；本脚本不自动开放端口。需要长期托管生命周期/登录时沿用主文档的 hosted 配置。不要把本机模式直接暴露到公网。

## 更新与卸载

保留 `data`、后端 `outputs` 和模型清单。网页更新使用正常 Git 流程，先处理自己的未提交修改，再同步锁定依赖。适配更新重新运行此安装脚本；上游模型代码固定提交，升级上游应先在独立目录实测。uv 自身使用 `uv self update`，集中 Python 使用 `uv python install` / `uv python uninstall` 管理。

卸载这个应用先停止服务，再删除这两个专用项目目录；生成结果请先备份。无需卸载共享 uv/Python/NVIDIA 驱动，不自动删除其他项目环境或缓存。

来源：腾讯 [官方运行说明](https://github.com/Tencent-Hunyuan/HY-Motion-1.0)、[权重说明](https://github.com/Tencent-Hunyuan/HY-Motion-1.0/blob/master/ckpts/README.md)、PyTorch [2.5.1 CUDA 安装命令](https://pytorch.org/get-started/previous-versions/)。
