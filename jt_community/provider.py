"""Single-request, server-configured Images API adapter.

Adapted from JT server.py (compact_dreamapi_prompts, build_dreamapi_input,
_dreamapi_request_json, _dreamapi_download_image, dreamapi_run_image), reviewed
source commit 294bf689662fec2583a439bcf9f5a841cb8eff07. Attribution records
provenance only; this module does not grant rights to the original project.

An exception after dispatch NEVER authorizes another generation. The caller
must persist request_started before this module sends the billable POST.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import warnings

MAX_RESPONSE_BYTES = 96 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_PIXELS = 32 * 1024 * 1024
SIZES = {"1024x1024": (1024, 1024), "1024x1536": (1024, 1536), "1536x1024": (1536, 1024)}
QUALITIES = frozenset({"low", "medium", "high", "auto"})


class ProviderError(RuntimeError):
    """Safe-to-display error. No upstream body, URL, key or prompt is included."""

    def __init__(self, message: str, *, code: str, result_received: bool = False,
                 status: int | None = None):
        super().__init__(message)
        self.code = code
        self.result_received = result_received
        self.status = status


class ProviderRejected(ProviderError):
    """The provider explicitly rejected this request with 4xx other than 408."""


class ProviderUncertain(ProviderError):
    """Generation may have happened. Never automatically submit it again."""


@dataclass(frozen=True)
class ProviderConfig:
    endpoint: str
    api_key: str = field(repr=False)
    model: str
    timeout: float = 120
    max_prompt_chars: int = 1600

    def __post_init__(self):
        try:
            parsed = urllib.parse.urlsplit(self.endpoint)
            port = parsed.port
        except (TypeError, ValueError):
            raise ValueError("Provider endpoint must be a valid HTTPS Images API URL") from None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path != "/v1/images/generations"
                or any(ord(char) <= 32 or ord(char) == 127 for char in self.endpoint)
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("Provider endpoint must be an HTTPS /v1/images/generations URL")
        if (not isinstance(self.api_key, str) or not self.api_key.strip()
                or len(self.api_key) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in self.api_key)):
            raise ValueError("A valid server-side provider API key is required")
        if not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", self.model):
            raise ValueError("A valid provider model is required")
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or not 0 < self.timeout <= 3600):
            raise ValueError("Provider timeout must be between 0 and 3600 seconds")
        if (isinstance(self.max_prompt_chars, bool) or not isinstance(self.max_prompt_chars, int)
                or not 256 <= self.max_prompt_chars <= 12000):
            raise ValueError("Provider prompt budget must be between 256 and 12000 characters")


def compact_dreamapi_prompts(positive, negative, max_total=1600):
    """Deterministically compact, preserving the first/highest-priority phrases."""
    positive, negative = str(positive or "").strip(), str(negative or "").strip()
    if len(positive) + len(negative) <= max_total:
        return positive, negative, False

    def phrases(text):
        seen, rows = set(), []
        for part in re.split(r"[,\n]+", text):
            item = re.sub(r"\s+", " ", part).strip(" ,")
            if item and item.casefold() not in seen:
                rows.append(item)
                seen.add(item.casefold())
        return rows

    def fit(rows, budget):
        kept, used = [], 0
        for item in rows:
            extra = len(item) + (2 if kept else 0)
            if used + extra > budget:
                if not kept and budget > 0:
                    kept.append(item[:budget].rstrip())
                break
            kept.append(item)
            used += extra
        return ", ".join(kept)

    negative_budget = min(max_total // 4, max(200, len(negative)))
    positive_out = fit(phrases(positive), max_total - negative_budget)
    negative_out = fit(phrases(negative), max_total - len(positive_out))
    return positive_out, negative_out, True


def build_dreamapi_input(positive, negative, size, orientation, max_total=1600, final_size=None):
    """Bound the complete upstream prompt, including the canvas instructions."""
    prefix = [f"Required canvas: exactly {size}, {orientation} composition."]
    if final_size and final_size != size:
        prefix.append(f"Compose for a centered final crop to {final_size}; keep every important subject inside that narrower safe area.")
    else:
        prefix.append("Keep the important subject inside the center safe area so a final crop will not cut it off.")
    overhead = len("\n".join(prefix)) + 1 + (len("\nAvoid: ") if str(negative or "").strip() else 0)
    positive_out, negative_out, compacted = compact_dreamapi_prompts(
        positive, negative, max(1, max_total - overhead))
    if not any(char.isalnum() for char in positive_out):
        raise ValueError("Subject prompt must contain readable text")
    text = "\n".join([*prefix, positive_out] + (["Avoid: " + negative_out] if negative_out else []))
    if len(text) > max_total:
        raise ValueError("Provider prompt budget is too small")
    return text, compacted, len(positive_out)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Provider operation deadline exceeded")
    return remaining


def _within_deadline(operation, deadline):
    """Bound DNS, connection, body reads and decoding by one shared deadline.

    A timed-out worker may still finish DNS/connect/read I/O. The authenticated
    transport separately checks this same deadline after connect and before
    every socket write, so a late connection cannot dispatch a billable POST.
    Workers have no filesystem/ledger side effects; keep the uncertainty fence.
    """
    mailbox = queue.Queue(maxsize=1)

    def worker():
        try:
            mailbox.put((True, operation()))
        except BaseException as error:
            mailbox.put((False, error))

    timeout = _remaining(deadline)
    threading.Thread(target=worker, daemon=True).start()
    try:
        success, result = mailbox.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("Provider operation deadline exceeded") from None
    _remaining(deadline)
    if not success:
        raise result
    return result


def _read_limited(response, maximum):
    chunks, size = [], 0
    while True:
        chunk = response.read(min(65536, maximum + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise ValueError("Provider response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


class _NoAuthenticatedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """Prevent a blocked connection from dispatching after caller timeout.

    A thread join timeout alone cannot cancel urllib. In particular, DNS or
    connect can finish after the caller has returned. Check after that blocking
    work, before *each* headers/body write, and between writes. Socket sendall
    also uses only the original deadline's remaining time, never a fresh cap.
    """

    def __init__(self, *args, deadline, **kwargs):
        self._deadline = deadline
        super().__init__(*args, **kwargs)

    def _check_deadline(self):
        try:
            return _remaining(self._deadline)
        except TimeoutError:
            self.close()
            raise

    def connect(self):
        self._check_deadline()
        try:
            super().connect()
            remaining = self._check_deadline()
            self.sock.settimeout(remaining)
        except BaseException:
            self.close()
            raise

    def send(self, data):
        self._check_deadline()
        try:
            if self.sock is None:
                if not self.auto_open:
                    raise http.client.NotConnected()
                self.connect()
            # This adapter sends only already-bounded bytes, including proxy
            # CONNECT headers. Reject streaming iterables rather than allowing
            # http.client to perform multiple unchecked sendall operations.
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise TypeError("Provider transport requires bounded bytes")
            self.sock.settimeout(self._check_deadline())
            self.sock.sendall(data)
            self._check_deadline()
        except BaseException:
            self.close()
            raise

    def getresponse(self):
        remaining = self._check_deadline()
        if self.sock is not None:
            self.sock.settimeout(remaining)
        return super().getresponse()


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self._deadline = deadline

    def https_open(self, request):
        def connection(host, **kwargs):
            return _DeadlineHTTPSConnection(host, deadline=self._deadline, **kwargs)
        return self.do_open(connection, request, context=self._context)


def _post_json(config, body, deadline):
    # No retry handler and no redirects: never forward the Bearer key to a
    # second endpoint, and never repeat a potentially billable POST.
    opener = urllib.request.build_opener(_NoAuthenticatedRedirects(), _DeadlineHTTPSHandler(deadline))
    request = urllib.request.Request(config.endpoint, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + config.api_key,
                 "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with opener.open(request, timeout=_remaining(deadline)) as response:
            if not 200 <= response.status < 300:
                raise ValueError("Unexpected provider response status")
            raw = _read_limited(response, MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        # Do not read arbitrary error bodies (which can drip forever or contain
        # secrets), but do close the response/socket before carrying its status.
        error.close()
        raise
    return json.loads(raw.decode("utf-8"))


def _download_public_image(url, deadline):
    """Credential-free HTTPS GET with a freshly checked, pinned IP at every hop."""
    target = url
    for _ in range(4):
        parsed = urllib.parse.urlsplit(target)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port not in {None, 443}
                or any(ord(char) <= 32 or ord(char) == 127 for char in target)):
            raise ValueError("Unsafe image location")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        ips = [row[4][0] for row in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError("Unsafe image destination")
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=_remaining(deadline),
                                                context=ssl.create_default_context())
        pinned_ip = ips[0]
        connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
            (pinned_ip, 443), timeout, source_address)
        try:
            connection.request("GET", urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                               headers={"Accept": "image/*"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Invalid image redirect")
                target = urllib.parse.urljoin(target, location)
                continue
            if response.status != 200:
                raise ValueError("Image download was rejected")
            return _read_limited(response, MAX_IMAGE_BYTES)
        finally:
            connection.close()
    raise ValueError("Too many image redirects")


def _normalize_image(source, size, fit):
    from PIL import Image, ImageOps
    if not source or len(source) > MAX_IMAGE_BYTES:
        raise ValueError("Invalid image size")
    # Treat Pillow's own decompression-bomb signal as a hard validation error.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        # Reject unrelated formats before invoking their parsers, not just
        # after Image.open auto-detection. This is defense in depth, not a
        # substitute for running the patched Pillow version in requirements.
        with Image.open(io.BytesIO(source), formats=["PNG", "JPEG", "WEBP"]) as decoded:
            if (decoded.format not in {"PNG", "JPEG", "WEBP"} or decoded.width <= 0 or decoded.height <= 0
                    or decoded.width * decoded.height > MAX_SOURCE_PIXELS):
                raise ValueError("Unsupported image data or dimensions")
            decoded.load()
            target = SIZES[size]
            source_size = f"{decoded.width}x{decoded.height}"
            geometry_normalized = decoded.size != target
            # Always reencode: exclude private/provider-added metadata from the
            # downloadable artifact instead of preserving arbitrary PNG chunks.
            converted = decoded.convert("RGB")
            if geometry_normalized and fit == "cover":
                output = ImageOps.fit(converted, target, method=Image.Resampling.LANCZOS, centering=(.5, .5))
            elif geometry_normalized:
                contained = ImageOps.contain(converted, target, method=Image.Resampling.LANCZOS)
                output = Image.new("RGB", target, "white")
                output.paste(contained, ((target[0] - contained.width) // 2, (target[1] - contained.height) // 2))
            else:
                output = converted
            output.info.clear()
            result = io.BytesIO()
            output.save(result, "PNG", optimize=True)
            return result.getvalue(), {"source_size": source_size, "output_size": size,
                "width": target[0], "height": target[1], "geometry_normalized": geometry_normalized,
                "image_reencoded": True}


def generate_image(config: ProviderConfig, payload: dict, output_path, checkpoint: Callable[[str], None]):
    """Send one generation, validate and atomically save one normalized PNG.

    Config comes only from the server environment. Extra payload fields cannot
    override endpoint, key or model. `checkpoint` must durably record phases.
    A result_received error means a response arrived but no usable local result
    was saved; the upstream generation must not be submitted again.
    """
    if not isinstance(config, ProviderConfig) or not isinstance(payload, dict):
        raise ValueError("Invalid provider configuration or input")
    prompt = payload.get("prompt", "")
    negative = payload.get("negative_prompt", "")
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000
            or not isinstance(negative, str) or len(negative) > 12000):
        raise ValueError("Prompts must be text, with a nonempty subject, at most 12000 characters each")
    size, quality, fit = payload.get("size", "1024x1024"), payload.get("quality", "medium"), payload.get("fit", "cover")
    if (not isinstance(size, str) or size not in SIZES or not isinstance(quality, str)
            or quality not in QUALITIES or not isinstance(fit, str) or fit not in {"cover", "contain"}):
        raise ValueError("Unsupported image size, quality or fit")
    width, height = SIZES[size]
    orientation = "square" if width == height else "landscape" if width > height else "portrait"
    upstream_prompt, compacted, _ = build_dreamapi_input(prompt, negative, size, orientation, config.max_prompt_chars)
    body = {"model": config.model, "prompt": upstream_prompt, "size": size, "quality": quality,
            "n": 1, "output_format": "png"}
    deadline = time.monotonic() + config.timeout
    # Deliberately outside the network try: a failed pre-dispatch ledger write
    # prevents the POST and does not claim a provider request took place.
    checkpoint("request_started")
    try:
        result = _within_deadline(lambda: _post_json(config, body, deadline), deadline)
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500 and error.code != 408:
            raise ProviderRejected("The image provider rejected the request. Check your configuration and request before trying again.",
                                   code="provider_rejected", status=error.code) from None
        raise ProviderUncertain("The provider response was not confirmed. Do not generate again; review the original request with your provider.",
                                code="provider_uncertain", status=error.code) from None
    except Exception:
        raise ProviderUncertain("The provider response was not confirmed. Do not generate again; review the original request with your provider.",
                                code="provider_uncertain") from None
    if (not isinstance(result, dict) or not isinstance(result.get("data"), list)
            or not result["data"] or any(not isinstance(item, dict) for item in result["data"])):
        raise ProviderUncertain("The provider returned an unusable response. Do not generate again; review the original request with your provider.",
                                code="invalid_response") from None
    image = next((item for item in result["data"] if isinstance(item.get("b64_json"), str) and item["b64_json"]), None)
    url_image = next((item for item in result["data"] if isinstance(item.get("url"), str) and item["url"]), None)
    if image is None and url_image is None:
        raise ProviderUncertain("The provider returned no usable image. Do not generate again; review the original request with your provider.",
                                code="invalid_response") from None
    try:
        checkpoint("result_received")
        if image is not None:
            encoded = image["b64_json"]
            if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
                raise ValueError("Image exceeds the byte limit")
            source = _within_deadline(lambda: base64.b64decode(encoded, validate=True), deadline)
        else:
            source = _within_deadline(lambda: _download_public_image(url_image["url"], deadline), deadline)
        encoded_png, metadata = _within_deadline(lambda: _normalize_image(source, size, fit), deadline)
        _remaining(deadline)
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".image-", suffix=".tmp", dir=destination.parent)
            with os.fdopen(fd, "wb") as output:
                output.write(encoded_png)
                output.flush()
                os.fsync(output.fileno())
            _remaining(deadline)
            os.replace(temporary, destination)
            # The image file must be durable before the app commits its
            # succeeded ledger entry. fsync(file) alone does not persist rename.
            if os.name == "posix":
                directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return {**metadata, "file": destination.name, "size_bytes": len(encoded_png),
                "model": config.model, "quality": quality, "fit": fit, "prompt_compacted": compacted}
    except Exception:
        raise ProviderUncertain("A provider result arrived but could not be saved. Do not generate again; review or recover the original result with your provider.",
                                code="result_processing_failed", result_received=True) from None
