# HY-Motion FC GPU 部署

页面和队列运行在普通服务器；每项任务由服务器同步调用独立的 HY-Motion FC GPU 函数。函数提交原始官方 NPZ、FBX、元数据等到私有 OSS 后返回清单；普通服务器直接从 OSS 下载并校验 SHA-256，浏览器查看普通服务器的状态和产物。GPU 函数最小实例为 0，关闭延迟释放，全部请求结束后由 FC 自动回收实例。

**镜像和 27 个权重/配置文件已部署；1 秒、2 步的真实 GPU 双项批次已生成有效 FBX/NPZ，并已观测到该批次后实例自动归零。HTML 路径修复部署成功后，另一个同参数单项小样已返回有效官方预览，文件散列与 ZIP 完整性通过核验；预览与已在浏览器沙箱中播放的官方 HTML 字节一致。动作质量、常规负载性能及账单尚未验证。** 已有的 `kimodo-official` 是另一个模型，不能填为本项目端点。该部署不需要开关 ECS 虚拟机。OSS 存储、网络流量和普通服务器费用独立存在。

## 文件和接口

- `service.py`：HTTP 服务、请求验证、单实例互斥、超时、幂等和失败记录。启动和 `/healthz` 不导入模型；请求仍可能唤起 FC 实例，不能把外部健康轮询指向 GPU 函数。
- `worker.py`：固定官方源码版本 `4e426f5a1021cbcf7f375458c37b840ee7225229`，调用 `T2MRuntime.generate_motion`；CUDA 必需；单种子、CFG 5.0，关闭提示词改写和时长估计，无额外动作矫正或 AI 验收。官方输出先写入源码目录内真实的 `output/fc-*` 临时目录，以相对路径供官方 HTML 读取器使用，再原样复制允许的产物到任务目录；错误 HTML 会明确失败，不计为成功预览。
- `storage.py`：使用 FC 平台在 HTTP 请求头注入的执行角色临时凭证；通过 OSS API 原子登记任务，保存并读回核验产物，最后发布完成清单。结果不经 GPU 函数下载。
- `../../remote_client.py`：普通服务器客户端、官方 Python SDK 签名、无重试、OSS URL/目录/大小/散列验证。

`POST /v1/generate` 接受 `request.example.json` 的结构。`job_id` 是 1–80 个字母、数字、下划线或连字符，必须以字母或数字开头；`config` 只含 `prompt`、`seconds`、`seed`、`steps`、`threads`。提示词最多 4000 字符；时长 `(0,12]` 秒；种子 `0..4294967295`；步数 `1..200`；线程 `1..64`。默认值分别为 4 秒、42、50、8。

成功响应为 `status=complete`、`backend=tencent_hymotion_official`、`job_id`、规范化请求的 `request_sha256` 以及 `artifacts`。每项产物含 `size`、`sha256`、`object_key` 和一小时有效的 HTTPS OSS `url`。结果包括官方 `motion_000.npz`、`motion_000.fbx`、`motion_meta.json`、可能的提示词 TXT、HTML 预览、生成报告和日志。NPZ/FBX/元数据直接来自官方 runtime，没有再次重解码或修改动作。

相同 ID、相同请求且已经完成时返回原清单，并仅刷新下载 URL；不同请求、已失败或已登记但未完成的任务返回 409，不再次推理。OSS 的 `x-oss-forbid-overwrite: true` 使任务登记跨实例也不能被覆盖。最大实例仍须限制为 1，避免不同任务并行计费。429 表示繁忙；503 表示配置/挂载未就绪；504 表示子进程超时并已终止。平台终止或连接断开时，结果可能不确定；保留原 ID，检查 `request.json`、`manifest.json`、`failure.json`，不能改 ID 盲重试。

`GET /v1/jobs/{job_id}` 仅用于人工恢复检查，同样需要签名并可能唤起实例；普通页面不调用它。没有自动定时请求、预热或后台生成。所有保存工作在 HTTP 响应前完成。

## 1. 权重及持久存储

