"""Small web interface. Model runtimes stay in separate, existing environments."""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import subprocess
import threading
import time
import uuid
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "app_config.json").read_text(encoding="utf-8"))
DATA = Path(os.environ.get("MOTION_DATA_DIR", ROOT / "data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
SHOWCASE = os.environ.get("MOTION_SHOWCASE", "0") == "1"
MAX_UPLOAD = 256 * 1024 * 1024
ARTIFACT_EXTENSIONS = {".fbx", ".npz", ".npy", ".pt", ".html", ".mp4", ".json", ".log", ".bvh"}
jobs: dict[str, dict] = {}
pending: queue.Queue = queue.Queue(maxsize=200)
batches: dict[str, dict] = {}
lock = threading.RLock()
stopping = threading.Event()
worker_thread = None
clients: dict[str, dict] = {}
client_seen = False
CLIENT_TIMEOUT = 180
CLOSE_GRACE = 3
MAX_BATCH_ITEMS = 100
SUBMISSION_FIELDS = {'_submission_key', '_submission_sha256'}


def hosted():
    return os.environ.get('MOTION_MODE', 'local') == 'hosted'


def execution_backend():
    return os.environ.get('MOTION_BACKEND', 'local')


def authenticated_user(headers):
    """Hosted access requires a server-held password. Nothing is embedded in JS."""
    username = os.environ.get('MOTION_AUTH_USER', '')
    digest = os.environ.get('MOTION_AUTH_PASSWORD_SHA256', '')
    if not username or not re.fullmatch(r'[a-f0-9]{64}', digest):
        return None
    try:
        scheme, value = headers.get('authorization', '').split(' ', 1)
        user, password = base64.b64decode(value, validate=True).decode('utf-8').split(':', 1)
        valid = secrets.compare_digest(user.encode(), username.encode())
        valid &= secrets.compare_digest(hashlib.sha256(password.encode()).hexdigest(), digest)
        return user if scheme.lower() == 'basic' and valid else None
    except (ValueError, UnicodeError):
        return None


def stop_task(task, message='任务已取消'):
    """A disconnected HTTP client cannot stop an already submitted FC invocation."""
    with lock:
        if task['status'] not in {'queued', 'running'}:
            return None
        if (task['status'] == 'running' and task.get('execution_backend') == 'fc'
                and task.get('_process') is not None):
            task.update(cancel_requested=True, message='已停止后续排队；当前云任务已提交，将等待完成并保存结果。')
            persist(task)
            return None
        task.update(status='cancelled', message=message, finished_at=time.time())
        persist(task)
        return task.get('_process')


def cancel_owned(owner=None):
    with lock:
        active = [j for j in jobs.values() if j["status"] in {"queued", "running"} and (owner is None or j["_owner"] == owner)]
        processes = [stop_task(task, '界面已关闭，任务已停止。') for task in active]
    for process in processes:
        kill_tree(process)


async def watch_clients(application):
    while not stopping.is_set():
        await asyncio.sleep(1)
        now = time.monotonic()
        with lock:
            expired = [key for key, item in clients.items() if (not item.get("connected") and now - item["seen"] > CLIENT_TIMEOUT) or (item.get("closed") and now - item["closed"] > CLOSE_GRACE)]
            owners = {clients.pop(key)["owner"] for key in expired}
            remaining_owners = {item["owner"] for item in clients.values()}
        if not hosted():
            for owner in owners - remaining_owners:
                await asyncio.to_thread(cancel_owned, owner)
        if not hosted() and client_seen and not clients and getattr(application.state, "exit_on_close", False):
            await asyncio.to_thread(cancel_owned)
            callback = getattr(application.state, "shutdown_callback", None)
            if callback:
                callback()
            return


def persist(job):
    public = {k: v for k, v in job.items() if not k.startswith("_") or k in SUBMISSION_FIELDS}
    path = Path(job["_dir"]) / "task.json"
    atomic_json(path, public)
    (Path(job["_dir"]) / ".owner").write_text(job["_owner"], encoding="ascii")


def atomic_json(path, content):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def restore_jobs():
    for path in (DATA / 'batches').glob('*.json'):
        try:
            batch = json.loads(path.read_text(encoding='utf-8'))
            if (not re.fullmatch(r'[a-f0-9]{32}', batch['id']) or path.stem != batch['id']
                    or not re.fullmatch(r'[a-f0-9]{64}', batch['_owner'])):
                continue
            batches[batch['id']] = batch
        except (OSError, ValueError, KeyError):
            continue
    restored = []
    for directory in DATA.iterdir():
        if not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
            continue
        try:
            task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
            owner = (directory / ".owner").read_text(encoding="ascii")
            if not re.fullmatch(r"[a-f0-9]{64}", owner) or task["id"] != directory.name:
                continue
            if task.get('batch_id'):
                batch = batches.get(task['batch_id'])
                if not batch or batch['_owner'] != owner or task['id'] not in batch['job_ids']:
                    continue  # A batch is runnable only after its manifest was committed.
            task.update(_owner=owner, _dir=str(directory))
            if hosted() and task['status'] == 'queued' and task.get('execution_backend', 'local') == execution_backend():
                restored.append(task)
            elif hosted() and task['status'] == 'running':
                task.update(status='failed', message='服务重启时此任务正在运行，结果状态尚未确认；为避免重复计费，未自动重跑。', finished_at=time.time())
                persist(task)
            elif task["status"] in {"queued", "running"}:
                task.update(status="cancelled", message="服务已重启，此任务已停止，请重新创建。", finished_at=time.time())
                persist(task)
            jobs[task["id"]] = task
        except (OSError, ValueError, KeyError):
            continue
    for task in sorted(restored, key=lambda item: item['created_at']):
        try:
            pending.put_nowait(task)
        except queue.Full:
            task.update(status='failed', message='恢复队列已满，任务未提交。', finished_at=time.time())
            persist(task)


def log_line(job, text):
    # Child output is plain text, never interpreted as HTML or a shell command.
    line = text.rstrip()[:4000]
    with lock:
        job["log"].append(line)
        job["log"] = job["log"][-250:]
        job["message"] = line or job["message"]
    with (Path(job["_dir"]) / "task.log").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)


def execute(job):
    with lock:
        if job["status"] == "cancelled":
            return
        job.update(status="running", started_at=time.time(), message="正在启动模型进程…")
        persist(job)
    try:
        if job.get('execution_backend', 'local') != execution_backend():
            raise RuntimeError('任务所用计算后端已改变，未执行。')
        if '_command' not in job:
            import backend
            job['_command'] = backend.resume_command(Path(job['_dir']), job['config'])
        command = job["_command"]
        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        # No shell. User prompt and filenames only become argument values.
        with lock:
            if job["status"] == "cancelled":
                return
            proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", **options)
            job["_process"] = proc
        for line in proc.stdout:
            log_line(job, line)
        code = proc.wait()
        proc.stdout.close()
        with lock:
            if job["status"] != "cancelled":
                job["status"] = "complete" if code == 0 else "failed"
                job["message"] = "已完成，可以预览或下载结果。" if code == 0 else f"模型进程退出（{code}），请查看日志。"
                job["exit_code"] = code
    except Exception as exc:
        log_line(job, f"{type(exc).__name__}: {exc}")
        with lock:
            if job["status"] != "cancelled":
                job.update(status="failed", message=str(exc))
    finally:
        with lock:
            job.pop("_process", None)
            job["finished_at"] = time.time()
            persist(job)


def work():
    while not stopping.is_set():
        try:
            task = pending.get(timeout=0.25)
        except queue.Empty:
            continue
        try:
            execute(task)
        finally:
            pending.task_done()


@contextlib.asynccontextmanager
async def lifespan(application):
    global worker_thread
    stopping.clear()
    restore_jobs()
    worker_thread = threading.Thread(target=work, daemon=True, name="motion-worker")
    worker_thread.start()
    watcher = asyncio.create_task(watch_clients(application))
    yield
    stopping.set()
    watcher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watcher
    with lock:
        active = [j.get("_process") for j in jobs.values()]
    for proc in active:
        kill_tree(proc)
    worker_thread.join(timeout=6)


app = FastAPI(title=CONFIG["title"], docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def session_and_origin(request: Request, call_next):
    user = authenticated_user(request.headers) if hosted() else None
    if hosted() and not user:
        return JSONResponse({'detail': '请使用服务器配置的账号登录。'}, status_code=401,
                            headers={'WWW-Authenticate': 'Basic realm="HY-Motion Studio", charset="UTF-8"'})
    if request.method in {"POST", "PUT", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            # Prevent cross-origin requests from controlling the local model process.
            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "请求来源不匹配，请在当前页面操作。"}, status_code=403)
    sid = request.cookies.get("motion_session", "")
    new = not re.fullmatch(r"[a-f0-9]{64}", sid)
    if new:
        sid = secrets.token_hex(32)
    request.state.owner = hashlib.sha256(('hosted:' + user if user else sid).encode()).hexdigest()
    response = await call_next(request)
    if new:
        response.set_cookie("motion_session", sid, httponly=True, samesite="lax", max_age=31536000,
                            secure=request.url.scheme == "https")
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "project": CONFIG["kind"], "showcase": SHOWCASE}


