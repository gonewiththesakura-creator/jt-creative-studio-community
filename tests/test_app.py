import http.client
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from PIL import Image

from jt_community.app import Application, APIError, Server, Settings, Store, canonical_origin, public_job
from jt_community.provider import ProviderRejected, ProviderUncertain

FIXTURE_TOKEN = "test-only-access-token-not-for-deployment-000000"


def settings(path, **kwargs):
    values = dict(access_token=FIXTURE_TOKEN, data_dir=Path(path), port=0,
                  endpoint="https://images.example.invalid/v1/images/generations",
                  api_key="test-only-provider-key", model="fixture-image-model")
    values.update(kwargs)
    return Settings(**values)


def payload(**kwargs):
    return dict(request_id=str(uuid.uuid4()), prompt="一只纸雕小鸟", negative_prompt="水印",
                size="1024x1024", quality="low", fit="cover", **kwargs)


def fake_generator(config, body, output, checkpoint):
    checkpoint("request_started")
    checkpoint("result_received")
    Image.new("RGB", (1024, 1024), "teal").save(output, "PNG")
    return {"width": 1024, "height": 1024, "prompt_compacted": False}


def terminal(app, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = app.store.get(job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(.01)
    raise AssertionError("fixture task did not complete")


class StoreAndApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(settings(self.tmp.name), fake_generator)

    def tearDown(self):
        if not self.app.closed:
            self.app.close()
        self.tmp.cleanup()

    def test_complete_save_and_restart_workflow(self):
        body = payload()
        job, duplicate = self.app.submit(body)
        self.assertFalse(duplicate)
        done = terminal(self.app, job["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertTrue((self.app.store.images / f"{job['id']}.png").exists())
        self.app.change(job["id"], "save", {"saved": True})
        self.app.close()
        self.app = Application(settings(self.tmp.name), lambda *_: self.fail("must not re-submit after restart"))
        duplicate, deduped = self.app.submit(body)
        self.assertTrue(deduped)
        self.assertTrue(duplicate["saved"])
        self.assertEqual(duplicate["id"], job["id"])

    def test_idempotency_conflict_does_not_dispatch(self):
        body = payload()
        job, _ = self.app.submit(body)
        with self.assertRaises(APIError) as caught:
            self.app.submit(body | {"prompt": "different"})
        self.assertEqual(caught.exception.status, 409)
        terminal(self.app, job["id"])

    def test_simultaneous_duplicate_is_one_generation(self):
        calls = []
        def generate(*args):
            calls.append(1)
            time.sleep(.1)
            return fake_generator(*args)
        self.app.generator = generate
        body = payload()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.app.submit(body))) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        terminal(self.app, results[0][0]["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len({j["id"] for j, _ in results}), 1)

    def test_uncertainty_blocks_new_task_until_manual_ack(self):
        calls = []
        def uncertain(*args):
            calls.append(1)
            args[-1]("request_started")
            raise ProviderUncertain("safe", code="network")
        self.app.generator = uncertain
        body = payload()
        job, _ = self.app.submit(body)
        self.assertEqual(terminal(self.app, job["id"])["status"], "uncertain")
        with self.assertRaises(APIError): self.app.submit(payload())
        self.app.submit(body)
        self.assertEqual(len(calls), 1)
        self.app.change(job["id"], "acknowledge", {"confirmed": True})
        self.app.generator = fake_generator
        new, _ = self.app.submit(payload())
        self.assertEqual(terminal(self.app, new["id"])["status"], "succeeded")
        self.assertEqual(self.app.store.get(job["id"])["status"], "uncertain")

    def test_interrupted_task_stays_fenced_after_restart(self):
        job, _ = self.app.submit(payload())
        done = terminal(self.app, job["id"])
        done.update(status="running", stage="request_started")
        self.app.store.update(done)
        self.app.close()
        self.app = Application(settings(self.tmp.name), fake_generator)
        self.assertEqual(self.app.store.get(job["id"])["status"], "uncertain")
        with self.assertRaises(APIError): self.app.submit(payload())

    def test_explicit_rejection_is_terminal_but_not_retried(self):
        def reject(*args):
            raise ProviderRejected("safe", code="http_rejected", status=429)
        self.app.generator = reject
        job, _ = self.app.submit(payload())
        self.assertEqual(terminal(self.app, job["id"])["status"], "failed")

    def test_second_instance_refused_without_changing_ledger(self):
        with self.assertRaises(ValueError): Store(Path(self.tmp.name))

    def test_provider_secrets_do_not_enter_ledger_or_serializer(self):
        job, _ = self.app.submit(payload())
        done = terminal(self.app, job["id"])
        text = json.dumps(public_job(done))
        self.assertNotIn("test-only-provider-key", text)
        self.assertNotIn(FIXTURE_TOKEN, text)
        self.assertNotIn("example.invalid", text)
        self.assertNotIn("test-only-provider-key", (Path(self.tmp.name) / "jobs.sqlite3").read_bytes().decode("latin1"))

    def test_input_boundaries(self):
        for changed in [{"prompt": ""}, {"prompt": "x" * 8001}, {"size": "1x1"},
                        {"quality": []}, {"fit": "bad"}, {"request_id": "../../secret"},
                        {"endpoint": "https://evil.example"}, {"prompt": {"x": 1}}]:
            with self.subTest(changed=changed), self.assertRaises(APIError):
                self.app.submit(payload() | changed)

    def test_missing_provider_configuration_is_safe(self):
        self.app.close()
        self.app = Application(settings(self.tmp.name, endpoint="", api_key="", model=""), fake_generator)
        with self.assertRaises(APIError) as caught: self.app.submit(payload())
        self.assertEqual(caught.exception.status, 503)

    def test_settings_reject_unsafe_defaults(self):
        for changed in [dict(access_token="short"), dict(bind="0.0.0.0"),
                        dict(public_url="http://public.example"), dict(public_url="https://public.example/path"),
                        dict(endpoint="http://images.example/v1/images/generations"), dict(provider_timeout=1)]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                settings(self.tmp.name, **changed).validate()

    def test_settings_repr_does_not_leak_secrets(self):
        displayed = repr(settings(self.tmp.name))
        self.assertNotIn(FIXTURE_TOKEN, displayed)
        self.assertNotIn("test-only-provider-key", displayed)

    def test_canonical_browser_origins(self):
        self.assertEqual(canonical_origin("https://JT.EXAMPLE.invalid:443/"), "https://jt.example.invalid")
        self.assertEqual(canonical_origin("http://LOCALHOST:80"), "http://localhost")
        self.assertEqual(canonical_origin("https://[::1]:8443"), "https://[::1]:8443")
        for value in ["https://example.invalid:bad", "https://example.invalid:0", "https://example.invalid\n", "https://user:pass@example.invalid"]:
            with self.subTest(value=value), self.assertRaises(ValueError): canonical_origin(value)

    def test_shutdown_admission_fence(self):
        self.app.closing = True
        with self.assertRaises(APIError) as caught: self.app.submit(payload())
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.app.store.all(), [])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = Server(settings(self.tmp.name), fake_generator)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app.close()
        self.thread.join()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address[:2], timeout=3)
        merged = {"Origin": self.server.origin, "Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        if body is not None:
            body = json.dumps(body).encode()
            merged["Content-Type"] = "application/json"
        merged.update(headers or {})
        conn.request(method, path, body=body, headers=merged)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        conn.close()
        return result

    def login(self):
        status, headers, body = self.call("POST", "/api/login", {"token": FIXTURE_TOKEN})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = json.loads(body)["csrf_token"]
        return headers

    def test_full_http_workflow(self):
        headers = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        status, _, raw = self.call("POST", "/api/jobs", payload())
        self.assertEqual(status, 202)
        job_id = json.loads(raw)["job"]["id"]
        terminal(self.server.app, job_id)
        status, _, raw = self.call("POST", f"/api/jobs/{job_id}/save", {"saved": True})
        self.assertTrue(json.loads(raw)["job"]["saved"])
        status, headers, raw = self.call("GET", f"/api/jobs/{job_id}/image?download=1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment"))
        with Image.open(io.BytesIO(raw)) as result: self.assertEqual(result.size, (1024, 1024))
        status, headers, raw = self.call("GET", f"/api/jobs/{job_id}/export")
        self.assertEqual(json.loads(raw)["format"], "jt-community-job-v1")
        self.assertTrue(headers["Content-Disposition"].startswith("attachment"))
        self.assertEqual(self.call("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.call("GET", "/api/jobs")[0], 401)

    def test_http_workflow_through_real_adapter_with_mock_upstream_only(self):
        from jt_community.provider import generate_image
        self.login()
        self.server.app.generator = generate_image
        source = io.BytesIO()
        Image.new("RGB", (64, 96), "#649897").save(source, "PNG")
        envelope = {"data": [{"b64_json": base64.b64encode(source.getvalue()).decode()}]}
        with patch("jt_community.provider._post_json", return_value=envelope) as upstream:
            status, _, raw = self.call("POST", "/api/jobs", payload() | {"size": "1024x1536"})
            self.assertEqual(status, 202)
            job_id = json.loads(raw)["job"]["id"]
            job = terminal(self.server.app, job_id)
            self.assertEqual(job["status"], "succeeded")
            self.assertTrue(job["metadata"]["geometry_normalized"])
            self.assertEqual(upstream.call_count, 1)
            self.assertIn("Required canvas", upstream.call_args[0][1]["prompt"])
        status, _, raw = self.call("GET", f"/api/jobs/{job_id}/image")
        self.assertEqual(status, 200)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.size, (1024, 1536))

    def test_all_private_reads_require_login(self):
        for path in ["/api/session", "/api/jobs", "/api/jobs/" + "a" * 32,
                     "/api/jobs/" + "a" * 32 + "/image", "/api/jobs/" + "a" * 32 + "/export"]:
            self.assertEqual(self.call("GET", path)[0], 401)

    def test_health_does_not_expose_upstream(self):
        status, _, body = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_project_license_is_readable_without_exposing_source_files(self):
        status, headers, body = self.call("GET", "/LICENSE")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertIn(b"GNU AFFERO GENERAL PUBLIC LICENSE", body)
        self.assertEqual(self.call("GET", "/requirements.txt")[0], 401)

    def test_csrf_and_cross_origin_blocked(self):
        self.login()
        self.assertEqual(self.call("POST", "/api/jobs", payload(), {"X-CSRF-Token": "bad"})[0], 403)
        self.assertEqual(self.call("POST", "/api/jobs", payload(), {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/login", {"token": FIXTURE_TOKEN}, {"Origin": "null"})[0], 403)

    def test_dns_rebinding_host_blocked(self):
        self.assertEqual(self.call("GET", "/api/health", headers={"Host": "evil.example"})[0], 403)

    def test_query_token_is_not_authentication(self):
        self.assertEqual(self.call("GET", "/api/jobs?token=" + FIXTURE_TOKEN)[0], 401)

    def test_static_allowlist_blocks_source_data_and_traversal(self):
        self.login()
        for path in ["/../jt_community/app.py", "/%2e%2e/data/jobs.sqlite3", "/data/jobs.sqlite3", "/.env", "/static/../jt_community/app.py"]:
            self.assertEqual(self.call("GET", path)[0], 404)

    def test_security_headers_on_private_response(self):
        self.login()
        _, headers, _ = self.call("GET", "/api/jobs")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_rejected_login_is_rate_limited(self):
        for _ in range(10):
            self.assertEqual(self.call("POST", "/api/login", {"token": "wrong"})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"token": "wrong"})[0], 429)

    def test_non_object_json_rejected(self):
        self.assertEqual(self.call("POST", "/api/login", ["invalid"])[0], 400)


class CLITests(unittest.TestCase):
    def test_old_pillow_is_refused_before_startup(self):
        with tempfile.TemporaryDirectory() as data, patch("PIL.__version__", "12.3.0"):
            with self.assertRaisesRegex(ValueError, "Pillow 13.0.0"):
                settings(data).validate()

    def test_real_entrypoint_starts_without_upstream_probe(self):
        with tempfile.TemporaryDirectory() as data:
            env = os.environ.copy()
            for key in list(env):
                if key.startswith("JT_"):
                    env.pop(key)
            env.update(JT_ACCESS_TOKEN=FIXTURE_TOKEN, JT_PORT="0", JT_DATA_DIR=data,
                       JT_BIND="127.0.0.1", PYTHONUNBUFFERED="1")
            process = subprocess.Popen([os.sys.executable, "-m", "jt_community.app"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=env)
            try:
                line = process.stdout.readline()
                self.assertIn("JT Community listening at http://127.0.0.1:", line)
                self.assertNotIn(FIXTURE_TOKEN, line)
                port = int(line.split("127.0.0.1:")[1].split(";")[0])
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", "/api/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"ok": True})
                connection.close()
            finally:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
