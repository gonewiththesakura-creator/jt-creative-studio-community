"""Offline provider contracts, adapted from JT DreamAPI regression coverage.

No request reaches a real provider. Fixtures are generated in memory; all
network transports and DNS operations are mocked.
"""
import base64
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from jt_community import provider


def png(width=48, height=64, color=(20, 90, 150), metadata=False):
    out = io.BytesIO()
    info = PngInfo()
    if metadata:
        info.add_text("provider_internal", "private upstream metadata")
    Image.new("RGB", (width, height), color).save(out, "PNG", pnginfo=info)
    return out.getvalue()


def response(source=None):
    return {"data": [{"b64_json": base64.b64encode(source or png()).decode("ascii")} ]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "result.png"
        self.config = provider.ProviderConfig(
            "https://images.example.test/v1/images/generations", "offline-fixture-secret", "image-fixture")
        self.payload = {"prompt": "A blue landscape", "negative_prompt": "watermark",
                        "size": "1024x1024", "quality": "medium", "fit": "cover"}
        self.phases = []

    def generate(self, config=None, payload=None, checkpoint=None):
        return provider.generate_image(config or self.config, payload or self.payload,
                                       self.output, checkpoint or self.phases.append)

    def test_config_is_https_only_without_credential_url_or_fragments(self):
        for endpoint in (
            "http://images.example.test/v1/images/generations",
            "https://user:password@images.example.test/v1/images/generations",
            "https://images.example.test/v1/images/generations?token=x",
            "https://images.example.test/v1/images/generations#fragment",
            "https://images.example.test/v1/responses",
            "https://images.example.test/v1/images/generations\n",
            "https://images.example.test:bad/v1/images/generations",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                provider.ProviderConfig(endpoint, "fixture", "fixture")
        self.assertNotIn(self.config.api_key, repr(self.config))

    def test_config_rejects_invalid_key_model_budget_and_timeout(self):
        for change in ({"api_key": ""}, {"api_key": "secret\r\nHeader: value"}, {"model": "x\n"},
                       {"timeout": float("nan")}, {"timeout": -1}, {"timeout": True},
                       {"max_prompt_chars": 100}, {"max_prompt_chars": True}):
            args = {"endpoint": self.config.endpoint, "api_key": "fixture", "model": "fixture", **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                provider.ProviderConfig(**args)

    def test_prompt_compaction_is_bounded_and_keeps_first_phrase(self):
        positive = "Priority subject, " + ", ".join("fine detail " + str(i) for i in range(1000))
        full, compacted, count = provider.build_dreamapi_input(positive, "text, watermark" * 100,
                                                              "1024x1024", "square")
        self.assertTrue(compacted)
        self.assertIn("Priority subject", full)
        self.assertLessEqual(len(full), 1600)
        self.assertGreater(count, 0)
        self.assertIn("Required canvas: exactly 1024x1024", full)

    def test_short_prompt_is_not_compacted(self):
        self.assertEqual(provider.compact_dreamapi_prompts("blue sky", "text"), ("blue sky", "text", False))

    def test_only_supported_parsers_are_allowed_before_open(self):
        with patch.object(Image, "open", wraps=Image.open) as opened:
            provider._normalize_image(png(), "1024x1024", "cover")
        self.assertEqual(opened.call_args.kwargs["formats"], ["PNG", "JPEG", "WEBP"])

    def test_subject_that_normalizes_to_punctuation_is_rejected_before_dispatch(self):
        with patch.object(provider, "_post_json") as post, self.assertRaises(ValueError):
            self.generate(payload={**self.payload, "prompt": ",,\n --"})
        post.assert_not_called()
        self.assertEqual(self.phases, [])

    def test_invalid_payload_never_dispatches_or_checkpoints(self):
        for change in ({"prompt": ""}, {"prompt": 7}, {"prompt": "x" * 12001},
                       {"negative_prompt": []}, {"size": "1024x1000"}, {"size": {}},
                       {"quality": "max"}, {"quality": []}, {"fit": "stretch"}):
            with self.subTest(change=change), patch.object(provider, "_post_json") as post:
                with self.assertRaises(ValueError):
                    self.generate(payload={**self.payload, **change})
                post.assert_not_called()
        self.assertEqual(self.phases, [])

    def test_generation_uses_trusted_server_model_and_one_post(self):
        with patch.object(provider, "_post_json", return_value=response()) as post:
            metadata = self.generate(payload={**self.payload, "endpoint": "http://evil.invalid/",
                                               "api_key": "other", "model": "untrusted"})
        post.assert_called_once()
        config, body, deadline = post.call_args.args
        self.assertIs(config, self.config)
        self.assertEqual(body["model"], "image-fixture")
        self.assertEqual(set(body), {"model", "prompt", "size", "quality", "n", "output_format"})
        self.assertEqual(body["n"], 1)
        self.assertEqual(self.phases, ["request_started", "result_received"])
        self.assertEqual(metadata["file"], "result.png")
        self.assertNotIn(self.config.api_key, json.dumps(metadata))
        self.assertNotIn("output_path", metadata)
        self.assertGreater(metadata["size_bytes"], 0)
        with Image.open(self.output) as image:
            self.assertEqual(image.size, (1024, 1024))
            self.assertEqual(image.format, "PNG")

    def test_checkpoint_failure_prevents_billable_post(self):
        def fail(_phase):
            raise OSError("ledger unavailable")
        with patch.object(provider, "_post_json") as post, self.assertRaises(OSError):
            self.generate(checkpoint=fail)
        post.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_post_transport_disables_redirects_and_never_reads_error_body(self):
        fake_response = MagicMock()
        fake_response.status = 200
        fake_response.read.side_effect = [json.dumps(response()).encode(), b""]
        fake_response.__enter__.return_value = fake_response
        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_response
        with patch.object(provider.urllib.request, "build_opener", return_value=fake_opener) as build:
            self.generate()
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, provider._NoAuthenticatedRedirects)
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.invalid/"))
        request = fake_opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, self.config.endpoint)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.config.api_key)
        fake_opener.open.assert_called_once()

    def test_explicit_4xx_are_definite_rejections(self):
        for status in (400, 401, 403, 404, 422, 429):
            upstream = urllib.error.HTTPError(self.config.endpoint, status, self.config.api_key, {}, None)
            with self.subTest(status=status), patch.object(provider, "_post_json", side_effect=upstream) as post:
                with self.assertRaises(provider.ProviderRejected) as caught:
                    self.generate()
                self.assertEqual(caught.exception.status, status)
                self.assertFalse(caught.exception.result_received)
                self.assertNotIn(self.config.api_key, str(caught.exception))
                post.assert_called_once()
        self.assertFalse(self.output.exists())

    def test_408_5xx_and_redirects_are_uncertain_and_never_retried(self):
        for status in (301, 302, 307, 308, 408, 500, 502, 503):
            upstream = urllib.error.HTTPError(self.config.endpoint, status, self.config.api_key, {}, None)
            with self.subTest(status=status), patch.object(provider, "_post_json", side_effect=upstream) as post:
                with self.assertRaises(provider.ProviderUncertain) as caught:
                    self.generate()
                self.assertEqual(caught.exception.status, status)
                self.assertIn("Do not generate again", str(caught.exception))
                self.assertNotIn(self.config.api_key, str(caught.exception))
                post.assert_called_once()

    def test_network_errors_are_safe_uncertain_and_never_retried(self):
        for error in (TimeoutError(self.config.api_key), OSError(self.config.api_key),
                      urllib.error.URLError(self.config.api_key), ValueError(self.config.api_key)):
            with self.subTest(error=type(error)), patch.object(provider, "_post_json", side_effect=error) as post:
                with self.assertRaises(provider.ProviderUncertain) as caught:
                    self.generate()
                self.assertNotIn(self.config.api_key, str(caught.exception))
                self.assertFalse(caught.exception.result_received)
                post.assert_called_once()

    def test_malformed_json_after_post_is_uncertain(self):
        fake_response = MagicMock()
        fake_response.status = 200
        fake_response.read.side_effect = [b"{bad json", b""]
        fake_response.__enter__.return_value = fake_response
        opener = MagicMock()
        opener.open.return_value = fake_response
        with patch.object(provider.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(provider.ProviderUncertain):
                self.generate()
        opener.open.assert_called_once()
        self.assertEqual(self.phases, ["request_started"])

    def test_error_body_is_closed_without_being_read(self):
        body = MagicMock()
        error = urllib.error.HTTPError(self.config.endpoint, 403, "forbidden", {}, body)
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.object(provider.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(provider.ProviderRejected):
                self.generate()
        body.read.assert_not_called()
        body.close.assert_called_once()

    def test_response_body_read_has_hard_total_deadline(self):
        release, finished = threading.Event(), threading.Event()
        fake_response = MagicMock()
        fake_response.status = 200
        fake_response.__enter__.return_value = fake_response
        def read(_size):
            release.wait(2)
            finished.set()
            return b""
        fake_response.read.side_effect = read
        opener = MagicMock()
        opener.open.return_value = fake_response
        config = provider.ProviderConfig(self.config.endpoint, "fixture", "fixture", timeout=.03)
        started = time.monotonic()
        with patch.object(provider.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(provider.ProviderUncertain):
                self.generate(config=config)
            self.assertLess(time.monotonic() - started, .5)
            release.set()
            self.assertTrue(finished.wait(1))
        opener.open.assert_called_once()
        self.assertFalse(self.output.exists())

    def test_invalid_envelope_never_claims_result_received(self):
        for value in (None, [], {}, {"data": "wrong"}, {"data": []}, {"data": [7]}, {"data": [{}]}):
            with self.subTest(value=value), patch.object(provider, "_post_json", return_value=value):
                phases = []
                with self.assertRaises(provider.ProviderUncertain) as caught:
                    self.generate(checkpoint=phases.append)
                self.assertFalse(caught.exception.result_received)
                self.assertEqual(phases, ["request_started"])

    def test_invalid_base64_is_result_failure_not_regeneration(self):
        with patch.object(provider, "_post_json", return_value={"data": [{"b64_json": "not base64!"}]}) as post:
            with self.assertRaises(provider.ProviderUncertain) as caught:
                self.generate()
        self.assertTrue(caught.exception.result_received)
        self.assertEqual(caught.exception.code, "result_processing_failed")
        self.assertEqual(self.phases, ["request_started", "result_received"])
        post.assert_called_once()
        self.assertFalse(self.output.exists())

    def test_invalid_image_is_result_failure(self):
        with patch.object(provider, "_post_json", return_value=response(b"not an image")):
            with self.assertRaises(provider.ProviderUncertain) as caught:
                self.generate()
        self.assertTrue(caught.exception.result_received)
        self.assertFalse(self.output.exists())

    def test_metadata_is_removed_from_downloadable_png(self):
        with patch.object(provider, "_post_json", return_value=response(png(1024, 1024, metadata=True))):
            metadata = self.generate()
        self.assertFalse(metadata["geometry_normalized"])
        self.assertTrue(metadata["image_reencoded"])
        with Image.open(self.output) as image:
            self.assertNotIn("provider_internal", image.info)
        self.assertNotIn(b"private upstream metadata", self.output.read_bytes())

    def test_contain_adds_white_borders_and_cover_fills_canvas(self):
        for fit in ("contain", "cover"):
            with self.subTest(fit=fit), patch.object(provider, "_post_json", return_value=response(png(100, 50))):
                self.generate(payload={**self.payload, "fit": fit})
                with Image.open(self.output) as image:
                    self.assertEqual(image.getpixel((0, 0)), (255, 255, 255) if fit == "contain" else (20, 90, 150))
                    self.assertEqual(image.getpixel((512, 512)), (20, 90, 150))

    def test_portrait_and_landscape_preserve_exact_requested_size(self):
        for size, dimensions in provider.SIZES.items():
            with self.subTest(size=size), patch.object(provider, "_post_json", return_value=response()):
                metadata = self.generate(payload={**self.payload, "size": size})
                with Image.open(self.output) as image:
                    self.assertEqual(image.size, dimensions)
                self.assertEqual(metadata["output_size"], size)

    def test_gif_and_excessive_pixels_are_rejected(self):
        gif = io.BytesIO()
        Image.new("RGB", (10, 10)).save(gif, "GIF")
        with patch.object(provider, "_post_json", return_value=response(gif.getvalue())):
            with self.assertRaises(provider.ProviderUncertain):
                self.generate()
        with patch.object(provider, "MAX_SOURCE_PIXELS", 10), patch.object(provider, "_post_json", return_value=response()):
            with self.assertRaises(provider.ProviderUncertain):
                self.generate()
        self.assertFalse(self.output.exists())

    def test_response_bytes_are_bounded(self):
        reader = io.BytesIO(b"x" * 51)
        with self.assertRaises(ValueError):
            provider._read_limited(reader, 50)
        self.assertEqual(provider._read_limited(io.BytesIO(b"x" * 50), 50), b"x" * 50)

    def test_base64_is_rejected_before_decode_when_over_limit(self):
        with patch.object(provider, "MAX_IMAGE_BYTES", 3), patch.object(provider, "_post_json", return_value=response()):
            with patch.object(provider.base64, "b64decode") as decode:
                with self.assertRaises(provider.ProviderUncertain):
                    self.generate()
                decode.assert_not_called()

    def test_url_download_saves_one_image_without_second_post_or_url_metadata(self):
        url = "https://cdn.example.test/result.png?sig=private-url"
        with patch.object(provider, "_post_json", return_value={"data": [{"url": url}]}) as post:
            with patch.object(provider, "_download_public_image", return_value=png()) as download:
                metadata = self.generate()
        post.assert_called_once()
        download.assert_called_once()
        self.assertNotIn("private-url", json.dumps(metadata))
        self.assertEqual(self.phases, ["request_started", "result_received"])

    def test_download_failure_is_safe_and_never_regenerates(self):
        url = "https://cdn.example.test/result.png?sig=private-url"
        with patch.object(provider, "_post_json", return_value={"data": [{"url": url}]}) as post:
            with patch.object(provider, "_download_public_image", side_effect=OSError(url)):
                with self.assertRaises(provider.ProviderUncertain) as caught:
                    self.generate()
        post.assert_called_once()
        self.assertTrue(caught.exception.result_received)
        self.assertNotIn(url, str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_base64_is_preferred_over_url(self):
        answer = response()
        answer["data"].append({"url": "https://cdn.example.test/unused.png"})
        with patch.object(provider, "_post_json", return_value=answer), patch.object(provider, "_download_public_image") as download:
            self.generate()
        download.assert_not_called()

    def test_slow_post_hits_hard_deadline_and_never_writes_late(self):
        release = threading.Event()
        finished = threading.Event()
        def stall(*args):
            release.wait(2)
            finished.set()
            return response()
        config = provider.ProviderConfig(self.config.endpoint, "fixture", "fixture", timeout=.03)
        started = time.monotonic()
        with patch.object(provider, "_post_json", side_effect=stall) as post:
            with self.assertRaises(provider.ProviderUncertain):
                self.generate(config=config)
            self.assertLess(time.monotonic() - started, .5)
            release.set()
            self.assertTrue(finished.wait(1))
            post.assert_called_once()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.phases, ["request_started"])

    def test_real_urllib_cannot_dispatch_when_connect_finishes_after_timeout(self):
        # Real urllib/http.client request construction, but no real network.
        # This reproduces the late-dispatch bug that a mocked _post_json misses.
        release, entered, closed = threading.Event(), threading.Event(), threading.Event()
        writes = []
        class FakeSocket:
            def settimeout(self, _timeout):
                pass
            def sendall(self, data):
                writes.append(bytes(data))
            def makefile(self, *_args):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            def close(self):
                closed.set()
        def delayed_connect(connection):
            entered.set()
            release.wait(2)
            connection.sock = FakeSocket()
        real_build_opener = urllib.request.build_opener
        def offline_opener(*handlers):
            return real_build_opener(urllib.request.ProxyHandler({}), *handlers)
        config = provider.ProviderConfig(self.config.endpoint, "fixture", "fixture", timeout=.05)
        with patch.object(provider.urllib.request, "build_opener", side_effect=offline_opener), \
             patch.object(provider.http.client.HTTPSConnection, "connect", delayed_connect):
            try:
                with self.assertRaises(provider.ProviderUncertain):
                    self.generate(config=config)
                self.assertTrue(entered.is_set())
                self.assertEqual(writes, [])
            finally:
                release.set()
            self.assertTrue(closed.wait(1), "expired late connection was not closed")
        self.assertEqual(writes, [], "late connection dispatched billable POST bytes")
        self.assertEqual(self.phases, ["request_started"])
        self.assertFalse(self.output.exists())

    def test_real_urllib_deadline_transport_completes_one_normal_post(self):
        writes = []
        body = json.dumps(response()).encode()
        wire = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        class FakeSocket:
            def settimeout(self, _timeout):
                pass
            def sendall(self, data):
                writes.append(bytes(data))
            def makefile(self, *_args):
                return io.BytesIO(wire)
            def close(self):
                pass
        def connect(connection):
            connection.sock = FakeSocket()
        real_build_opener = urllib.request.build_opener
        def offline_opener(*handlers):
            return real_build_opener(urllib.request.ProxyHandler({}), *handlers)
        with patch.object(provider.urllib.request, "build_opener", side_effect=offline_opener), \
             patch.object(provider.http.client.HTTPSConnection, "connect", connect):
            metadata = self.generate()
        self.assertEqual(len(writes), 2)
        self.assertTrue(writes[0].startswith(b"POST /v1/images/generations HTTP/"))
        self.assertEqual(json.loads(writes[1])["n"], 1)
        self.assertTrue(self.output.exists())
        self.assertEqual(metadata["file"], "result.png")
        self.assertEqual(self.phases, ["request_started", "result_received"])

    def test_real_urllib_cannot_send_body_after_header_write_finishes_late(self):
        release, closed = threading.Event(), threading.Event()
        writes = []
        class FakeSocket:
            def settimeout(self, _timeout):
                pass
            def sendall(self, data):
                writes.append(bytes(data))
                release.wait(2)
            def makefile(self, *_args):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            def close(self):
                closed.set()
        def connect(connection):
            connection.sock = FakeSocket()
        real_build_opener = urllib.request.build_opener
        def offline_opener(*handlers):
            return real_build_opener(urllib.request.ProxyHandler({}), *handlers)
        config = provider.ProviderConfig(self.config.endpoint, "fixture", "fixture", timeout=.05)
        with patch.object(provider.urllib.request, "build_opener", side_effect=offline_opener), \
             patch.object(provider.http.client.HTTPSConnection, "connect", connect):
            try:
                with self.assertRaises(provider.ProviderUncertain):
                    self.generate(config=config)
                self.assertEqual(len(writes), 1)
                self.assertTrue(writes[0].startswith(b"POST /v1/images/generations HTTP/"))
            finally:
                release.set()
            self.assertTrue(closed.wait(1))
        self.assertEqual(len(writes), 1, "request body was dispatched after deadline")
        self.assertFalse(self.output.exists())

    def test_download_uses_remaining_total_deadline_not_a_new_timeout(self):
        seen = []
        def post(config, body, deadline):
            seen.append(deadline)
            return {"data": [{"url": "https://cdn.example.test/image.png"}]}
        def download(url, deadline):
            seen.append(deadline)
            return png()
        with patch.object(provider, "_post_json", side_effect=post), patch.object(provider, "_download_public_image", side_effect=download):
            self.generate()
        self.assertEqual(seen[0], seen[1])

    def test_failed_result_checkpoint_never_saves_or_reposts(self):
        def checkpoint(phase):
            self.phases.append(phase)
            if phase == "result_received":
                raise OSError("ledger failed")
        with patch.object(provider, "_post_json", return_value=response()) as post:
            with self.assertRaises(provider.ProviderUncertain) as caught:
                self.generate(checkpoint=checkpoint)
        self.assertTrue(caught.exception.result_received)
        post.assert_called_once()
        self.assertFalse(self.output.exists())

    def test_failed_atomic_save_preserves_existing_file_and_cleans_temporary(self):
        self.output.write_bytes(b"existing image")
        with patch.object(provider, "_post_json", return_value=response()), patch.object(provider.os, "replace", side_effect=OSError("disk failed")):
            with self.assertRaises(provider.ProviderUncertain) as caught:
                self.generate()
        self.assertTrue(caught.exception.result_received)
        self.assertEqual(self.output.read_bytes(), b"existing image")
        self.assertEqual([p.name for p in self.output.parent.iterdir()], ["result.png"])

    @unittest.skipUnless(os.name == "posix", "directory fsync is a POSIX durability guarantee")
    def test_success_syncs_file_then_rename_then_parent_directory(self):
        events = []
        real_fsync, real_replace = os.fsync, os.replace
        def fsync(fd):
            events.append("directory_sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_sync")
            return real_fsync(fd)
        def replace(source, target):
            events.append("replace")
            return real_replace(source, target)
        with patch.object(provider, "_post_json", return_value=response()), \
             patch.object(provider.os, "fsync", side_effect=fsync), \
             patch.object(provider.os, "replace", side_effect=replace):
            self.generate()
        self.assertEqual(events, ["file_sync", "replace", "directory_sync"])


class DownloadTests(unittest.TestCase):
    def public_dns(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    def test_unsafe_urls_rejected_before_dns(self):
        for url in ("http://cdn.example.test/a", "https://user:pass@cdn.example.test/a", "file:///tmp/a",
                    "https://cdn.example.test:444/a", "https://cdn.example.test/a#fragment",
                    "https://cdn.example.test/a\n"):
            with self.subTest(url=url), patch.object(provider.socket, "getaddrinfo") as dns:
                with self.assertRaises(ValueError):
                    provider._download_public_image(url, time.monotonic() + 3)
                dns.assert_not_called()

    def test_any_non_public_dns_answer_rejects_entire_destination(self):
        for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fe80::1", "::ffff:127.0.0.1"):
            with self.subTest(ip=ip), patch.object(provider.socket, "getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            ]), patch.object(provider.http.client, "HTTPSConnection") as connection:
                with self.assertRaises(ValueError):
                    provider._download_public_image("https://cdn.example.test/a", time.monotonic() + 3)
                connection.assert_not_called()

    def test_public_download_pins_validated_ip_and_sends_no_authorization(self):
        response = MagicMock()
        response.status = 200
        response.read.side_effect = [b"png bytes", b""]
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(provider.socket, "getaddrinfo", side_effect=self.public_dns), patch.object(provider.http.client, "HTTPSConnection", return_value=connection) as constructor:
            self.assertEqual(provider._download_public_image("https://cdn.example.test/a?sig=test", time.monotonic() + 3), b"png bytes")
        self.assertEqual(constructor.call_args.args[0], "cdn.example.test")
        self.assertEqual(connection.request.call_args.args, ("GET", "/a?sig=test"))
        self.assertEqual(connection.request.call_args.kwargs["headers"], {"Accept": "image/*"})
        with patch.object(provider.socket, "create_connection") as connect:
            connection._create_connection(("changed-dns.invalid", 443), 2)
        connect.assert_called_once_with(("93.184.216.34", 443), 2, None)
        connection.close.assert_called_once()

    def test_redirect_rechecks_public_dns_before_connecting(self):
        redirect = MagicMock()
        redirect.status = 302
        redirect.getheader.return_value = "https://private.example.test/secret"
        connection = MagicMock()
        connection.getresponse.return_value = redirect
        def dns(host, *args, **kwargs):
            ip = "127.0.0.1" if host == "private.example.test" else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
        with patch.object(provider.socket, "getaddrinfo", side_effect=dns), patch.object(provider.http.client, "HTTPSConnection", return_value=connection) as constructor:
            with self.assertRaises(ValueError):
                provider._download_public_image("https://cdn.example.test/a", time.monotonic() + 3)
        constructor.assert_called_once()
        connection.close.assert_called_once()

    def test_redirect_chain_has_finite_limit(self):
        redirect = MagicMock()
        redirect.status = 302
        redirect.getheader.return_value = "/again"
        connection = MagicMock()
        connection.getresponse.return_value = redirect
        with patch.object(provider.socket, "getaddrinfo", side_effect=self.public_dns), patch.object(provider.http.client, "HTTPSConnection", return_value=connection) as constructor:
            with self.assertRaises(ValueError):
                provider._download_public_image("https://cdn.example.test/a", time.monotonic() + 3)
        self.assertEqual(constructor.call_count, 4)
        self.assertEqual(connection.close.call_count, 4)


if __name__ == "__main__":
    unittest.main()
