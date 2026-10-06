# 墨格服务器部署

独立服务在 `127.0.0.1:18010` 运行普通 FastAPI 页面，GPU 请求交给 FC。生产墨格的 Docker、数据库和 `/api/` 路由不变。

## 目录与安装

- `/opt/hymotion-webui/releases/`：每次发布的代码及项目 `.venv`；`current` 指向当前版本。
- `/var/lib/hymotion-webui/`：服务账号可写的持久任务数据。
- `/etc/hymotion-webui/app.env`：root 600 的服务配置。
- `/etc/hymotion-webui/login.txt`：root 600 的登录信息；本机副本在忽略提交的 `runtime/moge-login.txt`。
- `/usr/local/bin/uv`：通过 [uv 官方安装器](https://docs.astral.sh/uv/getting-started/installation/)安装。
- `/opt/uv/python/`：uv 集中管理的 Python；`python3.11` 命令在 `/usr/local/bin`，不替换 `python` 或 `python3`。
- `/etc/profile.d/uv-managed-python.sh`：持久配置 uv 的 Python 目录，新登录终端自动读取。

先把此目录复制到服务器私有准备目录，运行 `bash bootstrap.sh`。脚本检查并复用已有 uv，创建低权限账号、目录和 systemd 单元，但不启动或启用服务。预先安全放置的 `login.txt` 采用 `username=hymotion`、`password=...`、`url=https://mogestudio.com/motion/` 格式；脚本只把密码的 SHA256 放入服务配置。

不要把云访问密钥或登录密码打包进代码。FC 未配置时，应用仍可访问且生成必须保持禁用；后续按云部署输出填写 endpoint、OSS 和最小权限凭据。

## 发布与验证

在 Windows 运行 `deploy/moge/package.ps1`，只打包 `app.py`、`backend.py`、`remote_client.py`、`app_config.json`、`pyproject.toml`、`uv.lock`、`static/`，并输出归档路径和 SHA256。测试通过后安全复制归档，在服务器运行 `bash deploy.sh /absolute/path/release.tar.gz <sha256>`。脚本校验归档、建立隔离环境、原子切换版本并测试认证与健康接口，失败回滚当前版本。

应用成功运行后执行 `bash install-route.sh`。脚本只写主站已包含的 `extension/mogestudio.com/hymotion.conf`，发布 `/motion/`；先建立时间备份，`nginx -t` 成功才 reload，验证失败恢复原文件。后台域的配置不修改。`/motion/` 前缀由 Nginx 剥除，再由应用的 `--root-path /motion` 还原外部 URL。

服务限制为一个进程、256 MiB 内存；只有持久目录可写。用 `systemctl status hymotion-webui` 和 `journalctl -u hymotion-webui` 查看服务。配置修改后只重启 `hymotion-webui`。

## 一次性凭据导入

服务器通过现有 OpenSSL 生成 RSA4096 私钥，保留在 `/etc/hymotion-webui/credential-import-private.pem`（root 600）。只把公钥复制到本机 `runtime/moge-credential-import-public.pem`。不要复制私钥，或把 RAM 返回值打印到终端、聊天、脚本日志。

云侧在内存中把专用身份的返回值转为 UTF-8 紧凑 JSON，仅允许 `ALIBABA_CLOUD_ACCESS_KEY_ID`、`ALIBABA_CLOUD_ACCESS_KEY_SECRET`、`ALIBABA_CLOUD_SECURITY_TOKEN` 三个字段；前两项必填，长期访问密钥的第三项留空或省略。使用 [RSA-OAEP](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/rsa/#encryption)，摘要和 MGF1 都是 SHA256，`label=None`。RSA4096 的明文上限为 446 字节，输出原始二进制密文为 512 字节，不附加 Base64 包装。

把密文复制至 `/etc/hymotion-webui/credentials.enc` 并设为 root 600，然后运行：

```sh
/opt/hymotion-webui/current/.venv/bin/python /opt/hymotion-webui/deploy/import-credentials.py /etc/hymotion-webui/credentials.enc
```

脚本校验字段、格式和权限，只在内存中解密；原子写入 root 600 的 `app.env`，保留其他配置，清除可能残留的旧 STS token。成功后删除一次性私钥并写入不含凭据值的完成记录。此操作不会重启服务；端点与 OSS 参数配置完成后再单独启动新配置。任何失败都不打印密钥或底层诊断。

云端创建函数后，将实际触发器和 OSS 的五个 `MOTION_REMOTE_*` 字段保存为 JSON，通过 SSH 标准输入交给 `configure-cloud.py`。它校验字段与区域域名，再合并环境配置，不调用 FC 或重启服务。例如：

```powershell
Get-Content -LiteralPath C:\Users\cheny\Downloads\hymotion-server-settings.json -Raw | ssh moge-production '/opt/hymotion-webui/current/.venv/bin/python /opt/hymotion-webui/deploy/configure-cloud.py'
```

待权重上传、函数挂载及权限全部核验后，若部署代码与本机哈希一致，只需 `systemctl restart hymotion-webui` 读取新配置，再执行 `healthcheck.py https://mogestudio.com/motion/api/health` 和读取认证后的 `/motion/api/status`。真实 GPU 调用属于独立验证步骤，不能把配置就绪当成推理已通过。

## 更新与卸载

uv 使用 `uv self update` 更新；Python 使用 `uv python install 3.11` 安装其可用修订版，再重新发布项目环境。先检查其他项目使用情况再执行 `uv python uninstall <完整版本>`。uv 的正常卸载按官方文档移除其安装器创建的命令文件，保留仍需使用的 Python；不要删除整个共享 `/opt/uv/python`。

卸载应用时先停用 `hymotion-webui.service`，备份数据与配置，再删除此应用专属目录及单元。删除专属 Nginx extension 文件后，配置测试成功才 reload。不要操作 `/www/server/moge`、共享反代 include 或墨格容器。
