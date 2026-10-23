"""Static UI safety checks and optional real-browser flow tests, with no paid provider calls.

The browser tests are opt-in and require Playwright and a local Chromium.
Run: python -m unittest tests.test_ui_contract -v
Browser: JT_BROWSER_TESTS=1 python -m unittest tests.test_ui_contract -v
Optional executable override: JT_CHROMIUM_EXECUTABLE=/path/to/chromium
"""
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class StaticUIContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "static" / "index.html").read_text()
        self.js = (ROOT / "static" / "app.js").read_text()
        self.elements = Elements()
        self.elements.feed(self.html)

    def test_csp_compatible_same_origin_assets(self):
        self.assertIn('lang="zh-CN"', self.html)
        self.assertIn("JT 灵感工作台", self.html)
        scripts = [attrs for tag, attrs in self.elements.tags if tag == "script"]
        self.assertEqual(scripts, [{"src": "/app.js", "defer": None}])
        self.assertIn(("link", {"rel": "stylesheet", "href": "/style.css"}), self.elements.tags)
        for _, attrs in self.elements.tags:
            self.assertNotIn("style", attrs)
            self.assertFalse(any(key.startswith("on") for key in attrs))
        self.assertNotIn("innerHTML", self.js)
        self.assertNotIn("insertAdjacentHTML", self.js)
        self.assertNotIn("localStorage", self.js)

    def test_controls_have_expected_contract(self):
        attrs_by_id = {attrs["id"]: attrs for _, attrs in self.elements.tags if "id" in attrs}
        self.assertEqual(attrs_by_id["token"]["type"], "password")
        self.assertEqual(attrs_by_id["token"]["autocomplete"], "current-password")
        sizes = [attrs["value"] for tag, attrs in self.elements.tags if tag == "input" and attrs.get("name") == "size"]
        self.assertEqual(sizes, ["1024x1024", "1024x1536", "1536x1024"])
        for control in ["prompt", "negative-prompt", "quality", "fit", "generate-button", "retry-button", "pending-panel"]:
            self.assertIn(control, attrs_by_id)
        self.assertIn('headers["X-CSRF-Token"] = state.csrf', self.js)
        self.assertIn('credentials: "same-origin"', self.js)
        self.assertIn('body: { confirmed: true }', self.js)
        self.assertIn('submit(state.pending)', self.js)

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_javascript_syntax(self):
        subprocess.run(["node", "--check", str(ROOT / "static" / "app.js")], check=True, capture_output=True)


    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_state_transitions_without_browser(self):
        result = subprocess.run(
            ["node", "-", str(ROOT / "static" / "app.js")],
            input=NODE_STATE_HARNESS, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("9 state scenarios passed", result.stdout)


BROWSER = os.environ.get("JT_CHROMIUM_EXECUTABLE") or shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


@unittest.skipUnless(os.environ.get("JT_BROWSER_TESTS") == "1", "Browser tests are opt-in: set JT_BROWSER_TESTS=1")
class BrowserFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BROWSER or not HAS_PLAYWRIGHT:
            raise RuntimeError("JT_BROWSER_TESTS=1 requires Playwright and Chromium; optionally set JT_CHROMIUM_EXECUTABLE")
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(executable_path=BROWSER, headless=True, args=["--no-sandbox"])
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        from jt_community.app import Server, Settings
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.mode = "success"
        self.delay = 0
        self.server = Server(Settings(
            access_token="ui-test-only-access-token-not-for-deployment-000000",
            data_dir=Path(self.tmp.name), port=0,
            endpoint="https://images.example.invalid/v1/images/generations",
            api_key="ui-test-only-provider-key", model="fixture-image-model",
        ), self.generator)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.server.origin)
        self.page.locator("#token").fill("ui-test-only-access-token-not-for-deployment-000000")
        self.page.locator("#login-button").click()
        self.page.wait_for_function("!document.querySelector('#generate-button').disabled")

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)
        self.server.app.close()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def generator(self, config, body, output, checkpoint):
        from PIL import Image
        from jt_community.provider import ProviderUncertain
        self.calls.append(dict(body))
        checkpoint("request_started")
        time.sleep(self.delay)
        if self.mode == "uncertain":
            raise ProviderUncertain("fixture uncertain response", code="network")
        checkpoint("result_received")
        Image.new("RGB", (1024, 1024), "#91b8a2").save(output, "PNG")
        return {"width": 1024, "height": 1024, "fixture": True}

    def generate(self, prompt="安静的绿色书店，午后阳光"):
        self.page.locator("#prompt").fill(prompt)
        self.page.locator("#generate-button").click()

    def wait_success(self):
        self.page.locator(".result-image").wait_for(timeout=10000)

    def test_complete_flow_parameters_download_save_reload_logout(self):
        self.page.locator(".negative-details summary").click()
        self.page.locator("#negative-prompt").fill("文字，水印")
        self.page.locator('input[value="1024x1536"]').check(force=True)
        self.page.locator("#quality").select_option("high")
        self.page.locator("#fit").select_option("contain")
        self.generate()
        self.wait_success()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["size"], "1024x1536")
        self.assertEqual(self.calls[0]["quality"], "high")
        self.assertEqual(self.calls[0]["fit"], "contain")
        self.assertEqual(self.calls[0]["negative_prompt"], "文字，水印")
        self.page.get_by_role("button", name="☆ 收藏作品").click()
        self.page.get_by_role("button", name="★ 已收藏").wait_for()
        with self.page.expect_download() as png:
            self.page.get_by_role("link", name="下载 PNG").click()
        self.assertTrue(png.value.suggested_filename.endswith(".png"))
        self.assertEqual(Path(png.value.path()).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        with self.page.expect_download() as export:
            self.page.get_by_role("link", name="导出 JSON").click()
        data = json.loads(Path(export.value.path()).read_text())
        self.assertIn("安静的绿色书店", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("ui-test-only-provider-key", json.dumps(data))
        self.page.reload()
        self.page.get_by_role("button", name="★ 已收藏").wait_for()
        self.page.locator("#favorites-button").click()
        self.assertEqual(self.page.locator(".history-card").count(), 1)
        self.assertEqual(self.page.evaluate("Object.keys(sessionStorage)"), [])
        self.assertEqual(self.page.evaluate("document.cookie"), "")
        self.page.locator("#logout-button").click()
        self.page.locator("#auth-view").wait_for()
        self.assertEqual(self.page.evaluate("Object.keys(sessionStorage)"), [])
        self.assertEqual(self.page.locator("#token").input_value(), "")

    def test_network_ambiguity_retains_original_id_without_auto_retry(self):
        posts = []
        def abort_post(route):
            if route.request.method == "POST":
                posts.append(route.request.post_data_json)
                route.abort()
            else:
                route.continue_()
        self.page.route("**/api/jobs", abort_post)
        self.generate("需要保持原样的描述")
        self.page.locator("#pending-panel").wait_for()
        self.page.wait_for_function("!document.querySelector('#retry-button').disabled")
        self.assertTrue(self.page.locator("#generate-button").is_disabled())
        self.assertTrue(self.page.locator("#prompt").is_disabled())
        saved = self.page.evaluate("JSON.parse(sessionStorage.getItem('jt-community.pending-request.v1'))")
        self.assertEqual(set(saved), {"request_id", "prompt", "negative_prompt", "size", "quality", "fit"})
        self.page.reload()
        self.page.wait_for_function("!document.querySelector('#retry-button').disabled")
        self.assertEqual(self.page.locator("#prompt").input_value(), "需要保持原样的描述")
        self.assertEqual(len(posts), 1)
        self.page.unroute("**/api/jobs", abort_post)
        retried = []
        self.page.on("request", lambda request: retried.append(request.post_data_json) if request.method == "POST" and request.url.endswith("/api/jobs") else None)
        self.page.locator("#retry-button").click()
        self.wait_success()
        self.assertEqual(retried, [saved])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.page.locator("#pending-panel").is_hidden())

    def test_lost_accepted_response_recovers_from_server_history(self):
        submissions = []
        def lose_response(route):
            if route.request.method == "POST":
                submissions.append(route.request.post_data_json)
                route.fetch()
                route.abort()
            else:
                route.continue_()
        self.page.route("**/api/jobs", lose_response)
        self.generate()
        self.wait_success()
        self.assertEqual(len(submissions), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.page.locator("#pending-panel").is_hidden())
        self.assertIn("找回", self.page.locator("#notice").inner_text())

    def test_uncertain_requires_explicit_ack_and_never_retries(self):
        self.mode = "uncertain"
        self.generate()
        self.page.get_by_text("这次生成，需要你核对一下", exact=True).wait_for(timeout=10000)
        self.assertTrue(self.page.locator("#generate-button").is_disabled())
        ack = self.page.get_by_role("button", name="确认已核对，允许新的创作")
        self.assertTrue(ack.is_disabled())
        self.page.reload()
        self.page.locator(".ack-check input").wait_for()
        self.assertFalse(self.page.locator(".ack-check input").is_checked())
        self.assertEqual(len(self.calls), 1)
        self.page.locator(".ack-check input").check()
        ack.click()
        self.page.wait_for_function("!document.querySelector('#generate-button').disabled")
        self.page.reload()
        self.page.wait_for_function("!document.querySelector('#generate-button').disabled")
        self.assertIn("已核对", self.page.locator("#result-badge").inner_text())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.server.app.store.all()[0]["status"], "uncertain")

    def test_duplicate_click_guard_and_safe_text_rendering(self):
        self.delay = .4
        prompt = '一只猫 <img src=x onerror="window.pwned=true">'
        self.page.locator("#prompt").fill(prompt)
        self.page.locator("#generate-button").evaluate("button => {button.click(); button.click();}")
        self.wait_success()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.page.locator(".result-prompt").inner_text(), prompt)
        self.assertIsNone(self.page.evaluate("window.pwned"))
        self.assertEqual(self.page.locator('img[src="x"]').count(), 0)

    def test_mobile_layout_has_no_horizontal_overflow(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.generate("a" * 4000)
        self.wait_success()
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.page.locator("#logout-button").click()
        self.page.locator("#auth-view").wait_for()
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))



