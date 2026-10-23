/* JT Community: same-origin UI. No provider credentials or automatic paid retries. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "jt-community.pending-request.v1";
  const STATUSES = { queued: "排队中", running: "生成中", succeeded: "已完成", failed: "生成失败", uncertain: "结果待核对" };
  const QUALITY = { auto: "自动质量", low: "低质量", medium: "中等质量", high: "高质量" };
  const state = {
    authenticated: false, csrf: "", provider: {}, jobs: [], selectedId: null,
    sending: false, loading: false, saving: false, acknowledging: false,
    hydrated: false, favorites: false, pending: null, pollTimer: null,
    pollDelay: 2500, epoch: 0, jobsRevision: 0, resultSignature: "", historySignature: "", storageAvailable: true,
  };

  class APIError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 25000);
    const headers = { Accept: "application/json" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
    }
    try {
      const response = await fetch(path, {
        method: options.method || "GET", credentials: "same-origin", cache: "no-store",
        headers, signal: controller.signal,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
      let data;
      try { data = await response.json(); }
      catch (_) { throw new APIError("服务器返回了无法确认的响应，请刷新记录核对。", response.ok ? 0 : response.status); }
      if (!response.ok) {
        const message = typeof data.error === "string" ? data.error : "操作未完成，请稍后重新确认。";
        throw new APIError(message, response.status);
      }
      return data;
    } catch (error) {
      if (error instanceof APIError) throw error;
      throw new APIError("连接暂时中断，暂不能确认服务器结果。请检查网络后刷新记录。", 0);
    } finally { window.clearTimeout(timer); }
  }

  function notify(message, isError = false) {
    $("notice").textContent = message;
    $("notice").classList.toggle("error", isError);
    $("notice").hidden = !message;
  }

  function loginError(message) {
    $("login-error").textContent = message;
    $("login-error").hidden = !message;
  }

  function validPending(value) {
    return value && typeof value === "object" && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value.request_id) &&
      typeof value.prompt === "string" && value.prompt.length <= 8000 &&
      typeof value.negative_prompt === "string" && value.negative_prompt.length <= 4000 &&
      ["1024x1024", "1024x1536", "1536x1024"].includes(value.size) &&
      ["auto", "low", "medium", "high"].includes(value.quality) && ["cover", "contain"].includes(value.fit) &&
      Object.keys(value).length === 6;
  }

  function loadPending() {
    try {
      const saved = sessionStorage.getItem(STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (validPending(parsed)) state.pending = parsed;
        else sessionStorage.removeItem(STORAGE_KEY);
      }
    } catch (_) { state.storageAvailable = false; }
    if (state.pending) restoreForm(state.pending);
  }

  function savePending(payload) {
    state.pending = payload;
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload)); }
    catch (_) { state.storageAvailable = false; }
  }

  function clearPending() {
    state.pending = null;
    try { sessionStorage.removeItem(STORAGE_KEY); }
    catch (_) { state.storageAvailable = false; }
  }

  function restoreForm(payload) {
    $("prompt").value = payload.prompt;
    $("negative-prompt").value = payload.negative_prompt;
    document.querySelectorAll('input[name="size"]').forEach((input) => { input.checked = input.value === payload.size; });
    $("quality").value = payload.quality;
    $("fit").value = payload.fit;
    updatePromptCount();
  }

  function updatePromptCount() { $("prompt-count").textContent = `${$("prompt").value.length} / 4000`; }

  function authenticate(session) {
    state.epoch += 1;
    state.authenticated = true;
    state.csrf = session.csrf_token;
    state.provider = session.provider || {};
    state.hydrated = false;
    $("auth-view").hidden = true;
    $("workspace").hidden = false;
    $("logout-button").hidden = false;
    $("token").value = "";
    loginError("");
    $("provider-status").textContent = state.provider.configured ? `已配置 · ${state.provider.model || "图像服务"}` : "尚未配置图像服务";
    $("provider-dot").classList.toggle("offline", !state.provider.configured);
    updateControls();
    renderResult();
    refreshJobs();
  }

  function unauthenticate(message = "") {
    state.epoch += 1;
    state.authenticated = false;
    state.csrf = "";
    state.jobs = [];
    state.selectedId = null;
    state.hydrated = false;
    state.resultSignature = "";
    state.historySignature = "";
    window.clearTimeout(state.pollTimer);
    $("auth-view").hidden = false;
    $("workspace").hidden = true;
    $("logout-button").hidden = true;
    $("token").value = "";
    $("result-content").replaceChildren();
    $("history-list").replaceChildren();
    loginError(message);
  }

  function acknowledged(job) { return job.acknowledged === true || job.uncertainty_acknowledged === true; }
  function activeJob() { return state.jobs.find((job) => ["queued", "running"].includes(job.status) || (job.status === "uncertain" && !acknowledged(job))); }
  function validJob(job) { return job && /^[a-zA-Z0-9_-]{1,100}$/.test(job.id) && Object.hasOwn(STATUSES, job.status); }
  function jobPath(job, suffix = "") { return `/api/jobs/${encodeURIComponent(job.id)}${suffix}`; }

  function updateControls() {
    const active = activeJob();
    const locked = state.sending || !!state.pending || !!active || !state.provider.configured || !state.hydrated;
    $("generate-button").disabled = locked;
    $("logout-button").disabled = state.sending || state.acknowledging;
    $("refresh-button").disabled = state.loading;
    $("retry-button").disabled = state.sending || state.loading || !!active || !state.authenticated;
    $("pending-panel").hidden = !state.pending;
    $("pending-id").textContent = state.pending ? `请求编号 ${state.pending.request_id}` : "";
    $("creator-form").querySelectorAll("textarea, input, select").forEach((input) => { input.disabled = state.sending || !!state.pending; });
    let label = "生成图像", help = "每次生成可能产生服务费用，请确认参数后提交。";
    if (state.sending) { label = "正在提交…"; help = "正在提交一次请求，请勿重复操作。"; }
    else if (state.pending) { label = "请先核对上次提交"; help = "原始描述与参数已保留，暂不创建新任务。"; }
    else if (!state.hydrated) { label = "正在核对任务记录"; help = "确认服务器任务状态后，就可以继续创作。"; }
    else if (active) {
      label = active.status === "uncertain" ? "请先核对上游结果" : "已有任务进行中";
      help = active.status === "uncertain" ? "结果不确定不会自动解锁，请打开待核对任务查看。" : "可先构思下一张图，当前任务结束后再提交。";
    } else if (!state.provider.configured) { label = "等待配置图像服务"; help = "请由部署者在服务器配置服务地址、密钥和模型。"; }
    $("generate-label").textContent = label;
    $("generate-help").textContent = help;
    $("retry-button").textContent = state.sending ? "正在核对…" : "用原请求重新确认";
  }

  function schedulePoll() {
    window.clearTimeout(state.pollTimer);
    if (state.authenticated && (activeJob() || state.pending || !state.hydrated)) {
      state.pollTimer = window.setTimeout(refreshJobs, state.pollDelay);
    }
  }

  async function refreshJobs() {
    if (!state.authenticated || state.loading) return;
    const epoch = state.epoch;
    const revision = state.jobsRevision;
    let refreshAgain = false;
    state.loading = true;
    updateControls();
    try {
      const data = await api("/api/jobs");
      if (epoch !== state.epoch) return;
      // Ignore a list response begun before a newer successful mutation.
      if (revision !== state.jobsRevision) { refreshAgain = true; return; }
      if (!Array.isArray(data.jobs) || data.jobs.some((job) => !validJob(job))) throw new APIError("任务记录格式不正确，暂不允许新建任务。", 0);
      state.jobs = data.jobs;
      state.hydrated = true;
      state.pollDelay = 2500;
      if (state.pending) {
        const recovered = state.jobs.find((job) => job.request_id === state.pending.request_id);
        if (recovered) {
          state.selectedId = recovered.id;
          clearPending();
          notify("已在任务记录中找回上次提交，没有重复生成。");
        }
      }
      if (!state.selectedId || !state.jobs.some((job) => job.id === state.selectedId)) {
        state.selectedId = (activeJob() || state.jobs[0] || {}).id || null;
      }
      renderHistory();
      renderResult();
    } catch (error) {
      if (epoch !== state.epoch) return;
      if (error.status === 401) unauthenticate("登录已过期，请重新输入访问口令。待确认请求会保留。");
      else {
        state.hydrated = false;
        state.pollDelay = Math.min(state.pollDelay * 2, 15000);
        notify(error.message, true);
      }
    } finally {
      state.loading = false;
      updateControls();
      if (refreshAgain && state.authenticated) refreshJobs();
      else schedulePoll();
    }
  }

  function requestId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  async function submit(payload) {
    if (state.sending || !state.authenticated || activeJob()) return;
    const epoch = state.epoch;
    state.sending = true;
    savePending(payload);
    updateControls();
    notify(state.storageAvailable ? "" : "浏览器无法保留待确认请求。请勿刷新或关闭此页，先核对提交结果。", !state.storageAvailable);
    try {
      const data = await api("/api/jobs", { method: "POST", body: payload });
      if (epoch !== state.epoch) return;
      if (!validJob(data.job)) throw new APIError("提交响应不完整，请先核对任务记录。", 0);
      clearPending();
      state.jobsRevision += 1;
      state.jobs = [data.job, ...state.jobs.filter((job) => job.id !== data.job.id)];
      state.selectedId = data.job.id;
      state.hydrated = true;
      notify(data.deduplicated ? "已找回原请求，没有重复生成。" : "任务已接收。可在这里查看进度，无需停留在本页。");
      renderHistory();
      renderResult();
    } catch (error) {
      if (epoch !== state.epoch) return;
      // Validation failures reject before task admission. All ambiguous failures retain the exact request.
      if ([400, 422].includes(error.status)) clearPending();
      if (error.status === 401) unauthenticate("登录已过期。重新登录后可继续核对原请求。");
      else notify(error.message, true);
    } finally {
      state.sending = false;
      updateControls();
      if (state.authenticated) refreshJobs();
      schedulePoll();
    }
  }

  function prettyDate(value) {
    if (!value) return "";
    const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function emptyArtwork() {
    const art = node("div", "empty-art"); art.setAttribute("aria-hidden", "true");
    const picture = node("span", "art-picture");
    picture.append(node("span", "art-sun"), node("span", "art-hill"), node("span", "art-hill second"), node("span", "art-floor"));
    art.append(node("span", "art-card"), picture, node("span", "art-spark", "✦"));
    return art;
  }

  function metadata(job) {
    const line = node("div", "metadata-line");
    [job.size ? job.size.replace("x", " × ") : "", QUALITY[job.quality] || "", job.fit === "contain" ? "完整保留" : "铺满裁切", prettyDate(job.created)].filter(Boolean).forEach((text) => line.append(node("span", "", text)));
    return line;
  }

  function exportLink(job) {
    const link = node("a", "button secondary", "导出 JSON");
    link.href = jobPath(job, "/export"); link.setAttribute("download", "");
    return link;
  }

  function renderResult() {
    const job = state.jobs.find((item) => item.id === state.selectedId);
    const signature = JSON.stringify(job || null);
    if (state.resultSignature === signature) return;
    state.resultSignature = signature;
    const content = $("result-content");
    content.replaceChildren();
    $("result-badge").textContent = job ? (job.status === "uncertain" && acknowledged(job) ? "已核对 · 结果不确定" : STATUSES[job.status]) : "等待灵感";
    $("result-badge").dataset.status = job ? job.status : "empty";
    if (!job) {
      const canvas = node("div", "empty-canvas"), inner = node("div");
      inner.append(emptyArtwork(), node("p", "empty-title", "这里，留给你的下一张作品"), node("p", "empty-copy", "写下脑海中的画面，让一个念头有了形状。"), node("p", "canvas-kicker", "MADE OF YOUR IMAGINATION"));
      canvas.append(inner); content.append(canvas); return;
    }
    if (job.status === "succeeded") {
      const wrap = node("div", "image-wrap"), image = node("img", "result-image");
      image.src = jobPath(job, "/image"); image.alt = `生成的图像：${job.prompt}`;
      image.addEventListener("error", () => {
        image.remove(); wrap.append(node("p", "image-unavailable", "图片暂时无法加载。请刷新记录，或尝试下载 PNG。"));
      }, { once: true });
      wrap.append(image); content.append(wrap);
      const info = node("div", "result-meta"), actions = node("div", "result-actions");
      const save = node("button", `button secondary${job.saved ? " saved" : ""}`, job.saved ? "★ 已收藏" : "☆ 收藏作品");
      save.type = "button"; save.setAttribute("aria-pressed", String(!!job.saved)); save.addEventListener("click", () => toggleSave(job, save));
      const download = node("a", "button primary", "下载 PNG ↓"); download.href = jobPath(job, "/image?download=1"); download.setAttribute("download", "");
      actions.append(save, download, exportLink(job));
      info.append(node("p", "result-prompt", job.prompt), metadata(job), actions); content.append(info);
      return;
    }
    const canvas = node("div", "empty-canvas"), inner = node("div"), art = node("div", "status-art");
    if (["queued", "running"].includes(job.status)) {
      art.classList.add("working"); const spinner = node("span", "spinner"); spinner.setAttribute("aria-hidden", "true"); art.append(spinner);
      inner.append(art, node("p", "empty-title", job.status === "queued" ? "灵感已经就位" : "正在让想象成形"), node("p", "empty-copy", "任务正在服务器处理，这里会自动更新状态。关闭页面不会取消任务。"));
    } else if (job.status === "uncertain") {
      art.textContent = "?";
      inner.append(art, node("p", "empty-title", "这次生成，需要你核对一下"), node("p", "empty-copy", "无法确认图像服务是否完成处理；可能已经产生费用。不会自动重试，也不会随时间自动解锁。"));
      inner.append(node("p", "uncertain-detail", job.error || "请在你配置的图像服务中，核对本次请求的生成记录和账单。"));
      if (acknowledged(job)) {
        inner.append(node("p", "status-note", "你已确认核对结果。可以主动提交新任务，但原任务不会重试。"));
      } else {
        const box = node("div", "ack-actions"), label = node("label", "ack-check"), check = node("input");
        check.type = "checkbox";
        label.append(check, node("span", "", "我已核对上游记录，理解本次可能已收费；继续创作可能另外产生费用。"));
        const button = node("button", "button secondary", "确认已核对，允许新的创作"); button.type = "button"; button.disabled = true;
        check.addEventListener("change", () => { button.disabled = !check.checked || state.acknowledging; });
        button.addEventListener("click", () => { if (check.checked) acknowledge(job, button); });
        box.append(label, button); inner.append(box);
      }
    } else {
      art.textContent = "!";
      inner.append(art, node("p", "empty-title", "这次没能完成"), node("p", "empty-copy", job.error || "生成失败，请检查服务配置和创作描述。"), node("p", "status-note", "不会自动重试。调整描述后，可主动提交一次新任务。"));
    }
    canvas.append(inner); content.append(canvas);
    const info = node("div", "result-meta"), actions = node("div", "result-actions");
    actions.append(exportLink(job));
    info.append(node("p", "result-prompt", job.prompt), metadata(job), actions); content.append(info);
  }

  function renderHistory() {
    const signature = JSON.stringify([state.jobs, state.selectedId, state.favorites]);
    if (state.historySignature === signature) return;
    state.historySignature = signature;
    const list = $("history-list"), visible = state.jobs.filter((job) => !state.favorites || job.saved);
    list.replaceChildren();
    $("history-count").textContent = String(state.jobs.length);
    $("history-empty").hidden = visible.length > 0;
    $("history-empty").textContent = state.favorites ? "还没有收藏。打开已完成的作品，点击收藏即可留在这里。" : "还没有创作记录。第一张图，从你的描述开始。";
    visible.forEach((job) => {
      const card = node("button", "history-card"); card.type = "button";
      card.setAttribute("aria-label", `${STATUSES[job.status]}：${job.prompt}`);
      card.setAttribute("aria-pressed", String(job.id === state.selectedId));
      const thumb = node("div", "history-thumb");
      if (job.status === "succeeded") {
        const image = node("img"); image.src = jobPath(job, "/image"); image.alt = ""; image.loading = "lazy";
        image.addEventListener("error", () => { image.remove(); thumb.append(node("span", "", "图片暂不可用")); }, { once: true });
        thumb.append(image);
      } else thumb.append(node("span", "", STATUSES[job.status]));
      if (job.saved) { const star = node("span", "favorite-indicator", "★"); star.setAttribute("aria-label", "已收藏"); thumb.append(star); }
      const info = node("div", "history-card-info"), line = node("span", "history-card-meta");
      line.append(node("span", "", prettyDate(job.created)), node("span", "", job.status === "uncertain" && acknowledged(job) ? "已核对" : STATUSES[job.status]));
      info.append(node("span", "history-card-prompt", job.prompt), line); card.append(thumb, info);
      card.addEventListener("click", () => {
        state.selectedId = job.id; renderHistory(); renderResult();
        if (window.matchMedia("(max-width: 720px)").matches) $("result-heading").scrollIntoView({ behavior: "auto", block: "start" });
      });
      list.append(card);
    });
  }

  async function toggleSave(job, button) {
    if (state.saving) return;
    const epoch = state.epoch; state.saving = true; button.disabled = true;
    try {
      const data = await api(jobPath(job, "/save"), { method: "POST", body: { saved: !job.saved } });
      if (epoch !== state.epoch) return;
      if (!validJob(data.job)) throw new APIError("收藏状态未确认，请刷新记录。", 0);
      state.jobsRevision += 1;
      state.jobs = state.jobs.map((item) => item.id === data.job.id ? data.job : item);
      renderHistory(); renderResult(); notify(data.job.saved ? "已收藏这张作品。" : "已取消收藏，作品仍保留在创作记录中。");
    } catch (error) {
      if (epoch !== state.epoch) return;
      if (error.status === 401) unauthenticate("登录已过期，请重新登录。");
      else { notify(error.message, true); refreshJobs(); }
    } finally { state.saving = false; button.disabled = false; }
  }

  async function acknowledge(job, button) {
    if (state.acknowledging) return;
    const epoch = state.epoch; state.acknowledging = true; button.disabled = true; updateControls();
    try {
      const data = await api(jobPath(job, "/acknowledge"), { method: "POST", body: { confirmed: true } });
      if (epoch !== state.epoch) return;
      if (!validJob(data.job) || !acknowledged(data.job)) throw new APIError("核对确认尚未生效，请刷新记录。", 0);
      state.jobsRevision += 1;
      state.jobs = state.jobs.map((item) => item.id === data.job.id ? data.job : item);
      renderHistory(); renderResult(); notify("核对确认已记录。原任务不会重试，你可以主动开始下一次创作。");
    } catch (error) {
      if (epoch !== state.epoch) return;
      if (error.status === 401) unauthenticate("登录已过期，请重新登录。");
      else { notify(error.message, true); refreshJobs(); }
    } finally { state.acknowledging = false; button.disabled = false; updateControls(); schedulePoll(); }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("login-button").disabled) return;
    $("login-button").disabled = true; loginError("");
    const token = $("token").value;
    try {
      await api("/api/login", { method: "POST", body: { token } });
      $("token").value = "";
      const session = await api("/api/session");
      if (!session.authenticated || !session.csrf_token) throw new APIError("登录状态未确认，请重新登录。", 0);
      authenticate(session);
    } catch (error) { $("token").value = ""; loginError(error.message); $("token").focus(); }
    finally { $("login-button").disabled = false; }
  });

  $("logout-button").addEventListener("click", async () => {
    if (state.sending || state.acknowledging) return;
    $("logout-button").disabled = true;
    try {
      await api("/api/logout", { method: "POST", body: {} });
      clearPending();
      $("creator-form").reset(); updatePromptCount(); notify("");
      unauthenticate();
    } catch (error) {
      if (error.status === 401) { clearPending(); $("creator-form").reset(); updatePromptCount(); unauthenticate(); }
      else notify("退出尚未确认，请重试。" + error.message, true);
    } finally { $("logout-button").disabled = false; }
  });

  $("creator-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if ($("generate-button").disabled) return;
    const prompt = $("prompt").value.trim();
    if (!prompt) { $("prompt").focus(); notify("先写下你想创作的画面。", true); return; }
    submit({ request_id: requestId(), prompt, negative_prompt: $("negative-prompt").value.trim(), size: document.querySelector('input[name="size"]:checked').value, quality: $("quality").value, fit: $("fit").value });
  });
  $("retry-button").addEventListener("click", () => { if (state.pending && !$("retry-button").disabled) submit(state.pending); });
  $("refresh-button").addEventListener("click", () => refreshJobs());
  $("favorites-button").addEventListener("click", () => {
    state.favorites = !state.favorites; $("favorites-button").setAttribute("aria-pressed", String(state.favorites)); renderHistory();
  });
  $("prompt").addEventListener("input", updatePromptCount);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.authenticated) refreshJobs(); });
  window.addEventListener("online", () => { if (state.authenticated) refreshJobs(); });

  loadPending();
  api("/api/session").then((session) => {
    if (session.authenticated && session.csrf_token) authenticate(session);
  }).catch((error) => { if (error.status !== 401) loginError(error.message); });
})();
