"""Small web interface. Model runtimes stay in separate, existing environments."""
from __future__ import annotations

import argparse
import asyncio
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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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
pending: queue.Queue = queue.Queue(maxsize=4)
lock = threading.RLock()
stopping = threading.Event()
worker_thread = None
clients: dict[str, dict] = {}
client_seen = False
CLIENT_TIMEOUT = 180
CLOSE_GRACE = 3


def cancel_owned(owner=None):
    with lock:
        active = [j for j in jobs.values() if j["status"] in {"queued", "running"} and (owner is None or j["_owner"] == owner)]
        for task in active:
            task.update(status="cancelled", message="界面已关闭，任务已停止。", finished_at=time.time())
            persist(task)
        processes = [j.get("_process") for j in active]
    for process in processes:
        kill_tree(process)


async def watch_clients(application):
    while not stopping.is_set():
        await asyncio.sleep(1)
        now = time.monotonic()
        with lock:
            expired = [key for key, item in clients.items() if now - item["seen"] > CLIENT_TIMEOUT or (item.get("closed") and now - item["closed"] > CLOSE_GRACE)]
            owners = {clients.pop(key)["owner"] for key in expired}
            remaining_owners = {item["owner"] for item in clients.values()}
        for owner in owners - remaining_owners:
            await asyncio.to_thread(cancel_owned, owner)
        if client_seen and not clients and getattr(application.state, "exit_on_close", False):
            await asyncio.to_thread(cancel_owned)
            callback = getattr(application.state, "shutdown_callback", None)
            if callback:
                callback()
            return


def persist(job):
    public = {k: v for k, v in job.items() if not k.startswith("_")}
    path = Path(job["_dir"]) / "task.json"
    path.write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
    (Path(job["_dir"]) / ".owner").write_text(job["_owner"], encoding="ascii")


def restore_jobs():
    for directory in DATA.iterdir():
        if not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
            continue
        try:
            task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
            owner = (directory / ".owner").read_text(encoding="ascii")
            if not re.fullmatch(r"[a-f0-9]{64}", owner) or task["id"] != directory.name:
                continue
            task.update(_owner=owner, _dir=str(directory))
            if task["status"] in {"queued", "running"}:
                task.update(status="cancelled", message="服务已重启，此任务已停止，请重新创建。", finished_at=time.time())
                persist(task)
            jobs[task["id"]] = task
        except (OSError, ValueError, KeyError):
            continue


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
    request.state.owner = hashlib.sha256(sid.encode()).hexdigest()
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
def status():
    import backend
    try:
        info = backend.inspect_backend()
    except Exception as exc:
        info = {"ready": False, "message": str(exc)}
    if SHOWCASE:
        info = {"ready": False, "message": "这是公开界面演示，未启用在线推理。下载项目后可连接本机模型环境。"}
    return {**CONFIG, "backend": info, "showcase": SHOWCASE}


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
            if relative.startswith("input/") or relative == "task.json":
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
            clients[client_id] = {"owner": request.state.owner, "seen": time.monotonic()}
            client_seen = True
    return {"ok": True}


def snapshot(task):
    with lock:
        info = {k: v for k, v in task.items() if not k.startswith("_")}
        info["log"] = list(task["log"])
    info["artifacts"] = artifacts(task) if task["status"] in {"complete", "failed", "cancelled"} else []
    return info


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
    if not backend.inspect_backend().get("ready"):
        raise HTTPException(409, "模型环境未就绪，请查看环境状态和项目说明。")
    try:
        params = json.loads(config)
        if not isinstance(params, dict):
            raise ValueError("参数必须为对象")
    except (ValueError, TypeError):
        raise HTTPException(400, "参数格式不正确")
    if len(config) > 12000:
        raise HTTPException(400, "参数过长")
    with lock:
        if sum(j["_owner"] == request.state.owner and j["status"] in {"queued", "running"} for j in jobs.values()) >= 2:
            raise HTTPException(429, "已有两个待处理任务，请等待或取消后重试。")
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
        # Validate through the adapter before accepting a task; no process runs here.
        command = backend.build_command(directory, params, input_path)
        task = {"id": job_id, "status": "queued", "message": "已加入队列", "created_at": time.time(),
                "config": params, "log": [], "_owner": request.state.owner, "_dir": str(directory), "_input": input_path, "_command": command}
        with lock:
            if pending.full():
                raise HTTPException(429, "队列已满，请稍后重试。")
            jobs[job_id] = task
            persist(task)
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
    with lock:
        if task["status"] in {"queued", "running"}:
            task.update(status="cancelled", message="任务已取消", finished_at=time.time())
            proc = task.get("_process")
            persist(task)
        else:
            proc = None
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


def main():
    parser = argparse.ArgumentParser(description=CONFIG["title"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=CONFIG["port"], type=int)
    args = parser.parse_args()
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, access_log=False))
    app.state.exit_on_close = not SHOWCASE and not os.environ.get("SPACE_ID")
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