复用兼容的现有工具和私有 OSS 桶。模型前缀使用 `assets/hymotion/`，结果前缀使用 `results/hymotion/`，兼容已有 `assets/*` 只读、`results/*` 读写权限范围。不要覆盖 Kimodo 的对象。桶必须禁止公共访问，并与函数同区。

挂载后的目录结构：

```text
/mnt/hymotion-assets/ckpts/
  tencent/HY-Motion-1.0/config.yml
  tencent/HY-Motion-1.0/latest.ckpt
  Qwen3-8B/                         # 完整 config、tokenizer、index 和 5 个 safetensors 分片
  clip-vit-large-patch14/           # 完整 config、tokenizer、vocab、merges 和 model.safetensors
```

已有本地权重位于 `E:\dev\HYMotion\repo\ckpts`，来源及逐文件大小/散列在 `E:\dev\HYMotion\official-model-manifest.json`。本目录的 `assets-manifest.json` 列出全部 27 个对象、共 22,283,453,329 bytes，包含固定 revision 的官方公开 Hugging Face URL、相对本机路径、OSS 目标 key、大小与 SHA-256；本次已重新读取全部本地文件核验散列，全部 27 个对象已于 2026-10-07 05:45（北京时间）完成上传并通过 OSS HEAD 大小及 SHA-256 元数据核对；上传流也通过了本地 SHA-256 校验，没有远端全量读回。`prepare_assets_manifest.py --backend-root <existing-HYMotion> --verify-hashes` 可重新生成清单。可以从现有本地文件上传，也可在云端按清单直接从官方源逐文件传输，不需要经过普通网页服务器的磁盘。上传后核验远端对象大小和 SHA-256，不以目录存在作为就绪证据。可用正常安装、已配置身份的官方 `ossutil cp <source-directory> oss://<bucket>/assets/hymotion/ckpts/<target>/ -r` 分别上传 `tencent/HY-Motion-1.0`、`Qwen3-8B`、`clip-vit-large-patch14`；保留目录结构及许可证，不多套一层目录，不上传凭据或本地虚拟环境。

模型挂载只读；结果通过 OSS SDK 写入，无须读写 OSS 文件系统挂载。执行角色可使用 `ram-oss-policy.example.json` 和 `ram-trust-policy.example.json`，替换占位符；已有角色若已覆盖这些前缀可复用。正常服务器调用身份只需目标函数的 InvokeFunction 权限，不需要对模型桶的写权限；OSS 下载凭据封装在短期签名 URL 中。

本机上传工具 `upload_assets.py` 直接从 `--local-root` 读取现有文件，16 MiB 一片流向 OSS，不再复制 22 GB 到其他磁盘。默认只做本地 SHA-256 核验；显式加 `--upload --bucket <private-bucket>` 才访问 OSS。每文件累计大小和 SHA-256 匹配后才完成 multipart，完成请求禁止覆盖；已有对象必须大小、散列元数据及远端读回散列都吻合才能跳过。异常或取消时尝试中止当前分片上传；已完成的对象保留，重跑会核验后跳过。SDK 诊断与凭据值均不打印。

```text
uv run --group cloud python cloud/fc-hymotion/upload_assets.py --local-root E:\dev\HYMotion --max-files 1
uv run --group cloud python cloud/fc-hymotion/upload_assets.py --local-root E:\dev\HYMotion --upload --bucket <private-bucket> --region cn-hangzhou
```

上传环境使用项目 cloud 依赖组中的 `oss2==2.19.1`；凭据来自现有 `ALIBABA_CLOUD_*`（或 `OSS_*`）环境变量，或已安装的官方 credentials SDK 的正常 provider chain，不手动解析或显示 CLI 密钥配置。电脑使用公网 OSS 端点，只有在同地域阿里云网络中才使用 `--internal`。该工具不会创建桶、修改 ACL 或调用 FC。

也支持范围更小的签名上传计划：在有现成环境凭据的 CloudShell 中运行兼容 Python 3.6 的 `issue_upload_plan.py --manifest assets-manifest.json --bucket <bucket> --output <private-plan.json>`。它仅签发每个目标对象的 PUT/HEAD 链接，不调用 OSS API、不上传文件，计划以 0600 权限保存，默认一小时有效。把这个临时计划安全传到本机即可，不传管理凭据。