@app.get("/api/status")
def status(request: Request):
    import backend
    try:
        info = backend.inspect_backend()
    except Exception as exc:
        info = {"ready": False, "message": str(exc)}
    if SHOWCASE:
        info = {"ready": False, "message": "这是公开界面演示，未启用在线推理。下载项目后可连接本机模型环境。"}
    return {**CONFIG, "backend": info, "showcase": SHOWCASE,
            'runtime': {'mode': 'hosted' if hosted() else 'local', 'backend': execution_backend(),
                        'cancel_on_close': not hosted(), 'root_path': request.scope.get('root_path', '')}}


def owned(job_id, request):
    with lock:
        task = jobs.get(job_id)
    if not task or task["_owner"] != request.state.owner:
        raise HTTPException(404, "任务不存在")
    return task


def artifacts(task):
    root = Path(task["_dir"]).resolve()
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in ARTIFACT_EXTENSIONS and not path.is_symlink() and path.resolve().is_relative_to(root):
            relative = path.relative_to(root).as_posix()
            if (relative.startswith(('input/', '.')) or relative in {'task.json', 'remote-recovery.json'}
                    or any(part.startswith('.') for part in path.relative_to(root).parts)):
                continue
            result.append({"name": relative, "size": path.stat().st_size,
                           "url": f"/api/jobs/{task['id']}/files/{relative}"})
    return result