NODE_STATE_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const source = fs.readFileSync(process.argv[2], 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {};
    this.dataset = {}; this.className = ''; this._text = ''; this.value = ''; this.disabled = false; this.hidden = false;
    this.classList = {
      add: name => { this.className += ' ' + name; },
      toggle: (name, enabled) => {
        const words = this.className.split(/\s+/).filter(word => word && word !== name);
        if (enabled) words.push(name); this.className = words.join(' ');
      },
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  append(...items) { for (const item of items) { this.children.push(item); item.parent = this; } }
  replaceChildren(...items) { this.children = []; this._text = ''; this.append(...items); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(event, handler) { (this.listeners[event] ||= []).push(handler); }
  async emit(event) { await Promise.all((this.listeners[event] || []).map(handler => handler({ preventDefault() {} }))); }
  focus() {} scrollIntoView() {}
}
async function settle() { for (let index = 0; index < 16; index++) await new Promise(resolve => setImmediate(resolve)); }
function descendants(root, predicate) {
  let found = [];
  for (const child of root.children) { if (predicate(child)) found.push(child); found.push(...descendants(child, predicate)); }
  return found;
}
async function harness(options = {}) {
  const ids = new Map(), storage = options.storage || new Map(), timers = new Map();
  let timerId = 0;
  const get = id => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
  const radios = ['1024x1024', '1024x1536', '1536x1024'].map((value, i) => Object.assign(new Element('input'), { value, checked: i === 0 }));
  const fields = [get('prompt'), get('negative-prompt'), get('quality'), get('fit'), ...radios];
  get('quality').value = 'auto'; get('fit').value = 'cover';
  get('creator-form').querySelectorAll = () => fields;
  get('creator-form').reset = () => { get('prompt').value = ''; get('negative-prompt').value = ''; get('quality').value = 'auto'; get('fit').value = 'cover'; };
  const server = { jobs: options.jobs || [], calls: [], posts: [], mode: options.mode || 'success', status: options.status || 'succeeded', savedRequests: [], ackRequests: [] };
  const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => copy(data) });
  async function fetch(path, request) {
    server.calls.push({ path, request });
    const body = request.body ? JSON.parse(request.body) : null;
    if (path === '/api/session') return response({ authenticated: true, csrf_token: 'test-csrf', provider: { configured: true, model: 'fixture' } });
    if (path === '/api/jobs' && request.method === 'GET') {
      if (server.holdList) {
        server.holdList = false;
        const snapshot = copy(server.jobs);
        return await new Promise(resolve => { server.releaseList = () => resolve(response({ jobs: snapshot })); });
      }
      return response({ jobs: server.jobs });
    }
    if (path === '/api/jobs' && request.method === 'POST') {
      server.posts.push(body);
      assert.equal(request.headers['X-CSRF-Token'], 'test-csrf');
      assert.equal(request.credentials, 'same-origin');
      if (server.mode === 'not-admitted') throw new Error('network');
      if (server.mode === 'invalid') return response({ error: 'invalid prompt' }, 400);
      const old = server.jobs.find(job => job.request_id === body.request_id);
      if (old) return response({ job: old, deduplicated: true });
      const job = { ...body, id: 'job' + (server.jobs.length + 1), status: server.status, created: 1780000000, saved: false, acknowledged: false, model: 'fixture' };
      server.jobs.unshift(job);
      if (server.mode === 'lost-response') throw new Error('network');
      return response({ job, deduplicated: false }, 202);
    }
    if (path.endsWith('/save')) {
      server.savedRequests.push(body); const job = server.jobs.find(job => path.includes('/' + job.id + '/'));
      job.saved = body.saved; return response({ job });
    }
    if (path.endsWith('/acknowledge')) {
      server.ackRequests.push(body); const job = server.jobs.find(job => path.includes('/' + job.id + '/'));
      job.acknowledged = true; return response({ job });
    }
    if (path === '/api/logout') return response({ ok: true });
    throw new Error('Unexpected API path: ' + path);
  }
  const context = {
    document: {
      getElementById: get, createElement: tag => new Element(tag), hidden: false,
      querySelectorAll: () => radios,
      querySelector: () => radios.find(input => input.checked), addEventListener() {},
    },
    window: {
      setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
      clearTimeout: id => timers.delete(id), addEventListener() {}, matchMedia: () => ({ matches: false }),
    },
    sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    fetch, crypto, AbortController, console,
  };
  vm.runInNewContext(source, context);
  await settle();
  return {
    get, server, storage, timers,
    async submit(prompt = '一只纸雕小鸟') { get('prompt').value = prompt; await get('creator-form').emit('submit'); await settle(); },
    async click(id) { if (!get(id).disabled) await get(id).emit('click'); await settle(); },
    resultNodes(predicate) { return descendants(get('result-content'), predicate); },
    async poll() { const safe = Array.from(timers.values()).filter(timer => timer.ms !== 25000); for (const timer of safe) await timer.fn(); await settle(); },
  };
}
(async () => {
  // 1. Success, immutable payload, safe text, favorite, and authenticated download routes.
  const a = await harness();
  assert.equal(a.get('generate-button').disabled, false);
  await a.submit('一只鸟 <img src=x onerror="bad()">');
  assert.equal(a.server.posts.length, 1);
  assert.equal(a.storage.size, 0);
  assert.equal(a.get('result-content').textContent.includes('<img src=x'), true);
  assert.equal(a.resultNodes(item => item.tagName === 'img').length, 1);
  const links = a.resultNodes(item => item.tagName === 'a');
  assert.equal(links.some(link => link.href === '/api/jobs/job1/image?download=1'), true);
  assert.equal(links.some(link => link.href === '/api/jobs/job1/export'), true);
  const save = a.resultNodes(item => item.tagName === 'button')[0];
  await save.emit('click'); await settle();
  assert.equal(a.server.jobs[0].saved, true);
  assert.deepEqual(a.server.savedRequests, [{ saved: true }]);

  // 2. No-response ambiguity keeps exact parameters, no automatic POST, reload and explicit retry use same ID.
  const b = await harness({ mode: 'not-admitted' });
  await b.submit('原始描述');
  assert.equal(b.get('generate-button').disabled, true);
  assert.equal(b.get('prompt').disabled, true);
  const original = JSON.parse([...b.storage.values()][0]);
  assert.deepEqual(Object.keys(original).sort(), ['fit', 'negative_prompt', 'prompt', 'quality', 'request_id', 'size']);
  await b.poll(); assert.equal(b.server.posts.length, 1);
  const reloaded = await harness({ mode: 'not-admitted', storage: b.storage });
  assert.equal(reloaded.get('prompt').value, '原始描述');
  assert.equal(reloaded.server.posts.length, 0);
  reloaded.server.mode = 'success'; await reloaded.click('retry-button');
  assert.deepEqual(reloaded.server.posts, [original]);
  assert.equal(reloaded.storage.size, 0);

  // 3. An admitted request with a lost response is reconciled, never re-submitted.
  const c = await harness({ mode: 'lost-response' }); await c.submit();
  assert.equal(c.server.posts.length, 1); assert.equal(c.storage.size, 0);
  assert.equal(c.get('notice').textContent.includes('找回'), true);

  // 4. Explicit rejection frees the form without reusing an invalid request.
  const d = await harness({ mode: 'invalid' }); await d.submit();
  assert.equal(d.storage.size, 0); assert.equal(d.get('generate-button').disabled, false);
  assert.equal(d.server.posts.length, 1);

  // 5. Running state fences duplicate UI submissions; safe polling never generates.
  const e = await harness({ status: 'running' });
  e.get('prompt').value = '两个点击'; await Promise.all([e.get('creator-form').emit('submit'), e.get('creator-form').emit('submit')]); await settle();
  assert.equal(e.server.posts.length, 1); assert.equal(e.get('generate-button').disabled, true);
  await e.poll(); assert.equal(e.server.posts.length, 1);

  // 6. Uncertainty cannot unlock automatically; acknowledgment preserves history and never POSTs a job.
  const f = await harness({ status: 'uncertain' }); await f.submit();
  assert.equal(f.get('generate-button').disabled, true);
  await f.poll(); assert.equal(f.get('generate-button').disabled, true);
  const ackButton = f.resultNodes(item => item.tagName === 'button')[0];
  const checkbox = f.resultNodes(item => item.tagName === 'input')[0];
  assert.equal(ackButton.disabled, true);
  await ackButton.emit('click'); await settle(); assert.equal(f.server.ackRequests.length, 0);
  checkbox.checked = true; await checkbox.emit('change');
  assert.equal(ackButton.disabled, false);
  await ackButton.emit('click'); await settle();
  assert.deepEqual(f.server.ackRequests, [{ confirmed: true }]);
  assert.equal(f.server.jobs[0].status, 'uncertain'); assert.equal(f.server.posts.length, 1);
  assert.equal(f.get('generate-button').disabled, false);

  // 7. Logout clears recoverable prompt payload and form, never stores access credentials.
  const g = await harness({ mode: 'not-admitted' }); await g.submit('应清除的描述');
  assert.equal(g.storage.size, 1); await g.click('logout-button');
  assert.equal(g.storage.size, 0); assert.equal(g.get('prompt').value, ''); assert.equal(g.get('workspace').hidden, true);

  // 8. Acknowledgment is retained across reload, old uncertain job is still visible.
  const h = await harness({ jobs: copy(f.server.jobs) });
  assert.equal(h.get('generate-button').disabled, false);
  assert.equal(h.get('result-badge').textContent.includes('已核对'), true);
  assert.equal(h.server.posts.length, 0);
  // 9. A pre-mutation history response cannot erase a newer accepted task.
  const i = await harness(); i.server.holdList = true;
  const staleRefresh = i.get('refresh-button').emit('click'); await settle();
  await i.submit('不能被旧记录覆盖');
  assert.equal(i.get('result-content').textContent.includes('不能被旧记录覆盖'), true);
  i.server.releaseList(); await staleRefresh; await settle();
  assert.equal(i.get('result-content').textContent.includes('不能被旧记录覆盖'), true);
  assert.equal(i.server.posts.length, 1);
  console.log('9 state scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""

if __name__ == "__main__":
    unittest.main()
