"""Batch and hosted-service boundaries. Uses a fake backend, never FC or a model."""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import zipfile

from fastapi.testclient import TestClient
import backend as real_backend

ROOT = Path(__file__).resolve().parents[1]


class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = tempfile.TemporaryDirectory(prefix="batch-tests-")
        with mock.patch.dict(os.environ, {"MOTION_DATA_DIR": cls.suite.name, "MOTION_SHOWCASE": "0"}):
            spec = importlib.util.spec_from_file_location("motion_batch_test_app", ROOT / "app.py")
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        cls.suite.cleanup()

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "MOTION_MODE": "local", "MOTION_BACKEND": "local",
            "MOTION_AUTH_USER": "", "MOTION_AUTH_PASSWORD_SHA256": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory(dir=self.suite.name)
        self.addCleanup(self.temp.cleanup)
        self.mod.DATA = Path(self.temp.name)
        self.mod.SHOWCASE = False
        self.mod.CONFIG = {**self.mod.CONFIG, "kind": "hymotion"}
        self.mod.jobs = {}
        self.mod.batches = {}
        self.mod.pending = queue.Queue(maxsize=200)
        self.mod.stopping = threading.Event()
        self.mod.clients = {}
        self.mod.client_seen = False
        self.mod.app.state.exit_on_close = False
        self.mod.app.state.shutdown_callback = None
        self.build_calls = []
        self.fake = types.SimpleNamespace(
            validate_config=real_backend.validate_config,
            inspect_backend=mock.Mock(return_value={"ready": True, "message": "test backend"}),
            build_command=mock.Mock(side_effect=self.build),
            resume_command=mock.Mock(side_effect=self.resume),
        )
        patcher = mock.patch.dict(sys.modules, {"backend": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Without entering its lifespan, TestClient leaves the queue deterministic.
        self.client = self.new_client()

    def new_client(self, auth=None, **kwargs):
        client = TestClient(self.mod.app, **kwargs)
        if auth:
            value = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            client.headers["Authorization"] = "Basic " + value
        self.addCleanup(client.close)
        return client

    def command(self, directory):
        code = "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.mkdir(); (p/'motion.npz').write_bytes(b'fake motion'); print('finished')"
        return [sys.executable, "-u", "-c", code, str(directory / "output")]

    def build(self, directory, config, input_path=None):
        self.assertIsNone(input_path)
        params = real_backend.validate_config(config)
        directory = Path(directory)
        self.build_calls.append((directory, params))
        with (directory / "request.json").open("x", encoding="utf-8") as stream:
            json.dump(params, stream)
        return self.command(directory)

    def resume(self, directory, config):
        self.assertEqual(json.loads((directory / "request.json").read_text()), config)
        return self.command(directory)

    def submit(self, body=None, client=None, key=None):
        return (client or self.client).post("/api/batches", json=body if body is not None else {
            "prompts": ["walk", "turn"], "repeat": 1, "seed_mode": "increment",
            "config": {"seconds": 2, "seed": 70, "steps": 25, "threads": 3},
        }, headers={"Idempotency-Key": key} if key is not None else {})

    def batch(self, body=None, client=None):
        response = self.submit(body, client)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def hosted(self, username="operator", password="test password"):
        os.environ.update(MOTION_MODE="hosted", MOTION_AUTH_USER=username,
                          MOTION_AUTH_PASSWORD_SHA256=hashlib.sha256(password.encode()).hexdigest())
        return self.new_client(auth=(username, password))

    def clear_memory(self, capacity=200):
        self.mod.jobs = {}
        self.mod.batches = {}
        self.mod.pending = queue.Queue(maxsize=capacity)

    def test_increment_and_fixed_expand_prompts_in_order_without_changing_parameters(self):
        for strategy, expected in (("increment", [4294967295, 0, 1, 2]), ("fixed", [4294967295] * 4)):
            with self.subTest(strategy=strategy):
                result = self.batch({"name": "  gestures  ", "prompts": [" walk ", "turn"], "repeat": 2,
                                     "seed_mode": strategy, "config": {"seconds": 2.5, "seed": 4294967295, "steps": 37, "threads": 6}})
                self.assertEqual(result["name"], "gestures")
                self.assertEqual(result["total"], 4)
                self.assertEqual(result["counts"]["queued"], 4)
                self.assertEqual([j["config"]["prompt"] for j in result["jobs"]], ["walk", "walk", "turn", "turn"])
                self.assertEqual([j["config"]["seed"] for j in result["jobs"]], expected)
                self.assertEqual([j["batch_index"] for j in result["jobs"]], list(range(4)))
                for job in result["jobs"]:
                    self.assertEqual(job["batch_id"], result["id"])
                    self.assertEqual([job["config"][key] for key in ("seconds", "steps", "threads")], [2.5, 37, 6])
                    stored = json.loads((self.mod.DATA / job["id"] / "request.json").read_text())
                    self.assertEqual(stored, job["config"])

    def test_random_seeds_are_selected_once_and_saved_in_request_and_history(self):
        with mock.patch.object(self.mod.secrets, "randbelow", side_effect=[13, 73]) as random_seed:
            result = self.batch({"prompts": ["walk"], "repeat": 2, "seed_mode": "random", "config": {"seed": 42}})
        self.assertEqual(random_seed.call_count, 2)
        self.assertEqual([j["config"]["seed"] for j in result["jobs"]], [13, 73])
        reloaded = self.client.get(f"/api/batches/{result['id']}").json()
        self.assertEqual([j["config"]["seed"] for j in reloaded["jobs"]], [13, 73])
        for job in result["jobs"]:
            stored = json.loads((self.mod.DATA / job["id"] / "request.json").read_text())
            self.assertEqual(stored["seed"], job["config"]["seed"])

    def test_batch_retry_keeps_identity_random_seeds_and_queue_after_hosted_restart(self):
        client = self.hosted()
        body = {"prompts": ["walk"], "repeat": 2, "seed_mode": "random"}
        with mock.patch.object(self.mod.secrets, "randbelow", side_effect=[13, 73]) as random_seed:
            first = self.submit(body, client=client, key="lost-response").json()
            retry = self.submit(body, client=client, key="lost-response")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["id"], first["id"])
        self.assertEqual(random_seed.call_count, 2)
        self.assertEqual(self.fake.build_command.call_count, 2)
        self.assertEqual(self.mod.pending.qsize(), 2)
        self.clear_memory()
        self.mod.restore_jobs()
        # Retrying an accepted submission does not depend on current backend readiness.
        self.fake.inspect_backend.return_value = {"ready": False}
        second_browser = self.new_client(auth=("operator", "test password"))
        restored = self.submit(body, client=second_browser, key="lost-response")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["id"], first["id"])
        self.assertEqual([j["config"]["seed"] for j in restored.json()["jobs"]], [13, 73])
        self.assertEqual(self.mod.pending.qsize(), 2)
        self.assertEqual(self.fake.build_command.call_count, 2)
        self.assertNotIn("_submission", restored.text)

    def test_submission_key_is_owner_scoped_and_rejects_changed_input_or_route(self):
        first = self.submit(key="same-key").json()
        other = self.new_client()
        independent = self.submit(client=other, key="same-key")
        self.assertEqual(independent.status_code, 200, independent.text)
        self.assertNotEqual(independent.json()["id"], first["id"])
        changed = self.submit({"prompts": ["different"]}, key="same-key")
        self.assertEqual(changed.status_code, 409, changed.text)
        other_route = self.client.post("/api/jobs", data={"config": '{"prompt":"walk"}'},
                                       headers={"Idempotency-Key": "same-key"})
        self.assertEqual(other_route.status_code, 409, other_route.text)
        self.assertEqual(len(self.mod.batches), 2)
        self.assertEqual(self.mod.pending.qsize(), 4)

    def test_simultaneous_batch_retries_commit_and_enqueue_only_once(self):
        self.hosted()
        clients = [self.new_client(auth=("operator", "test password")) for _ in range(2)]
        gate = threading.Barrier(2)
        def send(client):
            gate.wait(timeout=5)
            return self.submit(client=client, key="concurrent-key")
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(send, clients))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(responses[0].json()["id"], responses[1].json()["id"])
        self.assertEqual(len(self.mod.batches), 1)
        self.assertEqual(self.mod.pending.qsize(), 2)
        self.assertEqual(self.fake.build_command.call_count, 2)

    def test_single_submission_retry_is_persisted_and_new_key_can_create_another_job(self):
        client = self.hosted()
        options = {"data": {"config": '{"prompt":"walk"}'}, "headers": {"Idempotency-Key": "single-key"}}
        first = client.post("/api/jobs", **options)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertNotIn("_submission", first.text)
        self.clear_memory()
        self.mod.restore_jobs()
        retry = client.post("/api/jobs", **options)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertEqual(self.fake.build_command.call_count, 1)
        self.assertEqual(self.mod.pending.qsize(), 1)
        options["headers"]["Idempotency-Key"] = "intentional-new-generation"
        another = client.post("/api/jobs", **options)
        self.assertEqual(another.status_code, 200, another.text)
        self.assertNotEqual(another.json()["id"], first.json()["id"])
        self.assertEqual(self.mod.pending.qsize(), 2)

    def test_invalid_submission_keys_never_create_or_queue_work(self):
        for key in ("", "a" * 129, "spaces are invalid"):
            with self.subTest(key=key):
                self.assertEqual(self.submit(key=key).status_code, 400)
                self.assertEqual(self.client.post("/api/jobs", data={"config": '{"prompt":"walk"}'},
                    headers={"Idempotency-Key": key}).status_code, 400)
        self.fake.build_command.assert_not_called()
        self.assertTrue(self.mod.pending.empty())
        self.assertEqual(list(self.mod.DATA.iterdir()), [])

    def test_invalid_batches_reject_every_item_before_any_command_or_disk_write(self):
        bodies = [[], {"prompts": []}, {"prompts": ["walk", " "]}, {"prompts": ["walk", "bad\0prompt"]}]
        bodies += [{"prompts": ["walk"], "repeat": value} for value in (True, 0, 11, 1.5)]
        bodies += [{"prompts": ["walk"] * 51, "repeat": 2},
                   {"prompts": ["walk"], "seed_mode": "unknown"},
                   {"prompts": ["walk"], "config": []},
                   {"prompts": ["walk"], "config": {"seed": -1}},
                   {"prompts": ["walk"], "config": {"steps": 0}},
                   {"prompts": ["walk"], "name": " "}]
        for body in bodies:
            with self.subTest(body=body):
                response = self.submit(body)
                self.assertEqual(response.status_code, 400, response.text)
                self.fake.build_command.assert_not_called()
                self.assertEqual(self.mod.jobs, {})
                self.assertEqual(self.mod.batches, {})
                self.assertTrue(self.mod.pending.empty())
                self.assertEqual(list(self.mod.DATA.iterdir()), [])

    def test_builder_failure_does_not_commit_partial_batch_or_restore_orphans(self):
        def fail_second(directory, params, input_path):
            if self.build_calls:
                raise RuntimeError("simulated disk/adapter failure")
            return self.build(directory, params, input_path)
        self.fake.build_command.side_effect = fail_second
        response = self.submit()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(self.mod.jobs, {})
        self.assertEqual(self.mod.batches, {})
        self.assertTrue(self.mod.pending.empty())
        self.assertEqual(list((self.mod.DATA / "batches").glob("*.json")), [])
        # A saved child without a committed manifest must never run after restart.
        os.environ["MOTION_MODE"] = "hosted"
        self.mod.restore_jobs()
        self.assertEqual(self.mod.jobs, {})
        self.assertTrue(self.mod.pending.empty())

    def test_queue_capacity_rejects_entire_batch_and_preserves_existing_queue(self):
        self.mod.pending = queue.Queue(maxsize=3)
        accepted = self.batch()
        self.fake.build_command.reset_mock()
        rejected = self.submit()
        self.assertEqual(rejected.status_code, 429)
        self.fake.build_command.assert_not_called()
        self.assertEqual(self.mod.pending.qsize(), 2)
        self.assertEqual(set(self.mod.jobs), {j["id"] for j in accepted["jobs"]})
        self.assertEqual(set(self.mod.batches), {accepted["id"]})

    def test_active_item_limit_is_per_owner(self):
        with mock.patch.object(self.mod, "MAX_BATCH_ITEMS", 4):
            self.batch({"prompts": ["walk"], "repeat": 3})
            self.assertEqual(self.submit().status_code, 429)
            outsider = self.new_client()
            response = self.submit(client=outsider)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(outsider.get("/api/jobs").json()), 2)
            self.assertEqual(len(self.client.get("/api/jobs").json()), 3)

    def test_batch_and_child_endpoints_are_private_and_summaries_hide_internal_fields(self):
        result = self.batch()
        summaries = self.client.get("/api/batches").json()
        self.assertEqual(len(summaries), 1)
        self.assertNotIn("jobs", summaries[0])
        self.assertFalse(any(key.startswith("_") for key in result))
        self.assertTrue(all(not any(key.startswith("_") for key in job) for job in result["jobs"]))
        outsider = self.new_client()
        self.assertEqual(outsider.get("/api/batches").json(), [])
        self.assertEqual(outsider.get("/api/jobs").json(), [])
        for resource in (f"batches/{result['id']}", f"jobs/{result['jobs'][0]['id']}"):
            for suffix, method in (("", "get"), ("/cancel", "post"), ("/download", "get")):
                with self.subTest(resource=resource, suffix=suffix):
                    self.assertEqual(getattr(outsider, method)(f"/api/{resource}{suffix}").status_code, 404)

    def test_download_contains_only_complete_items_and_refreshes_after_more_finish(self):
        result = self.batch({"prompts": ["walk"], "repeat": 5})
        tasks = [self.mod.jobs[item["id"]] for item in result["jobs"]]
        for task, status in zip(tasks, ("complete", "failed", "cancelled", "running", "queued")):
            folder = Path(task["_dir"])
            (folder / "output").mkdir()
            (folder / "output" / "motion.npz").write_bytes(task["id"].encode())
            (folder / "input").mkdir()
            (folder / "input" / "private.json").write_text('{}')
            task["status"] = status
            self.mod.persist(task)
        response = self.client.get(f"/api/batches/{result['id']}/download")
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            manifest = json.loads(bundle.read("batch.json"))
            self.assertEqual([item["job_id"] for item in manifest], [tasks[0]["id"]])
            self.assertEqual(manifest[0]["seed"], tasks[0]["config"]["seed"])
            self.assertEqual(bundle.read(manifest[0]["folder"] + "/output/motion.npz"), tasks[0]["id"].encode())
            self.assertFalse(any("input/" in name or name.endswith("task.json") or ".owner" in name for name in bundle.namelist()))
            self.assertTrue(all(name == "batch.json" or name.startswith(manifest[0]["folder"] + "/") for name in bundle.namelist()))
        tasks[4]["status"] = "complete"
        self.mod.persist(tasks[4])
        updated = self.client.get(f"/api/batches/{result['id']}/download")
        with zipfile.ZipFile(io.BytesIO(updated.content)) as bundle:
            self.assertEqual([item["job_id"] for item in json.loads(bundle.read("batch.json"))], [tasks[0]["id"], tasks[4]["id"]])

    def test_batch_without_completed_items_has_no_download(self):
        result = self.batch()
        self.assertEqual(self.client.get(f"/api/batches/{result['id']}/download").status_code, 409)
        self.assertEqual(list((self.mod.DATA / "batches").glob("*.zip")), [])

    def test_remote_recovery_signed_urls_are_private_in_snapshots_files_and_archives(self):
        result = self.batch()
        task = self.mod.jobs[result["jobs"][0]["id"]]
        directory = Path(task["_dir"])
        secret_url = "https://example.invalid/result?signature=private-test-signature"
        (directory / "remote-recovery.json").write_text(json.dumps({"url": secret_url}))
        (directory / "output").mkdir()
        (directory / "output" / "motion.npz").write_bytes(b"public motion artifact")
        task["status"] = "complete"
        self.mod.persist(task)
        for endpoint in ("/api/jobs", f"/api/jobs/{task['id']}", f"/api/batches/{result['id']}"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("remote-recovery.json", response.text)
            self.assertNotIn("private-test-signature", response.text)
        self.assertEqual(self.client.get(f"/api/jobs/{task['id']}/files/remote-recovery.json").status_code, 404)
        for endpoint in (f"/api/jobs/{task['id']}/download", f"/api/batches/{result['id']}/download"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
                self.assertFalse(any(name.endswith("remote-recovery.json") for name in bundle.namelist()))
                self.assertTrue(any(name.endswith("output/motion.npz") for name in bundle.namelist()))
                self.assertFalse(any(b"private-test-signature" in bundle.read(name) for name in bundle.namelist()))

    def watch_once(self):
        async def one_tick(_seconds):
            self.mod.stopping.set()
        self.mod.stopping.clear()
        with mock.patch.object(self.mod.asyncio, "sleep", side_effect=one_tick):
            asyncio.run(self.mod.watch_clients(self.mod.app))

    def close_lease(self, client):
        client_id = "a" * 32
        self.assertEqual(client.post(f"/api/client/{client_id}/open").status_code, 200)
        self.assertEqual(client.post(f"/api/client/{client_id}/close").status_code, 200)
        self.mod.clients[client_id]["closed"] = time.monotonic() - self.mod.CLOSE_GRACE - 1
        self.mod.app.state.exit_on_close = True
        callback = mock.Mock()
        self.mod.app.state.shutdown_callback = callback
        self.watch_once()
        return callback

    def test_hosted_browser_close_does_not_cancel_jobs_or_stop_service(self):
        client = self.hosted()
        result = self.batch(client=client)
        callback = self.close_lease(client)
        self.assertEqual([self.mod.jobs[j["id"]]["status"] for j in result["jobs"]], ["queued", "queued"])
        callback.assert_not_called()
        self.assertEqual(self.mod.clients, {})
        runtime = client.get("/api/status").json()["runtime"]
        self.assertEqual(runtime["mode"], "hosted")
        self.assertFalse(runtime["cancel_on_close"])

    def test_local_browser_close_still_cancels_batch_and_stops_service(self):
        result = self.batch()
        callback = self.close_lease(self.client)
        self.assertEqual([self.mod.jobs[j["id"]]["status"] for j in result["jobs"]], ["cancelled", "cancelled"])
        callback.assert_called_once_with()
        self.assertTrue(self.client.get("/api/status").json()["runtime"]["cancel_on_close"])

    def test_hosted_requires_configured_basic_auth_for_page_api_and_assets(self):
        os.environ["MOTION_MODE"] = "hosted"
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        authenticated = self.hosted()
        for path in ("/", "/api/jobs", "/api/batches", "/static/app.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertIn("Basic", response.headers["www-authenticate"])
        for value in ("Basic !!!", "Bearer token", "Basic " + base64.b64encode(b"operator:wrong").decode()):
            self.assertEqual(self.client.get("/api/status", headers={"Authorization": value}).status_code, 401)
        self.assertEqual(authenticated.get("/api/status").status_code, 200)
        self.fake.build_command.assert_not_called()

    def test_same_hosted_account_shares_history_across_browser_cookies(self):
        first = self.hosted()
        result = self.batch(client=first)
        second = self.new_client(auth=("operator", "test password"))
        self.assertEqual(second.get(f"/api/batches/{result['id']}").status_code, 200)
        self.assertNotEqual(first.cookies.get("motion_session"), second.cookies.get("motion_session"))
        self.assertEqual(len(second.get("/api/batches").json()), 1)
        self.assertEqual({j["id"] for j in second.get("/api/jobs").json()}, {j["id"] for j in result["jobs"]})
        # Changing the configured account must not grant it the previous owner's history.
        different = self.hosted(username="different")
        self.assertEqual(different.get("/api/batches").json(), [])
        self.assertEqual(different.get(f"/api/batches/{result['id']}").status_code, 404)

    def test_hosted_restart_restores_queued_in_order_but_never_replays_running(self):
        client = self.hosted()
        result = self.batch({"prompts": ["walk"], "repeat": 4}, client=client)
        ids = [job["id"] for job in result["jobs"]]
        for job_id, status in zip(ids, ("queued", "running", "queued", "complete")):
            self.mod.jobs[job_id]["status"] = status
            self.mod.persist(self.mod.jobs[job_id])
        self.clear_memory()
        with mock.patch.object(self.mod.subprocess, "Popen") as process:
            self.mod.restore_jobs()
        process.assert_not_called()
        self.assertEqual([job["id"] for job in list(self.mod.pending.queue)], [ids[0], ids[2]])
        self.assertEqual([self.mod.jobs[job_id]["status"] for job_id in ids], ["queued", "failed", "queued", "complete"])
        self.assertNotIn("_command", self.mod.jobs[ids[0]])
        self.assertEqual(json.loads((self.mod.DATA / ids[1] / "task.json").read_text(encoding="utf-8"))["status"], "failed")
        restored = client.get(f"/api/batches/{result['id']}")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["counts"], {"queued": 2, "running": 0, "complete": 1, "failed": 1, "cancelled": 0})
        # A queued item can rebuild only its invocation without recreating request.json.
        resumed = self.mod.pending.get_nowait()
        self.mod.execute(resumed)
        self.fake.resume_command.assert_called_once_with(Path(resumed["_dir"]), resumed["config"])
        self.assertEqual(resumed["status"], "complete")

    def test_hosted_restart_does_not_execute_queued_item_on_different_backend(self):
        client = self.hosted()
        result = self.batch(client=client)
        self.clear_memory()
        os.environ["MOTION_BACKEND"] = "fc"
        self.mod.restore_jobs()
        self.assertTrue(self.mod.pending.empty())
        self.assertEqual([self.mod.jobs[j["id"]]["status"] for j in result["jobs"]], ["cancelled", "cancelled"])

    def test_fc_cancellation_preserves_running_call_and_downloads_its_result(self):
        os.environ["MOTION_BACKEND"] = "fc"
        result = self.batch()
        running = self.mod.jobs[result["jobs"][0]["id"]]
        queued = self.mod.jobs[result["jobs"][1]["id"]]
        release = Path(running["_dir"]) / "release"
        output = Path(running["_dir"]) / "output"
        code = "from pathlib import Path; import sys,time; gate=Path(sys.argv[1]); print('ready',flush=True)\nwhile not gate.exists(): time.sleep(.01)\np=Path(sys.argv[2]); p.mkdir(); (p/'motion.npz').write_bytes(b'completed after cancel'); print('done',flush=True)"
        running["_command"] = [sys.executable, "-u", "-c", code, str(release), str(output)]
        thread = threading.Thread(target=self.mod.execute, args=(running,))
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while "ready" not in running["log"] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn("ready", running["log"])
            process = running["_process"]
            with mock.patch.object(self.mod, "kill_tree", wraps=self.mod.kill_tree) as kill:
                response = self.client.post(f"/api/jobs/{running['id']}/cancel")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "running")
                self.assertTrue(response.json()["cancel_requested"])
                self.assertEqual(queued["status"], "queued")
                response = self.client.post(f"/api/batches/{result['id']}/cancel")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(queued["status"], "cancelled")
                self.assertTrue(all(call.args[0] is None for call in kill.call_args_list))
                self.assertIsNone(process.poll())
            release.write_text("continue")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(running["status"], "complete")
            self.assertEqual((output / "motion.npz").read_bytes(), b"completed after cancel")
            self.assertEqual(self.client.get(f"/api/batches/{result['id']}/download").status_code, 200)
            with mock.patch.object(self.mod.subprocess, "Popen") as launch:
                self.mod.execute(queued)
            launch.assert_not_called()
        finally:
            release.write_text("continue")
            thread.join(timeout=5)
            if thread.is_alive():
                self.mod.kill_tree(running.get("_process"))
                thread.join(timeout=5)

    def test_fc_batch_cancel_during_prelaunch_never_starts_any_child(self):
        os.environ["MOTION_BACKEND"] = "fc"
        result = self.batch()
        starting = self.mod.jobs[result["jobs"][0]["id"]]
        queued = self.mod.jobs[result["jobs"][1]["id"]]
        del starting["_command"]
        preparing, release = threading.Event(), threading.Event()
        def paused_resume(directory, config):
            preparing.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test did not release the prelaunch barrier")
            return self.command(directory)
        self.fake.resume_command.side_effect = paused_resume
        with mock.patch.object(self.mod.subprocess, "Popen") as launch:
            thread = threading.Thread(target=self.mod.execute, args=(starting,))
            thread.start()
            try:
                self.assertTrue(preparing.wait(timeout=5))
                self.assertEqual(starting["status"], "running")
                self.assertNotIn("_process", starting)
                response = self.client.post(f"/api/batches/{result['id']}/cancel")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["counts"]["cancelled"], 2)
            finally:
                release.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.mod.execute(queued)
            launch.assert_not_called()
        self.assertEqual(starting["status"], "cancelled")
        self.assertEqual(json.loads((Path(starting["_dir"]) / "task.json").read_text(encoding="utf-8"))["status"], "cancelled")

    def test_showcase_rejects_batches_before_backend_checks(self):
        self.mod.SHOWCASE = True
        self.assertEqual(self.submit().status_code, 409)
        self.fake.inspect_backend.assert_not_called()
        self.fake.build_command.assert_not_called()

    def test_runtime_reports_reverse_proxy_prefix(self):
        client = self.new_client(root_path="/motion")
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["runtime"]["root_path"], "/motion")


if __name__ == "__main__":
    unittest.main()
