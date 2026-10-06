"""HTTP/process boundary tests. No model packages or weights are loaded."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import zipfile

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite_dir = tempfile.TemporaryDirectory(prefix="webui-tests-", dir=ROOT)
        with mock.patch.dict(os.environ, {"MOTION_DATA_DIR": cls.suite_dir.name, "MOTION_SHOWCASE": "0"}):
            spec = importlib.util.spec_from_file_location("motion_webui_test_app", ROOT / "app.py")
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        cls.suite_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.suite_dir.name)
        self.addCleanup(self.temp.cleanup)
        self.mod.DATA = Path(self.temp.name)
        self.mod.SHOWCASE = False
        self.mod.CONFIG = {**self.mod.CONFIG, "kind": "hymotion"}
        self.mod.jobs = {}
        self.mod.pending = queue.Queue(maxsize=4)
        self.mod.stopping = threading.Event()
        self.mod.worker_thread = None
        self.mod.clients = {}
        self.mod.client_seen = False
        self.mod.app.state.exit_on_close = False
        self.mod.app.state.shutdown_callback = None
        self.build_calls = []

        def build(directory, params, input_path):
            self.build_calls.append((directory, dict(params), input_path))
            if params.get("invalid"):
                raise ValueError("invalid model parameters")
            # Exclusive creation detects accidentally calling the builder twice.
            with (directory / "request.json").open("x", encoding="utf-8") as stream:
                json.dump(params, stream)
            return [sys.executable, "-c", "import sys; print(sys.argv[1])", params.get("prompt", "test")]

        self.fake = types.SimpleNamespace(
            inspect_backend=mock.Mock(return_value={"ready": True, "message": "test backend"}),
            build_command=mock.Mock(side_effect=build),
        )
        self.patch = mock.patch.dict(sys.modules, {"backend": self.fake})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # Tests without a lifespan intentionally keep tasks queued and deterministic.
        self.client = TestClient(self.mod.app)
        self.addCleanup(self.client.close)

    def submit(self, params=None, client=None, video=None):
        arguments = {"data": {"config": json.dumps(params or {"prompt": "walk"})}}
        if video is not None:
            arguments["files"] = {"video": video}
        return (client or self.client).post("/api/jobs", **arguments)

    def task(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        return self.mod.jobs[response.json()["id"]]

    def finish_with_files(self):
        task = self.task()
        directory = Path(task["_dir"])
        (directory / "input").mkdir()
        (directory / "input" / "source.mp4").write_bytes(b"private source video")
        (directory / "outputs").mkdir()
        (directory / "outputs" / "motion.npz").write_bytes(b"actual generated artifact")
        (directory / "outputs" / "preview.json").write_text('{"joints":[]}', encoding="utf-8")
        (directory / "private.txt").write_text("not an artifact", encoding="utf-8")
        task.update(status="complete", finished_at=time.time())
        self.mod.persist(task)
        return task

    def test_session_is_private_and_internals_are_not_returned(self):
        task = self.task()
        job_id = task["id"]
        own = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(own.status_code, 200)
        self.assertFalse(any(key.startswith("_") for key in own.json()))
        self.assertEqual(len(self.client.get("/api/jobs").json()), 1)
        outsider = TestClient(self.mod.app)
        self.addCleanup(outsider.close)
        self.assertEqual(outsider.get("/api/jobs").json(), [])
        self.assertEqual(outsider.get(f"/api/jobs/{job_id}").status_code, 404)
        self.assertEqual(outsider.post(f"/api/jobs/{job_id}/cancel").status_code, 404)
        self.assertEqual(outsider.get(f"/api/jobs/{job_id}/download").status_code, 404)

    def test_cookie_and_api_cache_headers(self):
        result = self.client.get("/api/health")
        cookie = result.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.headers["x-content-type-options"], "nosniff")

    def test_origin_mismatch_rejected_before_task_creation(self):
        response = self.client.post("/api/jobs", data={"config": "{}"}, headers={"Origin": "https://elsewhere.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.mod.jobs, {})
        self.fake.build_command.assert_not_called()

    def test_same_origin_allowed(self):
        response = self.client.post("/api/jobs", data={"config": '{"prompt":"walk"}'}, headers={"Origin": "http://testserver"})
        self.assertEqual(response.status_code, 200)

    def test_malformed_and_non_object_config_rejected(self):
        for config in ("{bad", "[]", "null", '"text"', "42"):
            with self.subTest(config=config):
                self.assertEqual(self.client.post("/api/jobs", data={"config": config}).status_code, 400)
        self.assertEqual(self.mod.jobs, {})
        self.fake.build_command.assert_not_called()

    def test_missing_and_oversized_config_rejected(self):
        self.assertEqual(self.client.post("/api/jobs").status_code, 422)
        response = self.submit({"prompt": "a" * 12001})
        self.assertEqual(response.status_code, 400)
        self.fake.build_command.assert_not_called()

    def test_backend_validation_error_does_not_queue(self):
        response = self.submit({"invalid": True})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.mod.pending.empty())
        self.assertEqual(self.mod.jobs, {})

    def test_missing_runtime_does_not_queue(self):
        self.fake.inspect_backend.return_value = {"ready": False}
        self.assertEqual(self.submit().status_code, 409)
        self.fake.build_command.assert_not_called()

    def test_showcase_never_invokes_backend_command(self):
        self.mod.SHOWCASE = True
        self.assertEqual(self.submit().status_code, 409)
        self.assertFalse(self.client.get("/api/status").json()["backend"]["ready"])
        self.fake.build_command.assert_not_called()
        self.assertTrue(self.mod.pending.empty())

    def test_two_active_task_limit_is_per_session(self):
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit().status_code, 429)
        other = TestClient(self.mod.app)
        self.addCleanup(other.close)
        self.assertEqual(self.submit(client=other).status_code, 200)

    def test_global_queue_capacity(self):
        self.mod.pending = queue.Queue(maxsize=1)
        self.assertEqual(self.submit().status_code, 200)
        other = TestClient(self.mod.app)
        self.addCleanup(other.close)
        self.assertEqual(self.submit(client=other).status_code, 429)
        self.assertEqual(len(self.mod.jobs), 1)

    def test_command_built_once_and_prompt_is_not_executed_by_shell(self):
        marker = Path(self.temp.name) / "injected.txt"
        prompt = f'walk & echo unsafe > "{marker}"; $(echo unsafe)'
        response = self.submit({"prompt": prompt})
        self.assertEqual(response.status_code, 200)
        task = self.mod.jobs[response.json()["id"]]
        real_popen = subprocess.Popen
        with mock.patch.object(self.mod.subprocess, "Popen", wraps=real_popen) as popen:
            self.mod.execute(task)
        self.assertEqual(self.fake.build_command.call_count, 1)
        self.assertEqual(task["status"], "complete")
        self.assertIn(prompt, task["log"])
        self.assertFalse(marker.exists())
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        self.assertIsInstance(popen.call_args.args[0], list)

    def test_cancel_queued_task_never_launches(self):
        task = self.task()
        response = self.client.post(f"/api/jobs/{task['id']}/cancel")
        self.assertEqual(response.json()["status"], "cancelled")
        with mock.patch.object(self.mod.subprocess, "Popen") as popen:
            self.mod.execute(task)
        popen.assert_not_called()

    def test_lifespan_worker_cancellation_stops_real_harmless_child(self):
        self.fake.build_command.side_effect = lambda *args: [sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"]
        proc = None
        try:
            with TestClient(self.mod.app) as client:
                response = self.submit(client=client)
                self.assertEqual(response.status_code, 200)
                task = self.mod.jobs[response.json()["id"]]
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    with self.mod.lock:
                        proc = task.get("_process")
                        started = "ready" in task["log"]
                    if proc is not None and started:
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(proc, "worker did not launch the harmless child")
                self.assertTrue(started)
                self.assertEqual(client.post(f"/api/jobs/{task['id']}/cancel").json()["status"], "cancelled")
                proc.wait(timeout=6)
                self.assertIsNotNone(proc.poll())
        finally:
            self.mod.kill_tree(proc)
            self.mod.stopping.set()
            if self.mod.worker_thread:
                self.mod.worker_thread.join(timeout=6)
                self.assertFalse(self.mod.worker_thread.is_alive())

    def test_artifacts_and_zip_contain_outputs_not_uploaded_inputs(self):
        task = self.finish_with_files()
        endpoint = f"/api/jobs/{task['id']}"
        info = self.client.get(endpoint).json()
        names = {item["name"] for item in info["artifacts"]}
        self.assertIn("outputs/motion.npz", names)
        self.assertNotIn("input/source.mp4", names)
        self.assertNotIn("task.json", names)
        self.assertNotIn(".owner", names)
        self.assertNotIn("private.txt", names)
        result = self.client.get(endpoint + "/download")
        self.assertEqual(result.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(result.content)) as bundle:
            self.assertEqual(set(bundle.namelist()), names)
            self.assertEqual(bundle.read("outputs/motion.npz"), b"actual generated artifact")
        self.assertEqual(self.client.get(endpoint + "/files/outputs/motion.npz").content, b"actual generated artifact")

    def test_private_files_and_path_traversal_cannot_be_downloaded(self):
        task = self.finish_with_files()
        base = f"/api/jobs/{task['id']}/files/"
        for name in ("task.json", ".owner", "input/source.mp4", "private.txt", "../app.py", "%2e%2e%2fapp.py", "%2e%2e%5capp.py", "C:%5cWindows%5cwin.ini"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(base + name).status_code, 404)
        outsider = TestClient(self.mod.app)
        self.addCleanup(outsider.close)
        self.assertEqual(outsider.get(base + "outputs/motion.npz").status_code, 404)

    def test_unfinished_job_has_no_bundle(self):
        task = self.task()
        self.assertEqual(self.client.get(f"/api/jobs/{task['id']}/download").status_code, 409)

    def test_video_validation_and_untrusted_filename(self):
        self.mod.CONFIG["kind"] = "gemx"
        for video, expected in ((None, 400), (("clip.exe", b"bad", "video/mp4"), 400), (("clip.mp4", b"", "video/mp4"), 400)):
            with self.subTest(video=video):
                self.assertEqual(self.submit(video=video).status_code, expected)
        response = self.submit(video=("../../elsewhere.mp4", b"short test content", "video/mp4"))
        self.assertEqual(response.status_code, 200)
        task = self.mod.jobs[response.json()["id"]]
        path = self.build_calls[-1][2]
        self.assertEqual(path, Path(task["_dir"]) / "input" / "source.mp4")
        self.assertEqual(path.read_bytes(), b"short test content")

    def test_oversized_video_removed_and_not_queued(self):
        self.mod.CONFIG["kind"] = "gemx"
        with mock.patch.object(self.mod, "MAX_UPLOAD", 8):
            response = self.submit(video=("clip.mp4", b"123456789", "video/mp4"))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(list(self.mod.DATA.rglob("*.mp4")), [])
        self.fake.build_command.assert_not_called()
        self.assertTrue(self.mod.pending.empty())

    def test_restore_cancels_incomplete_jobs_without_enqueuing(self):
        task = self.task()
        job_id = task["id"]
        task["status"] = "running"
        self.mod.persist(task)
        self.mod.jobs.clear()
        self.mod.pending = queue.Queue(maxsize=4)
        self.mod.restore_jobs()
        self.assertEqual(self.mod.jobs[job_id]["status"], "cancelled")
        self.assertTrue(self.mod.pending.empty())
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 200)

    def watch_once(self):
        async def one_tick(_seconds):
            self.mod.stopping.set()

        self.mod.stopping.clear()
        with mock.patch.object(self.mod.asyncio, "sleep", side_effect=one_tick):
            asyncio.run(self.mod.watch_clients(self.mod.app))

    def open_client(self, client=None, client_id="a" * 32):
        response = (client or self.client).post(f"/api/client/{client_id}/open")
        self.assertEqual(response.status_code, 200)
        return client_id

    def expire_client(self, client_id):
        self.mod.clients[client_id]["closed"] = time.monotonic() - self.mod.CLOSE_GRACE - 1

    def test_client_lease_privacy_and_validation(self):
        client_id = self.open_client()
        outsider = TestClient(self.mod.app)
        self.addCleanup(outsider.close)
        for action in ("open", "heartbeat", "close"):
            self.assertEqual(outsider.post(f"/api/client/{client_id}/{action}").status_code, 404)
        self.assertEqual(self.client.post("/api/client/invalid/open").status_code, 400)
        self.assertEqual(self.client.post(f"/api/client/{client_id}/unknown").status_code, 400)

    def test_last_local_tab_close_cancels_task_and_requests_shutdown(self):
        task = self.task()
        client_id = self.open_client()
        self.expire_client(client_id)
        callback = mock.Mock()
        self.mod.app.state.exit_on_close = True
        self.mod.app.state.shutdown_callback = callback
        self.watch_once()
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(self.mod.clients, {})
        callback.assert_called_once_with()

    def test_page_refresh_reopens_lease_within_grace(self):
        task = self.task()
        client_id = self.open_client()
        self.assertEqual(self.client.post(f"/api/client/{client_id}/close").status_code, 200)
        self.assertIn("closed", self.mod.clients[client_id])
        self.open_client(client_id=client_id)
        self.assertNotIn("closed", self.mod.clients[client_id])
        self.watch_once()
        self.assertEqual(task["status"], "queued")
        self.assertIn(client_id, self.mod.clients)

    def test_another_tab_keeps_same_session_tasks_alive(self):
        task = self.task()
        closed = self.open_client(client_id="a" * 32)
        remaining = self.open_client(client_id="b" * 32)
        self.expire_client(closed)
        callback = mock.Mock()
        self.mod.app.state.exit_on_close = True
        self.mod.app.state.shutdown_callback = callback
        self.watch_once()
        self.assertEqual(task["status"], "queued")
        self.assertIn(remaining, self.mod.clients)
        callback.assert_not_called()

    def test_shared_space_close_only_cancels_that_owner(self):
        own = self.task()
        client_id = self.open_client()
        other = TestClient(self.mod.app)
        self.addCleanup(other.close)
        other_result = self.submit(client=other)
        other_task = self.mod.jobs[other_result.json()["id"]]
        self.open_client(client=other, client_id="b" * 32)
        self.expire_client(client_id)
        callback = mock.Mock()
        self.mod.app.state.shutdown_callback = callback
        self.mod.app.state.exit_on_close = False
        self.watch_once()
        self.assertEqual(own["status"], "cancelled")
        self.assertEqual(other_task["status"], "queued")
        callback.assert_not_called()

    def test_disconnected_tab_expires_without_close_beacon(self):
        task = self.task()
        client_id = self.open_client()
        self.mod.clients[client_id]["seen"] = time.monotonic() - self.mod.CLIENT_TIMEOUT - 1
        self.watch_once()
        self.assertEqual(task["status"], "cancelled")
        self.assertNotIn(client_id, self.mod.clients)

    def wait_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(predicate(), "expected asynchronous state transition did not occur")

    def assert_socket_rejected(self, client, client_id, headers):
        with self.assertRaises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(f"/api/client/{client_id}/watch", headers=headers):
                self.fail("untrusted WebSocket connection was accepted")
        self.assertEqual(rejected.exception.code, 1008)

    def test_websocket_disconnect_marks_close_without_beacon_and_stops_local(self):
        task = self.task()
        client_id = self.open_client()
        callback = mock.Mock()
        self.mod.app.state.exit_on_close = True
        self.mod.app.state.shutdown_callback = callback
        with self.client.websocket_connect(f"/api/client/{client_id}/watch", headers={"origin": "http://testserver"}) as socket:
            self.wait_until(lambda: self.mod.clients[client_id].get("connected"))
            socket.send_text("ping")
            self.assertNotIn("closed", self.mod.clients[client_id])
        self.wait_until(lambda: bool(self.mod.clients[client_id].get("closed")))
        self.assertFalse(self.mod.clients[client_id]["connected"])
        self.expire_client(client_id)
        self.watch_once()
        self.assertEqual(task["status"], "cancelled")
        callback.assert_called_once_with()

    def test_connected_websocket_survives_background_heartbeat_timeout(self):
        task = self.task()
        client_id = self.open_client()
        with self.client.websocket_connect(f"/api/client/{client_id}/watch", headers={"origin": "http://testserver"}):
            self.wait_until(lambda: self.mod.clients[client_id].get("connected"))
            self.mod.clients[client_id]["seen"] = time.monotonic() - self.mod.CLIENT_TIMEOUT - 1
            self.watch_once()
            self.assertEqual(task["status"], "queued")
            self.assertIn(client_id, self.mod.clients)
            self.assertTrue(self.mod.clients[client_id]["connected"])

    def test_websocket_rejects_missing_or_cross_origin(self):
        client_id = self.open_client()
        for origin in (None, "https://elsewhere.invalid", "null"):
            with self.subTest(origin=origin):
                self.assert_socket_rejected(self.client, client_id, {} if origin is None else {"origin": origin})
        self.assertFalse(self.mod.clients[client_id].get("connected", False))

    def test_websocket_rejects_missing_or_malformed_session_cookie(self):
        client_id = self.open_client()
        outsider = TestClient(self.mod.app)
        self.addCleanup(outsider.close)
        self.assert_socket_rejected(outsider, client_id, {"origin": "http://testserver"})
        self.assert_socket_rejected(outsider, client_id, {"origin": "http://testserver", "cookie": "motion_session=invalid"})

    def test_websocket_rejects_other_owner_and_unknown_lease(self):
        client_id = self.open_client()
        outsider = TestClient(self.mod.app)
        self.addCleanup(outsider.close)
        outsider.get("/api/health")
        self.assert_socket_rejected(outsider, client_id, {"origin": "http://testserver"})
        self.assert_socket_rejected(self.client, "f" * 32, {"origin": "http://testserver"})

    def test_websocket_reconnect_within_grace_keeps_task_alive(self):
        task = self.task()
        client_id = self.open_client()
        with self.client.websocket_connect(f"/api/client/{client_id}/watch", headers={"origin": "http://testserver"}):
            self.wait_until(lambda: self.mod.clients[client_id].get("connected"))
        self.wait_until(lambda: bool(self.mod.clients[client_id].get("closed")))
        with self.client.websocket_connect(f"/api/client/{client_id}/watch", headers={"origin": "http://testserver"}):
            self.wait_until(lambda: self.mod.clients[client_id].get("connected"))
            self.assertNotIn("closed", self.mod.clients[client_id])
            self.watch_once()
            self.assertEqual(task["status"], "queued")


if __name__ == "__main__":
    unittest.main()