@app.post("/api/client/{client_id}/{action}")
def client_lifecycle(client_id: str, action: str, request: Request):
    global client_seen
    if not re.fullmatch(r"[a-f0-9-]{20,64}", client_id) or action not in {"open", "heartbeat", "close"}:
        raise HTTPException(400, "无效的界面会话")
    with lock:
        previous = clients.get(client_id)
        if previous and previous["owner"] != request.state.owner:
            raise HTTPException(404, "界面会话不存在")
        if action == "close":
            if previous:
                previous["closed"] = time.monotonic()
        else:
            clients[client_id] = {"owner": request.state.owner, "seen": time.monotonic(), "connected": bool(previous and previous.get("connected"))}
            client_seen = True
    return {"ok": True}


@app.websocket("/api/client/{client_id}/watch")
async def watch_connection(websocket: WebSocket, client_id: str):
    from urllib.parse import urlparse
    origin = websocket.headers.get("origin", "")
    sid = websocket.cookies.get("motion_session", "")
    if (urlparse(origin).netloc != websocket.headers.get("host")
            or not re.fullmatch(r"[a-f0-9]{64}", sid)):
        await websocket.close(code=1008)
        return
    user = authenticated_user(websocket.headers) if hosted() else None
    if hosted() and not user:
        await websocket.close(code=1008)
        return
    owner = hashlib.sha256(('hosted:' + user if user else sid).encode()).hexdigest()
    with lock:
        lease = clients.get(client_id)
        valid = lease and lease["owner"] == owner
    if not valid:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    with lock:
        lease.update(connected=True, seen=time.monotonic())
        lease.pop("closed", None)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with lock:
            current = clients.get(client_id)
            if current:
                current.update(connected=False, closed=time.monotonic())


def snapshot(task):
    with lock:
        info = {k: v for k, v in task.items() if not k.startswith("_")}
        info["log"] = list(task["log"])
    info["artifacts"] = artifacts(task) if task["status"] in {"complete", "failed", "cancelled"} else []
    return info


