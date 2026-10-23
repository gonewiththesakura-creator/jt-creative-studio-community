"""Small authenticated shell around JT's existing Images API workflow.

This is a separate review candidate, not an in-place production migration.
Only public serializers cross the HTTP boundary. Provider keys never enter jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.cookies
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field

ROOT = Path(__file__).resolve().parent.parent
COOKIE = "jt_session"
MAX_BODY = 64 * 1024
ACTIVE = {"queued", "running", "uncertain"}
PUBLIC_FIELDS = (
    "id", "status", "created", "finished", "prompt", "negative_prompt", "size",
    "quality", "fit", "model", "saved", "request_id", "error", "acknowledged", "stage", "metadata",
)


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def canonical_origin(value):
    """Use the same lowercased host/default-port form as a browser Origin."""
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        if ":" in hostname:
            ipaddress.IPv6Address(hostname)
        elif not re.fullmatch(r"[a-z0-9.-]+", hostname):
            raise ValueError()
        local = hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme not in {"http", "https"} or not hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or (port is not None and not 1 <= port <= 65535)
                or (parsed.scheme != "https" and not local)):
            raise ValueError()
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
            host += f":{port}"
        return f"{parsed.scheme}://{host}"
    except (ValueError, UnicodeError, TypeError):
        raise ValueError("JT_PUBLIC_URL must be a valid HTTPS origin (HTTP only on loopback)") from None


@dataclass(frozen=True)
class Settings:
    access_token: str = field(repr=False)
    data_dir: Path
    api_key: str = field(default="", repr=False)
    endpoint: str = ""
    model: str = ""
    bind: str = "127.0.0.1"
    port: int = 8189
    public_url: str = ""
    provider_timeout: int = 180
    max_jobs: int = 10000

    def validate(self):
        from PIL import __version__ as pillow_version
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", pillow_version)
        if not match or tuple(map(int, match.groups())) < (13, 0, 0):
            raise ValueError("Pillow 13.0.0 or newer is required; install the pinned requirements before starting")
        if len(self.access_token) < 32 or self.access_token.lower().startswith(("replace", "change", "example")):
            raise ValueError("JT_ACCESS_TOKEN must be a newly generated secret of at least 32 characters")
        if not 0 <= self.port <= 65535:
            raise ValueError("JT_PORT is invalid")
        if not 10 <= self.provider_timeout <= 600:
            raise ValueError("JT_PROVIDER_TIMEOUT must be between 10 and 600 seconds")
        if self.bind not in {"127.0.0.1", "localhost", "::1"} and not self.public_url:
            raise ValueError("Non-loopback binding requires JT_PUBLIC_URL and an HTTPS reverse proxy")
        if self.public_url:
            canonical_origin(self.public_url)
        if self.endpoint or self.api_key or self.model:
            if not (self.endpoint and self.api_key and self.model):
                raise ValueError("Configure JT_IMAGE_ENDPOINT, JT_IMAGE_API_KEY and JT_IMAGE_MODEL together")
            from .provider import ProviderConfig
            ProviderConfig(endpoint=self.endpoint, api_key=self.api_key, model=self.model,
                           timeout=self.provider_timeout)
        resolved = self.data_dir.resolve()
        if resolved == ROOT or ROOT / "static" == resolved or ROOT / "static" in resolved.parents:
            raise ValueError("JT_DATA_DIR cannot be the application root or a static directory")

    @classmethod
    def from_env(cls):
        return cls(
            access_token=os.environ.get("JT_ACCESS_TOKEN", ""),
            data_dir=Path(os.environ.get("JT_DATA_DIR", str(ROOT / "data"))),
            api_key=os.environ.get("JT_IMAGE_API_KEY", ""),
            endpoint=os.environ.get("JT_IMAGE_ENDPOINT", ""),
            model=os.environ.get("JT_IMAGE_MODEL", ""),
            bind=os.environ.get("JT_BIND", "127.0.0.1"),
            port=int(os.environ.get("JT_PORT", "8189")),
            public_url=os.environ.get("JT_PUBLIC_URL", "").rstrip("/"),
            provider_timeout=int(os.environ.get("JT_PROVIDER_TIMEOUT", "180")),
        )


class Store:
    """One process, one SQLite ledger, durable idempotency across restarts."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.images = self.root / "images"
        self.images.mkdir(mode=0o700, exist_ok=True)
        self.lockfile = (self.root / ".instance.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lockfile.seek(0)
                self.lockfile.write(b"0")
                self.lockfile.flush()
                self.lockfile.seek(0)
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.lockfile.close()
            raise ValueError("Another JT instance is already using this data directory") from error
        self.lock = threading.RLock()
        try:
            self.db = sqlite3.connect(self.root / "jobs.sqlite3", check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL)")
            self.db.commit()
            for job in self.all():
                if job["status"] in {"queued", "running"}:
                    job.update(status="uncertain", error="服务曾中断，无法确认上游是否已收费。请先核对供应商记录，切勿直接重生成。", stage="interrupted")
                    self.update(job)
        except Exception:
            self.lockfile.close()
            raise

    def close(self):
        self.db.close()
        self.lockfile.close()

    def all(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM jobs ORDER BY rowid DESC")]

    def get(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def update(self, job):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job["id"]))


def public_job(job):
    value = {key: job.get(key) for key in PUBLIC_FIELDS}
    value["result_url"] = f"/api/jobs/{job['id']}/image" if job["status"] == "succeeded" else None
    value["export_url"] = f"/api/jobs/{job['id']}/export"
    return value


class Application:
    def __init__(self, settings: Settings, generator=None):
        settings.validate()
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.sessions = {}
        self.session_lock = threading.Lock()
        self.login_attempts = []
        self.workers = set()
        self.closed = False
        self.closing = False
        if generator is None:
            from .provider import generate_image
            generator = generate_image
        self.generator = generator

    def login(self, token):
        now = time.monotonic()
        with self.session_lock:
            self.login_attempts = [t for t in self.login_attempts if t > now - 60]
            if len(self.login_attempts) >= 10:
                raise APIError(429, "登录尝试过于频繁，请一分钟后再试。")
            self.login_attempts.append(now)
            valid = isinstance(token, str) and hmac.compare_digest(token.encode(), self.settings.access_token.encode())
            if not valid:
                raise APIError(401, "访问口令不正确。")
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            if len(self.sessions) >= 64:
                self.sessions.pop(next(iter(self.sessions)))
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(session.encode()).hexdigest()] = {"csrf": csrf, "expires": now + 43200}
            return session, csrf

    def session(self, cookie):
        try:
            jar = http.cookies.SimpleCookie(cookie or "")
            raw = jar[COOKIE].value
        except (KeyError, http.cookies.CookieError):
            raise APIError(401, "请先登录。")
        key = hashlib.sha256(raw.encode()).hexdigest()
        with self.session_lock:
            value = self.sessions.get(key)
            if not value or value["expires"] <= time.monotonic():
                self.sessions.pop(key, None)
                raise APIError(401, "登录已过期，请重新登录。")
            return key, value

    def submit(self, body):
        allowed = {"request_id", "prompt", "negative_prompt", "size", "quality", "fit"}
        if set(body) - allowed:
            raise APIError(400, "请求包含不支持的字段。")
        request_id = body.get("request_id", "")
        try:
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except ValueError:
            raise APIError(400, "request_id 必须是标准 UUID。")
        payload = {}
        for key, limit in (("prompt", 8000), ("negative_prompt", 4000)):
            value = body.get(key, "")
            if not isinstance(value, str) or len(value) > limit:
                raise APIError(400, f"{key} 过长或格式不正确。")
            payload[key] = value.strip()
        if not any(char.isalnum() for char in payload["prompt"]):
            raise APIError(400, "请填写创作内容。")
        for key, default, choices in (
            ("size", "1024x1024", {"1024x1024", "1024x1536", "1536x1024"}),
            ("quality", "medium", {"low", "medium", "high", "auto"}),
            ("fit", "cover", {"cover", "contain"}),
        ):
            value = body.get(key, default)
            if not isinstance(value, str) or value not in choices:
                raise APIError(400, f"{key} 不受支持。")
            payload[key] = value
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.store.lock:
            if self.closing:
                raise APIError(503, "服务正在停止，暂不接收新任务。")
            existing = self.store.db.execute("SELECT fingerprint,data FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if not hmac.compare_digest(existing[0], fingerprint):
                    raise APIError(409, "同一个 request_id 不能用于不同内容。")
                return json.loads(existing[1]), True
            jobs = self.store.all()
            if any(j["status"] in ACTIVE and not j.get("acknowledged") for j in jobs):
                raise APIError(409, "已有未结束或结果未确认的任务。请等待或先核对上游记录。")
            if len(jobs) >= self.settings.max_jobs:
                raise APIError(507, "已达到本地任务数量上限，请先备份并维护数据目录。")
            if not self.settings.api_key:
                raise APIError(503, "尚未配置服务端模型 API。请按部署文档配置后重启。")
            job = dict(payload, id=uuid.uuid4().hex, request_id=request_id, status="queued", created=time.time(),
                       finished=None, model=self.settings.model, saved=False, error=None,
                       acknowledged=False, stage="queued", metadata=None)
            with self.store.db:
                self.store.db.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job["id"], request_id, fingerprint, json.dumps(job, ensure_ascii=False)))
            worker = threading.Thread(target=self.run_job, args=(job["id"],), daemon=True)
            self.workers.add(worker)
            worker.start()
            return job, False

    def run_job(self, job_id):
        from .provider import ProviderConfig, ProviderRejected
        try:
            with self.store.lock:
                job = self.store.get(job_id)
                job.update(status="running", stage="preparing")
                self.store.update(job)
            def checkpoint(stage):
                with self.store.lock:
                    current = self.store.get(job_id)
                    current["stage"] = stage
                    self.store.update(current)
            config = ProviderConfig(endpoint=self.settings.endpoint, api_key=self.settings.api_key,
                                    model=self.settings.model, timeout=self.settings.provider_timeout)
            result = self.generator(config, job, self.store.images / f"{job_id}.png", checkpoint)
            with self.store.lock:
                job = self.store.get(job_id)
                job.update(status="succeeded", stage="complete", finished=time.time(), metadata=result)
                self.store.update(job)
        except Exception as error:
            # Never expose provider bodies, URLs, credentials or local paths.
            with self.store.lock:
                job = self.store.get(job_id)
                if isinstance(error, ProviderRejected):
                    message, status = "供应商明确拒绝了请求。请检查模型、参数、额度或 API 配置。", "failed"
                else:
                    message, status = "无法确认上游最终结果，可能已收费。请先核对供应商记录，不要重复生成。", "uncertain"
                job.update(status=status, error=message, finished=time.time())
                self.store.update(job)
        finally:
            with self.store.lock:
                self.workers.discard(threading.current_thread())

    def change(self, job_id, action, body):
        with self.store.lock:
            job = self.store.get(job_id)
            if not job:
                raise APIError(404, "任务不存在。")
            if action == "save":
                if set(body) != {"saved"} or not isinstance(body["saved"], bool):
                    raise APIError(400, "saved 必须为布尔值。")
                if job["status"] != "succeeded":
                    raise APIError(409, "任务完成后才能收藏。")
                job["saved"] = body["saved"]
            else:
                if set(body) != {"confirmed"} or body["confirmed"] is not True or job["status"] != "uncertain":
                    raise APIError(409, "仅能人工确认结果未明的任务。")
                job["acknowledged"] = True
            self.store.update(job)
            return job

    def close(self):
        # Do not close a database while an accepted billable task still runs.
        with self.store.lock:
            self.closing = True
            workers = list(self.workers)
        for worker in workers:
            worker.join(self.settings.provider_timeout + 70)
        if any(worker.is_alive() for worker in workers):
            return False
        self.closed = True
        self.store.close()
        return True


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    def __init__(self, settings, generator=None):
        self.app = Application(settings, generator)
        self.capacity = threading.BoundedSemaphore(16)
        try:
            if ":" in settings.bind:
                self.address_family = socket.AF_INET6
            super().__init__((settings.bind, settings.port), Handler)
        except Exception:
            self.app.close()
            raise
        host, port = self.server_address[:2]
        host = f"[{host}]" if ":" in host else host
        self.origin = canonical_origin(settings.public_url) if settings.public_url else f"http://{host}:{port}"
        self.allowed_host = urllib.parse.urlsplit(self.origin).netloc.lower()
        self.secure_cookie = self.origin.startswith("https://")

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            request.settimeout(0.2)
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.capacity.release()

    def handle_error(self, request, client_address):
        # http.server's default traceback can include request/user data.
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "JTCommunity"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *args):
        pass  # No prompt, secret, request path or signed URL logging.

    def respond(self, status, data, content_type="application/json; charset=utf-8", headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def validate_host(self):
        if self.headers.get_all("Host", []) != [self.server.allowed_host]:
            raise APIError(403, "Host 不匹配，请使用配置的访问地址。")

    def body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
            raise APIError(400, "必须使用单一 Content-Length。")
        length = int(lengths[0]) if len(lengths[0]) <= 8 else MAX_BODY + 1
        if length > MAX_BODY:
            raise APIError(413, "请求内容过大。")
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "仅支持 application/json。")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise APIError(400, "请求内容不完整。")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, "JSON 格式不正确。")
        if not isinstance(value, dict):
            raise APIError(400, "请求内容必须为对象。")
        return value

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, mutate):
        try:
            self.validate_host()
            path = urllib.parse.urlsplit(self.path).path
            if not mutate and path == "/LICENSE":
                return self.respond(200, (ROOT / "LICENSE").read_bytes(), "text/plain; charset=utf-8")
            if not mutate and path in {"/", "/app.js", "/style.css"}:
                name, ctype = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}[path]
                return self.respond(200, (ROOT / "static" / name).read_bytes(), ctype)
            if not mutate and path == "/api/health":
                return self.respond(200, {"ok": True})
            if mutate:
                if self.headers.get_all("Origin", []) != [self.server.origin]:
                    raise APIError(403, "不接受跨来源请求。")
                body = self.body()
            if mutate and path == "/api/login":
                if set(body) != {"token"}:
                    raise APIError(400, "登录请求格式不正确。")
                token, csrf = self.server.app.login(body["token"])
                cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"
                if self.server.secure_cookie:
                    cookie += "; Secure"
                return self.respond(200, {"authenticated": True, "csrf_token": csrf}, headers={"Set-Cookie": cookie})
            session_key, session = self.server.app.session(self.headers.get("Cookie"))
            if mutate and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise APIError(403, "CSRF 校验失败。请刷新页面后重试。")
            app = self.server.app
            if not mutate and path == "/api/session":
                return self.respond(200, {"authenticated": True, "csrf_token": session["csrf"], "provider": {"model": app.settings.model, "configured": bool(app.settings.api_key)}})
            if mutate and path == "/api/logout":
                with app.session_lock:
                    app.sessions.pop(session_key, None)
                return self.respond(200, {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            if path == "/api/jobs":
                if mutate:
                    job, duplicate = app.submit(body)
                    return self.respond(200 if duplicate else 202, {"job": public_job(job), "deduplicated": duplicate})
                return self.respond(200, {"jobs": [public_job(j) for j in app.store.all()[:100]], "limit": 100})
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})(?:/(image|export|save|acknowledge))?", path)
            if not match:
                raise APIError(404, "页面或接口不存在。")
            job_id, action = match.groups()
            job = app.store.get(job_id)
            if not job:
                raise APIError(404, "任务不存在。")
            if mutate and action in {"save", "acknowledge"}:
                return self.respond(200, {"job": public_job(app.change(job_id, action, body))})
            if mutate:
                raise APIError(405, "不支持此操作。")
            if action is None:
                return self.respond(200, {"job": public_job(job)})
            if action == "export":
                return self.respond(200, {"format": "jt-community-job-v1", "job": public_job(job)}, headers={"Content-Disposition": f'attachment; filename="jt-{job_id}.json"'})
            if action == "image" and job["status"] == "succeeded":
                image_path = app.store.images / f"{job_id}.png"
                if image_path.is_symlink():
                    raise APIError(404, "图片不可用。")
                if image_path.stat().st_size > 40 * 1024 * 1024:
                    raise APIError(500, "图片超过安全大小限制。")
                data = image_path.read_bytes()
                disposition = "attachment" if urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("download") == ["1"] else "inline"
                return self.respond(200, data, "image/png", {"Content-Disposition": f'{disposition}; filename="jt-{job_id}.png"'})
            raise APIError(404, "结果尚不可用。")
        except APIError as error:
            self.respond(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.respond(500, {"error": "本地服务发生错误，请检查磁盘空间、配置和数据目录。"})


def main():
    parser = argparse.ArgumentParser(description="JT Community single-owner self-hosted review candidate")
    parser.parse_args()
    os.umask(0o077)
    try:
        server = Server(Settings.from_env())
    except (ValueError, OSError) as error:
        # Configuration validation errors are intentionally value-free.
        raise SystemExit(str(error))
    print(f"JT Community listening at {server.origin}; single-user mode; no automatic billable retries.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.app.close()


if __name__ == "__main__":
    main()