```text
uv run python cloud/fc-hymotion/upload_assets.py --local-root E:\dev\HYMotion --signed-plan <private-plan.json> --bucket <bucket> --region cn-hangzhou --upload --workers 2
```

签名模式只依赖已有 `httpx`，不加载本机云凭据或 OSS SDK；只允许指定桶和对象的 HTTPS 链接，并校验签入的 Content-Type、SHA-256 元数据与禁止覆盖头。所有对象小于 5 GB，使用单次流式 PUT；上传前先核验文件，流中再次计数和计算 SHA-256，最后一块只有全部匹配后才发送。最多两路并行，不跟随重定向、不自动重传大文件；每次完成后 HEAD 核对大小与 SHA-256 元数据。这里核验的是本地实际上传字节加远端元数据，没有远端全量读回散列，不应宣称做了独立远端字节校验。已有匹配对象跳过；异常对象拒绝覆盖；响应不明时保留原计划/清单，重新 HEAD 核查。签名计划不要放进仓库、前端或公开日志，过期后重新签发。

STS 签名的实际有效期取 URL 与临时凭据寿命的较短者；计划中的一小时 URL 期限不会延长原有 STS 凭据。若新旧计划都在声明期限前返回 403，先刷新 CloudShell 会话/临时凭据，再签发新计划。恢复时先 HEAD 核对对象；上传后的确认请求失败，不等于对象没有写入。依据见 [OSS 临时授权有效期](https://help.aliyun.com/zh/oss/developer-reference/authorized-access-1)。

## 2. 构建独立推理镜像

构建上下文必须是本目录；`.dockerignore` 只允许服务代码进入镜像。镜像安装官方 CUDA PyTorch 环境、固定提交的官方依赖、Git LFS 运行资产与官方 OSS SDK `oss2==2.19.1`。权重只从持久挂载读取，不进入镜像或在运行时在线下载。

```powershell
docker build --platform linux/amd64 -t hymotion-fc:official-runtime-20261007-preview .
```

上述命令需要在本目录执行。本次已复用现有 Docker Desktop 构建 `hymotion-fc:official-runtime-20261007-preview`，大小 11,049,108,572 bytes。无网络容器检查确认 Python 3.11.10、PyTorch 2.5.1+cu124、OSS SDK 2.19.1、官方 runtime 导入及 FBX 转换器初始化正常；另以既有 NPZ 验证修复后的真实 worker 输出路径及官方 HTML 生成，未挂载 GPU、初始化 CUDA 或重新运行模型。基础镜像原有 Ninja 包的平台元数据不兼容，已在容器内通过正规 PyPI `ninja==1.11.1.4` 修复，`pip check` 通过；未更改本机模型环境。

镜像索引及单平台 digest、检查范围在 `source-manifest.json`；构建与发布日志保存在忽略提交的 `runtime/`。本次已通过正常 ACR 登录/推送发布到私有仓库，实际发布的 Linux AMD64 manifest 为 `sha256:cc3a72603e7605785f3f1ed5480a2a12bdf5a2f62ca440b7099a959b40c7b576`。后续发布如果遇到 ACR 不接收带 attestation 的索引，可沿用 `docker push --platform linux/amd64`，并记录实际发布 digest。构建、发布和函数创建不等于已经验证模型推理。

## 3. 配置 FC

以 `create-function.example.json` 创建独立 `hymotion-official` 函数；替换镜像、RAM 角色和 OSS 桶占位符。示例使用杭州 Ada.1 48 GB、8 vCPU、64 GB 内存、10 GB 临时盘。官方标准模型文档给出最低显存 26 GB，实际上限需真实负载测试，不能据此保证所有 12 秒/200 步任务都在资源与时限内成功。

部署配置顺序：

本次函数与签名入口已经创建并读回。下列步骤保留为重新部署时的操作说明；账户、桶和私有镜像的具体定位仅保存在受限的 `runtime/` 运行记录中，不写入公开模板。

模板沿用实际部署的 `disableInjectCredentials: "Env"`：禁用执行角色凭据的环境变量注入，保留平台对请求头的临时凭据注入。服务端通过请求头访问 OSS；不要设为 `Request` 或 `All`，否则会阻断这条认证方式。

1. 创建只读模型挂载与函数；初始 `HYMOTION_TRUST_FC_AUTH=0`，业务请求保持拒绝。
2. 应用 `concurrency.example.json`：`reservedConcurrency=1`，同时函数 `instanceConcurrency=1`。
3. 应用 `scaling.example.json`：`minInstances=0`，仅按需伸缩，无定时/指标规则、无常驻资源池。
4. 控制台高级配置中确认“延时释放弹性实例”为关闭；保持会话亲和关闭，无 initializer、预热请求或外部健康轮询。模板没有猜测未明确记录的 `idleTimeout=0` 含义，创建模板本身不替代此项云端核验。
5. 使用 `http-trigger.example.json` 创建 `authType=function` 的签名 HTTP 触发器；确认所有外部入口都经过签名网关后，在原有完整环境变量表中将 `HYMOTION_TRUST_FC_AUTH` 改为 `1`。不能只更新一个键而覆盖其他配置。
6. 部署后通过 `GetFunction` 同时确认 `state=Active` 和 `lastUpdateStatus=Successful`，再核对部署镜像、角色、私有挂载、并发、最小实例及关闭延迟释放，最后才提交付费生成验证。`InProgress` 期间配置中的 resolved image digest 可能已变化，请求仍会执行更新前的代码。依据见 [FC 自定义容器状态](https://help.aliyun.com/en/functioncompute/states-of-custom-container-functions)。不要为了检查配置调用 `/healthz`；使用控制面配置/实例页面。

FC 超时为 1800 秒、worker 为 1740 秒、服务器客户端为 1820 秒；普通服务器代理不应直接承接这条浏览器长请求，页面请求由持久队列快速返回。若调整超时，三个配置需留出产物保存和传输余量。

模板的 `customContainerConfig.healthCheckConfig` 使用 `/healthz`，仅供 FC 平台检查已启动的容器，不加载模型；它与从普通服务器持续调用公开函数 URL 的外部轮询不同。字段依据见 [FC 自定义健康检查](https://help.aliyun.com/zh/functioncompute/api-fc-2023-03-30-struct-customhealthcheckconfig)。

“不用时不计 GPU 费”依赖弹性实例真正回收。最小实例 0 与关闭延迟释放使 FC 在请求结束后回收，不承诺响应返回的同一毫秒完成回收；不要用 kill/关机命令替代平台生命周期。首次生成后核对实例数 0 和账单。参考 [FC 实例类型](https://help.aliyun.com/zh/functioncompute/instance-types-and-specifications)、[延迟释放与计费](https://help.aliyun.com/zh/functioncompute/configure-elastic-instance-delayed-release)。

## 4. 普通服务器接入

按项目 `pyproject.toml` / `uv.lock` 安装依赖，客户端使用 `alibabacloud-openapi-util==0.2.4` 和 `alibabacloud-credentials==1.0.12`。通过服务器的服务环境/密钥管理系统设置 `server.env.example` 中的变量。端点只接受指定区域的 HTTPS `*.fcapp.run` 根地址；OSS 下载只接受指定桶的标准区域域名。

不要把 AccessKey、Secret、STS token 或 OSS 签名 URL放入前端、仓库、shell 历史或日志。客户端使用显式环境凭据，不读取用户 CLI profile；STS token 不会由本程序自动刷新，运行服务应提供有效凭据及正常的外部刷新机制。只检查变量存在性，不打印值。

```powershell
uv run python remote_client.py --check
```

在 WebUI 项目根目录运行。`--check` 和页面状态检查只检查本地配置，不访问云或启动 GPU。单任务 CLI（会付费，部署核验后再执行）为：

```text
uv run python remote_client.py --job-id <unique-id> --request <config.json> --output <new-output-directory>
```

这里的 `config.json` 仅包含 `prompt/seconds/seed/steps/threads`，CLI 自动封装 job_id。正常网页批量任务直接使用此入口，不需手动执行。`--output` 必须不存在；全部 OSS 产物验证完成后才发布该目录，`remote_manifest.json` 已去掉签名 URL。

如果已收到云端完成清单但下载失败，客户端在任务目录保留权限受限的 `remote-recovery.json`（含短期签名 URL，仅服务器可见）。使用同一条 CLI 并增加 `--recover <jobdir>/remote-recovery.json` 仅恢复 OSS 下载，不调用 FC；仍须原 job-id 和原 config，以校验请求散列。不要用新 ID 再生成。签名过期后应通过 OSS 控制面或人工签名任务查询恢复，正常任务路径不会为了下载再次唤起 GPU。

官方认证依据：[FC HTTP 签名示例](https://help.aliyun.com/zh/functioncompute/configure-signature-authentication-for-http-triggers)、[自定义容器执行角色临时凭证](https://help.aliyun.com/zh/functioncompute/grant-function-compute-permissions-to-access-other-alibaba-cloud-services)。

## 验证范围

```text
uv run python -m unittest discover -s tests -p test_remote_client.py
uv run python -m unittest discover -s cloud/fc-hymotion -p test_service.py
uv run python -m unittest discover -s cloud/fc-hymotion -p test_upload_assets.py
uv run python -m unittest discover -s cloud/fc-hymotion -p test_worker.py
```

上述轻量测试使用 fake worker、fake OSS 与 fake HTTP，验证幂等、防重复执行、超时/失败状态、路径及下载校验，不加载模型或调用云。新增 3 项 worker 回归测试覆盖源码内相对输出路径、原始产物复制、错误 HTML 拒绝和临时目录清理；本目录共 31 项测试通过。

另已完成一次真实双项 GPU 连接验证：每项 1 秒、2 步、8 线程，种子分别为 42 和 43。两项均报告 `cuda:0`、Tencent HY-Motion-1.0 standard、PyTorch 2.5.1+cu124，生成 30 帧/30 FPS 的官方 FBX/NPZ；14 个产物的 SHA-256 与远端清单一致，批次 ZIP（31,192,003 bytes）通过 CRC 与散列检查。

| 样本 | worker 耗时 | 普通服务器任务执行耗时 |
|---|---:|---:|
| 种子 42 | 55.663 秒 | 96.452 秒 |
| 种子 43 | 19.521 秒 | 27.060 秒 |

worker 计时包括模型构造、生成与 HTML 写入；任务执行计时还包含云请求、产物持久化和下载。上述数字不是冷/热启动或常规性能基准。此次仅验证连接与文件交付，没有动作质量验收，也没有默认步数、长动作或持续负载测试；报告没有记录 GPU 商业型号。

原始 HTML 预览暴露了绝对路径查找问题；修复后用两个既有 NPZ 在断网、无 GPU 的容器中生成了官方 HTML，均嵌入 30 帧，产物分别为 148,377 和 148,772 bytes，其中一个预览已在与生产相同的 `allow-scripts` iframe 沙箱中确认人偶播放。更新期间运行旧代码的探测不计为修复验证。

2026-10-07 06:18:25（北京时间）控制面确认修复镜像为 `state=Active`、`lastUpdateStatus=Successful` 后，又完成一个 1 秒、2 步、种子 42 的云端小样。它报告相同官方模型、`cuda:0`、30 帧/30 FPS，worker 耗时 47.818 秒，普通服务器任务执行耗时 69.702 秒；7 个产物散列均匹配远端清单，ZIP（15,633,751 bytes）通过完整性检查。云端 `preview.html` 为 148,377 bytes，无错误页面或未替换的数据占位符，SHA-256 为 `2634dae50e903606c36e2214e24441f953a94c55f9dbc8d503738a70a7676976`，与已通过浏览器验证的离线官方 HTML 完全一致。这确认了修复镜像的云端预览路径，仍不构成动作质量或常规性能验收。

首次批次后的控制面查询（`withAllActive=true`）在 2026-10-07 05:51:46（北京时间）仍观察到 1 个实例，05:53:14 观察到 0 个实例和空列表。这证明随后完成回收，不是精确回收时刻或即时回收保证；没有核对账单金额。详细任务报告、散列、图像及控制面证据保存在忽略提交的 `runtime/`，公开文档不含账户、桶或私有镜像定位。