def submission_identity(request, kind, payload):
    """Optional, owner-scoped retry identity; the same key cannot change its input."""
    key = request.headers.get('idempotency-key')
    if key is None:
        return {}
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', key):
        raise HTTPException(400, '无效的提交标识')
    encoded = json.dumps([kind, payload], ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return {'_submission_key': key, '_submission_sha256': hashlib.sha256(encoded).hexdigest()}


def previous_submission(owner, identity):
    """Caller holds lock through lookup and any subsequent commit/enqueue."""
    if not identity:
        return None
    for collection in (jobs, batches):
        for item in collection.values():
            if item['_owner'] == owner and item.get('_submission_key') == identity['_submission_key']:
                if item.get('_submission_sha256') != identity['_submission_sha256']:
                    raise HTTPException(409, '此提交标识已用于其他输入，请为新任务使用新的提交标识。')
                return batch_snapshot(item) if 'job_ids' in item else snapshot(item)
    return None


@app.get("/api/jobs")
def list_jobs(request: Request):
    with lock:
        own = [j for j in jobs.values() if j["_owner"] == request.state.owner]
    return [snapshot(j) for j in sorted(own, key=lambda j: j["created_at"], reverse=True)]


@app.get("/api/jobs/{job_id}")
def read_job(job_id: str, request: Request):
    return snapshot(owned(job_id, request))


@app.post("/api/jobs")
async def new_job(request: Request, config: str = Form(...), video: UploadFile | None = File(None)):
    if SHOWCASE:
        raise HTTPException(409, "当前 Space 仅展示界面。请在本机或自己配置的推理环境运行。")
    import backend
    try:
        params = json.loads(config)
        if not isinstance(params, dict):
            raise ValueError("参数必须为对象")
    except (ValueError, TypeError):
        raise HTTPException(400, "参数格式不正确")
    if len(config) > 12000:
        raise HTTPException(400, "参数过长")
    video_digest = None
    if request.headers.get('idempotency-key') is not None and CONFIG['kind'] == 'gemx' and video:
        digest, total = hashlib.sha256(), 0
        while chunk := await video.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD:
                raise HTTPException(413, '视频超过 256 MB，请先裁剪片段。')
            digest.update(chunk)
        await video.seek(0)
        video_digest = {'sha256': digest.hexdigest(), 'suffix': Path(video.filename or '').suffix.lower()}
    try:
        identity = submission_identity(request, 'job', {'config': params, 'video': video_digest})
    except (ValueError, TypeError):
        raise HTTPException(400, '参数格式不正确')
    with lock:
        previous = previous_submission(request.state.owner, identity)
        if previous is not None:
            return previous
        if sum(j["_owner"] == request.state.owner and j["status"] in {"queued", "running"} for j in jobs.values()) >= 2:
            raise HTTPException(429, "已有两个待处理任务，请等待或取消后重试。")
    if not backend.inspect_backend().get("ready"):
        raise HTTPException(409, "模型环境未就绪，请查看环境状态和项目说明。")
    job_id = uuid.uuid4().hex
    directory = DATA / job_id
    directory.mkdir()
    input_path = None
    try:
        if CONFIG["kind"] == "gemx":
            if not video or not video.filename:
                raise HTTPException(400, "请先上传视频")
            suffix = Path(video.filename).suffix.lower()
            if suffix not in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
                raise HTTPException(400, "支持 MP4、MOV、MKV、AVI 和 WebM 视频")
            folder = directory / "input"
            folder.mkdir()
            input_path = folder / ("source" + suffix)
            total = 0
            with input_path.open("wb") as destination:
                while chunk := await video.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, "视频超过 256 MB，请先裁剪片段。")
                    destination.write(chunk)
            if not total:
                raise HTTPException(400, "上传的视频为空")
        with lock:
            # Uploads may yield; another request can commit the same key in the meantime.
            previous = previous_submission(request.state.owner, identity)
            if previous is not None:
                if input_path:
                    input_path.unlink(missing_ok=True)
                    input_path.parent.rmdir()
                directory.rmdir()
                return previous
            if sum(j['_owner'] == request.state.owner and j['status'] in {'queued', 'running'} for j in jobs.values()) >= 2:
                raise HTTPException(429, '已有两个待处理任务，请等待或取消后重试。')
            if pending.full():
                raise HTTPException(429, "队列已满，请稍后重试。")
            # Validate through the adapter before accepting a task; no process runs here.
            command = backend.build_command(directory, params, input_path)
            task = {"id": job_id, "status": "queued", "message": "已加入队列", "created_at": time.time(),
                    'execution_backend': execution_backend(), **identity,
                    "config": params, "log": [], "_owner": request.state.owner, "_dir": str(directory), "_input": input_path, "_command": command}
            persist(task)
            jobs[job_id] = task
            pending.put_nowait(task)
        return snapshot(task)
    except HTTPException:
        if input_path:
            input_path.unlink(missing_ok=True)
        raise
    except (ValueError, FileNotFoundError, TypeError, RuntimeError) as exc:
        if input_path:
            input_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    finally:
        if video:
            await video.close()


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str, request: Request):
    task = owned(job_id, request)
    proc = stop_task(task)
    kill_tree(proc)
    return snapshot(task)


@app.get("/api/jobs/{job_id}/files/{filename:path}")
def get_file(job_id: str, filename: str, request: Request, preview: bool = False):
    task = owned(job_id, request)
    allowed = {item["name"] for item in artifacts(task)}
    if filename not in allowed:
        raise HTTPException(404, "文件不存在")
    base = Path(task["_dir"]).resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(404, "文件不存在")
    if target.suffix.lower() in {".mp4", ".json"}:
        return FileResponse(target)
    if preview and target.suffix.lower() == ".html":
        return FileResponse(target, media_type="text/html", headers={"Content-Security-Policy": "sandbox allow-scripts"})
    return FileResponse(target, filename=target.name)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, request: Request):
    task = owned(job_id, request)
    if task["status"] != "complete":
        raise HTTPException(409, "任务尚未完成")
    directory = Path(task["_dir"])
    # Only explicit artifacts are archived; inputs, caches and environment never are.
    bundle = directory / "results.zip"
    with lock:
        if not bundle.exists():
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zipped:
                for item in artifacts(task):
                    zipped.write(directory / item["name"], item["name"])
    return FileResponse(bundle, filename=f"{CONFIG['kind']}-{job_id[:8]}.zip")


def owned_batch(batch_id, request):
    with lock:
        batch = batches.get(batch_id)
    if not batch or batch['_owner'] != request.state.owner:
        raise HTTPException(404, '批次不存在')
    return batch


def batch_snapshot(batch, include_jobs=True):
    with lock:
        tasks = [jobs[job_id] for job_id in batch['job_ids'] if job_id in jobs]
        counts = {state: sum(task['status'] == state for task in tasks)
                  for state in ('queued', 'running', 'complete', 'failed', 'cancelled')}
        if counts['running']:
            state = 'running'
        elif counts['queued']:
            state = 'queued'
        elif counts['failed'] or len(tasks) != len(batch['job_ids']):
            state = 'failed'
        elif counts['cancelled']:
            state = 'cancelled'
        else:
            state = 'complete'
        result = {key: batch[key] for key in ('id', 'name', 'created_at')}
        result.update(status=state, total=len(batch['job_ids']), counts=counts)
        if include_jobs:
            result['jobs'] = [snapshot(task) for task in tasks]
    return result


@app.get('/api/batches')
def list_batches(request: Request):
    with lock:
        owned_batches = [batch for batch in batches.values() if batch['_owner'] == request.state.owner]
        return [batch_snapshot(batch, False) for batch in sorted(owned_batches, key=lambda b: b['created_at'], reverse=True)]


@app.get('/api/batches/{batch_id}')
def read_batch(batch_id: str, request: Request):
    return batch_snapshot(owned_batch(batch_id, request))


@app.post('/api/batches')
async def new_batch(request: Request):
    if SHOWCASE:
        raise HTTPException(409, '当前 Space 仅展示界面，未启用在线推理。')
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(413, '批量输入过长')
    import backend
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get('config', {}), dict):
            raise ValueError('参数必须为对象')
        identity = submission_identity(request, 'batch', body)
        with lock:
            previous = previous_submission(request.state.owner, identity)
            if previous is not None:
                return previous
        prompts = body.get('prompts')
        repeat = body.get('repeat', 1)
        strategy = body.get('seed_mode', 'increment')
        name = body.get('name', '批量生成')
        if (not isinstance(prompts, list) or not prompts or len(prompts) > MAX_BATCH_ITEMS
                or any(not isinstance(p, str) or not p.strip() for p in prompts)):
            raise ValueError('请提供 1–100 条非空提示词')
        if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 10:
            raise ValueError('每条重复次数须为 1–10')
        if len(prompts) * repeat > MAX_BATCH_ITEMS:
            raise ValueError('每批最多生成 100 项')
        if strategy not in {'fixed', 'increment', 'random'}:
            raise ValueError('无效的种子策略')
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('批次名称须为 1–80 个字符')
        configurations = []
        base = body.get('config', {})
        seed = base.get('seed', 42)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise ValueError('种子须为 0–4294967295 的整数')
        for prompt in prompts:
            for _ in range(repeat):
                next_seed = secrets.randbelow(2**32) if strategy == 'random' else seed
                if strategy == 'increment':
                    next_seed = (seed + len(configurations)) % 2**32
                configurations.append(backend.validate_config({**base, 'prompt': prompt.strip(), 'seed': next_seed}))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(400, str(exc))
    if not backend.inspect_backend().get('ready'):
        raise HTTPException(409, '模型环境未就绪，请查看环境状态和项目说明。')
    batch_id = uuid.uuid4().hex
    batch = {'id': batch_id, 'name': name.strip(), 'created_at': time.time(), **identity,
             '_owner': request.state.owner, 'job_ids': []}
    tasks = []
    with lock:
        previous = previous_submission(request.state.owner, identity)
        if previous is not None:
            return previous
        own_active = sum(j['_owner'] == request.state.owner and j['status'] in {'queued', 'running'} for j in jobs.values())
        if own_active + len(configurations) > MAX_BATCH_ITEMS:
            raise HTTPException(429, '当前账号最多保留 100 项待处理任务，请等待或取消已有任务。')
        if pending.maxsize and pending.qsize() + len(configurations) > pending.maxsize:
            raise HTTPException(429, '队列容量不足，整批尚未提交。')
        try:
            for index, params in enumerate(configurations):
                job_id = uuid.uuid4().hex
                directory = DATA / job_id
                directory.mkdir()
                command = backend.build_command(directory, params, None)
                task = {'id': job_id, 'batch_id': batch_id, 'batch_index': index,
                        'status': 'queued', 'message': '已加入批次队列',
                        'created_at': batch['created_at'] + index / 1000000,
                        'execution_backend': execution_backend(), 'config': params, 'log': [],
                        '_owner': request.state.owner, '_dir': str(directory), '_command': command}
                tasks.append(task)
                batch['job_ids'].append(job_id)
                persist(task)
            (DATA / 'batches').mkdir(exist_ok=True)
            atomic_json(DATA / 'batches' / f'{batch_id}.json', batch)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            # No manifest means no partial batch can run, including after a restart.
            raise HTTPException(400, f'批次未提交：{exc}')
        batches[batch_id] = batch
        for task in tasks:
            jobs[task['id']] = task
            pending.put_nowait(task)
    return batch_snapshot(batch)


@app.post('/api/batches/{batch_id}/cancel')
def cancel_batch(batch_id: str, request: Request):
    batch = owned_batch(batch_id, request)
    with lock:
        processes = [stop_task(jobs[job_id]) for job_id in batch['job_ids'] if job_id in jobs]
    for proc in processes:
        kill_tree(proc)
    return batch_snapshot(batch)


@app.get('/api/batches/{batch_id}/download')
def download_batch(batch_id: str, request: Request):
    batch = owned_batch(batch_id, request)
    with lock:
        complete = [jobs[job_id] for job_id in batch['job_ids'] if job_id in jobs and jobs[job_id]['status'] == 'complete']
        if not complete:
            raise HTTPException(409, '此批次暂无已完成的结果')
        folder = DATA / 'batches'
        # Each immutable bundle describes a specific set of completed results.
        digest = hashlib.sha256(','.join(t['id'] for t in complete).encode()).hexdigest()[:16]
        target = folder / f'{batch_id}-{digest}.zip'
        if not target.exists():
            temporary = target.with_suffix('.tmp')
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zipped:
                manifest = []
                for task in complete:
                    prefix = f"{task['batch_index'] + 1:03d}-{task['id'][:8]}"
                    manifest.append({'folder': prefix, 'job_id': task['id'], **task['config']})
                    for item in artifacts(task):
                        zipped.write(Path(task['_dir']) / item['name'], prefix + '/' + item['name'])
                zipped.writestr('batch.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            os.replace(temporary, target)
    return FileResponse(target, filename=f'hymotion-batch-{batch_id[:8]}.zip')


def main():
    parser = argparse.ArgumentParser(description=CONFIG["title"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=CONFIG["port"], type=int)
    parser.add_argument('--root-path', default=os.environ.get('MOTION_ROOT_PATH', ''))
    args = parser.parse_args()
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, root_path=args.root_path, access_log=False))
    app.state.exit_on_close = not hosted() and not SHOWCASE and not os.environ.get("SPACE_ID")
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
