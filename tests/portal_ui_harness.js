"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "..", "src", "pbip_mcp", "web_ui", "index.html"), "utf8");
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
assert.equal(scripts.length, 2, "Run both scripts from the shipped page, not a copied implementation");

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase();
    this.ownerDocument = document;
    this.parentElement = null;
    this.childNodes = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.dataset = {};
    this.hidden = this.disabled = this.open = this._checked = false;
    this.value = this.id = this.className = this._text = "";
    this.files = [];
  }
  get children() { return this.childNodes.filter((node) => node.tagName !== "#TEXT"); }
  get childElementCount() { return this.children.length; }
  get options() { return this.querySelectorAll("option"); }
  get textContent() { return this._text + this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(value) {
    this.replaceChildren();
    this._text = String(value);
  }
  set innerHTML(_) { throw new Error("Unsafe HTML rendering is not allowed in the portal harness"); }
  get checked() { return this._checked; }
  set checked(value) {
    if (value && this.getAttribute("type") === "radio") {
      for (const radio of this.ownerDocument.querySelectorAll('input[type="radio"]')) {
        if (radio !== this && radio.getAttribute("name") === this.getAttribute("name")) radio._checked = false;
      }
    }
    this._checked = Boolean(value);
  }
  setAttribute(name, value) {
    value = String(value);
    this.attributes.set(name, value);
    if (["id", "value", "type"].includes(name)) this[name] = value;
    if (name === "class") this.className = value;
    if (["hidden", "disabled", "checked"].includes(name)) this[name] = true;
    if (name === "webkitdirectory") this.webkitdirectory = true;
    if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
  }
  getAttribute(name) {
    if (["id", "value", "type"].includes(name)) return this[name];
    if (name.startsWith("data-")) return this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
    return this.attributes.get(name);
  }
  append(...nodes) {
    for (const node of nodes) {
      node.remove();
      node.parentElement = this;
      this.childNodes.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.childNodes) child.parentElement = null;
    this.childNodes = [];
    this._text = "";
    this.append(...nodes);
  }
  remove() {
    if (this.parentElement) {
      const nodes = this.parentElement.childNodes;
      nodes.splice(nodes.indexOf(this), 1);
      this.parentElement = null;
    }
  }
  insertBefore(node, reference) {
    node.remove();
    const index = reference ? this.childNodes.indexOf(reference) : this.childNodes.length;
    assert.ok(index >= 0);
    node.parentElement = this;
    this.childNodes.splice(index, 0, node);
  }
  replaceWith(node) {
    this.parentElement.insertBefore(node, this);
    this.remove();
  }
  after(node) {
    const siblings = this.parentElement.childNodes;
    this.parentElement.insertBefore(node, siblings[siblings.indexOf(this) + 1] || null);
  }
  contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
  matches(selector) {
    const tag = selector.match(/^[a-z]+/i)?.[0];
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    const className = selector.match(/\.([\w-]+)/)?.[1];
    if (className && !this.className.split(/\s+/).includes(className)) return false;
    for (const [, name, value] of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
      if (value === undefined ? !this.attributes.has(name) : this.getAttribute(name) !== value) return false;
    }
    return !selector.includes(":checked") || this.checked;
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  async emit(type, fields = {}) {
    const event = { target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...fields };
    await Promise.all((this.listeners.get(type) || []).map((listener) => listener(event)));
    return event;
  }
  click() { return this.disabled ? Promise.resolve() : this.emit("click"); }
  focus() { this.ownerDocument.activeElement = this; }
}

function createDocument() {
  const document = new Element("document");
  document.ownerDocument = document;
  document.createElement = (tag) => new Element(tag, document);
  document.getElementById = (id) => document.querySelectorAll("*").find((element) => element.id === id) || null;
  const stack = [document];
  const markup = html.replace(/<!--[\s\S]*?-->|<script\b[^>]*>[\s\S]*?<\/script>|<style\b[^>]*>[\s\S]*?<\/style>/g, "");
  for (const token of markup.matchAll(/<\/?[^>]+>|[^<]+/g)) {
    const value = token[0];
    if (value.startsWith("<!")) continue;
    if (value.startsWith("</")) { stack.pop(); continue; }
    if (value.startsWith("<")) {
      const tag = value.match(/^<([\w-]+)/)[1];
      const element = document.createElement(tag);
      for (const [, name, quoted, plain] of value.slice(tag.length + 1, -1).matchAll(/([\w-]+)(?:=(?:"([^"]*)"|([^\s>]+)))?/g)) {
        element.setAttribute(name, quoted ?? plain ?? "");
      }
      stack.at(-1).append(element);
      if (!["meta", "input", "br", "hr", "link"].includes(tag) && !value.endsWith("/>")) stack.push(element);
    } else {
      const text = document.createElement("#text");
      text.textContent = value;
      stack.at(-1).append(text);
    }
  }
  document.documentElement = document.querySelector("html");
  document.activeElement = document;
  for (const select of document.querySelectorAll("select")) select.value = select.options[0]?.value || "";
  return document;
}

function status(worker = { ready: true, state: "idle" }, seconds = 173) {
  return {
    ok: true, worker, queue: { mine_pending: 0, pending: null, running: null },
    capabilities: { directions: ["pbip_to_pbix", "pbix_to_pbip"], export_modes: ["definitions", "portable"], folder_upload: true },
    limits: { queue_seconds: seconds, archive_bytes: 1000000, file_count: 100, uncompressed_bytes: 1000000, member_bytes: 1000000 },
  };
}
function job(overrides = {}) {
  return {
    job_id: "job-1", status: "queued", phase: "queued", direction: "pbix_to_pbip", export_mode: "definitions",
    source: { name: "report.pbix" }, artifacts: [], artifact_status: "pending",
    created_at: "2026-09-11T04:00:00Z", updated_at: "2026-09-11T04:02:53Z",
    queue_deadline: "2026-09-11T04:02:53Z", started_at: null, finished_at: null,
    ...overrides,
  };
}
const knownWarnings = [
  "Shared Windows workers are not a sandbox for untrusted M queries or separate customer security domains.",
  "Model definitions may contain embedded data, sensitive text and connection information; none is automatically redacted.",
  "Definitions export omits the data cache; loading data again may be necessary. No automatic refresh is performed.",
];
const session = { ok: true, user: { id: "ui-test", display_name: "UI Test" }, csrf_token: "not-a-real-token" };
const response = (body, code = 200) => ({ ok: code >= 200 && code < 300, status: code, json: async () => structuredClone(body) });
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function date(value) {
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(value));
}

async function portal(initialStatus = status(), initialJobs = []) {
  const document = createDocument();
  const window = new Element("window", document);
  window.location = { search: "" };
  window.matchMedia = () => ({ matches: false });
  const timers = new Map();
  const routes = new Map();
  const requests = [];
  let timerId = 0;
  const server = { status: initialStatus, jobs: initialJobs };
  const navigator = { onLine: true };
  function enqueue(method, url, handler) {
    const key = method + " " + url;
    if (!routes.has(key)) routes.set(key, []);
    routes.get(key).push(handler);
  }
  const context = vm.createContext({
    document, window, navigator, URLSearchParams, AbortController, console,
    FormData: class {
      constructor() { this.entries = []; }
      append(...entry) { this.entries.push(entry); }
    },
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      const request = { url, method: options.method || "GET", ...options };
      requests.push(request);
      const handler = routes.get(request.method + " " + url)?.shift();
      if (handler) return handler(request);
      if (url === "/api/session") return response(session);
      if (url === "/api/status" && request.method === "GET") return response(server.status);
      if (url === "/api/jobs" && request.method === "GET") return response({ ok: true, jobs: server.jobs });
      if (url === "/api/jobs" && request.method === "POST") {
        const created = job({ job_id: "created-" + requests.length });
        server.jobs = [created, ...server.jobs];
        return response({ ok: true, job: created }, 201);
      }
      const cancel = url.match(/^\/api\/jobs\/([^/]+)\/cancel$/);
      if (cancel && request.method === "POST") {
        const item = server.jobs.find((item) => item.job_id === cancel[1]);
        assert.ok(item);
        const cancelled = { ...item, status: "cancelled" };
        server.jobs = server.jobs.map((item) => item.job_id === cancelled.job_id ? cancelled : item);
        return response({ ok: true, job: cancelled });
      }
      throw new Error("Unmocked request: " + request.method + " " + url);
    },
  });
  for (const script of scripts) vm.runInContext(script, context, { filename: "portal-inline.js" });
  await flush();
  const $ = (id) => {
    const element = document.getElementById(id);
    assert.ok(element, `Missing DOM element #${id}`);
    return element;
  };
  assert.equal($("app").hidden, false, "The actual bootstrap/login flow must complete");
  return {
    $, document, window, navigator, server, requests, enqueue, timers,
    posts: () => requests.filter((request) => request.method === "POST" && request.url === "/api/jobs"),
    async select(name = "report.pbix") {
      $("file-input").files = [{ name, size: 100 }];
      await $("file-input").emit("change");
    },
    async acknowledge(value = true) {
      $("acknowledge-offline-queue").checked = value;
      await $("acknowledge-offline-queue").emit("change");
    },
    async refresh(nextStatus = server.status) {
      server.status = nextStatus;
      await $("refresh-jobs").click();
      await flush();
    },
    submit: () => $("upload-form").emit("submit"),
  };
}

const groups = {
  async "worker-gating"() {
    for (const workerState of ["idle", "ready", "busy"]) {
      const ui = await portal(status({ ready: true, state: workerState }));
      await ui.select();
      assert.equal(ui.$("submit-job").disabled, false, workerState);
      assert.equal(ui.$("offline-queue").hidden, true);
      assert.equal(ui.$("worker-state").dataset.state, workerState === "busy" ? "busy" : "ready");
      await ui.submit();
      assert.equal(ui.posts().length, 1);
    }
    for (const workerState of ["offline", "absent", "stale", "stopped", "blocked", "busy"]) {
      const ui = await portal(status({ ready: false, state: workerState, updated_at: "2026-09-10T15:26:00Z" }));
      await ui.select();
      assert.equal(ui.$("submit-job").disabled, true, workerState);
      assert.equal(ui.$("worker-state").dataset.state, "offline");
      assert.match(ui.$("worker-help").textContent, /当前无法开始转换/);
      assert.match(ui.$("worker-history").textContent, /上次心跳时间/);
      assert.equal(ui.$("offline-queue").hidden, false);
      assert.equal(ui.$("acknowledge-offline-queue").checked, false);
      assert.match(ui.$("offline-queue-help").textContent, /173 秒/);
      assert.match(ui.$("offline-queue-help").textContent, /从服务端创建任务起计算/);
      assert.match(ui.$("offline-queue-help").textContent, /超时失败/);
      if (workerState === "stale") assert.match(ui.$("worker-state").textContent, /心跳已过期/);
      if (workerState === "blocked") assert.match(ui.$("worker-state").textContent, /受阻/);
      ui.$("submit-job").disabled = false;
      await ui.submit();
      assert.equal(ui.posts().length, 0, "The submit handler must enforce acknowledgement itself");
      await ui.acknowledge();
      assert.equal(ui.$("submit-job").disabled, false);
      await ui.acknowledge(false);
      assert.equal(ui.$("submit-job").disabled, true);
      await ui.acknowledge();
      await ui.submit();
      assert.equal(ui.posts().length, 1, "Explicit offline admission remains compatible");
      assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    }
    const malformedWorkers = [null, {}, { state: "busy" }, ...["false", "true", 0, 1, null, {}, []].map((ready) => ({ ready, state: "busy" })),
      { ready: true, state: "stale" }, { ready: true, state: "blocked" }, { ready: true, state: "unexpected" }, { ready: true }];
    for (const worker of malformedWorkers) {
      const ui = await portal(status(worker));
      await ui.select();
      await ui.acknowledge();
      assert.equal(ui.$("submit-job").disabled, true, JSON.stringify(worker));
      assert.equal(ui.$("offline-queue").hidden, true);
      assert.equal(ui.$("worker-state").dataset.state, "unknown");
      ui.$("submit-job").disabled = false;
      await ui.submit();
      assert.equal(ui.posts().length, 0);
    }
    for (const limit of [undefined, null, 0, -1, "173", Infinity, NaN]) {
      const snapshot = status({ ready: false, state: "stale" });
      snapshot.limits.queue_seconds = limit;
      const ui = await portal(snapshot);
      await ui.select();
      await ui.acknowledge();
      assert.equal(ui.$("submit-job").disabled, true);
      assert.equal(ui.$("offline-queue").hidden, true);
      await ui.submit();
      assert.equal(ui.posts().length, 0);
    }
  },

  async "worker-history"() {
    const heartbeat = "2026-09-10T15:26:00Z";
    const previouslyReady = "The worker has an active, unlocked interactive Windows session.";
    const stale = status({
      ready: false, state: "stale", updated_at: heartbeat,
      last_reported_state: "idle", last_reported_message: previouslyReady,
    });
    const ui = await portal(stale);
    await ui.select();
    assert.equal(ui.$("worker-history").hidden, false);
    assert.match(ui.$("worker-history").textContent, /仅供排查，不代表当前就绪/);
    assert.ok(ui.$("worker-history").textContent.includes("上次心跳时间：" + date(heartbeat)));
    assert.match(ui.$("worker-history").textContent, /上次上报状态：空闲（idle）/);
    assert.match(ui.$("worker-history").textContent, /当时节点具有活动且未锁定/);
    assert.equal(ui.$("worker-state").dataset.state, "offline");
    assert.match(ui.$("worker-state").textContent, /心跳已过期/);
    assert.equal(ui.$("submit-job").disabled, true, "Historical readiness must never authorize submission");
    await ui.submit();
    assert.equal(ui.posts().length, 0);

    const messages = [
      ["Conversion requires an interactive Windows worker.", /交互式 Windows 桌面/],
      ["Run the worker as a normal user, not SYSTEM.", /普通用户而不是 SYSTEM/],
      ["Session 0 cannot run Desktop conversion.", /会话 0 不能运行/],
      ["Keep the worker's RDP session connected and active.", /RDP 会话连接且处于活动状态/],
      ["Unlock the worker's interactive Default desktop.", /解锁节点的交互式 Default 桌面/],
      ["Start the worker on the user's interactive desktop.", /该用户的交互式桌面启动/],
      ["Windows could not verify an active, unlocked user desktop.", /当时无法确认/],
      ["Configure the installed standard x64 PBIDesktop.exe path.", /标准 x64 Power BI Desktop/],
      ["Interactive worker stopped.", /当时已停止/],
      ["No interactive worker has started.", /尚无交互式转换节点启动记录/],
      ["Worker heartbeat expired; controller cannot assert Desktop readiness.", /心跳已过期，控制服务无法确认/],
    ];
    for (const [message, translation] of messages) {
      await ui.refresh(status({ ...stale.worker, last_reported_state: "blocked", last_reported_message: message }));
      assert.match(ui.$("worker-history").textContent, /上次上报状态：受阻（blocked）/);
      assert.match(ui.$("worker-history").textContent, translation);
      assert.equal(ui.$("worker-history").textContent.includes(message), false);
      assert.equal(ui.$("worker-state").dataset.state, "offline");
      assert.equal(ui.$("submit-job").disabled, true);
    }
    const unknownMessage = '<img src=x onerror="globalThis.compromised=true">unrecognized heartbeat & context';
    const unknownState = "<script>unknown-state</script>";
    await ui.refresh(status({ ...stale.worker, last_reported_state: unknownState, last_reported_message: unknownMessage }));
    for (const unknown of [unknownState, unknownMessage]) {
      assert.ok(ui.$("worker-history").querySelectorAll("span").some((span) => span.textContent === unknown));
    }
    assert.equal(ui.$("worker-history").querySelectorAll("img").length, 0);
    assert.equal(ui.$("worker-history").querySelectorAll("script").length, 0);
    ui.enqueue("GET", "/api/status", () => { throw new Error("cannot refresh current status"); });
    await ui.refresh();
    assert.match(ui.$("worker-state").textContent, /状态未更新/);
    assert.match(ui.$("worker-history").textContent, /不代表当前就绪/);
    assert.equal(ui.$("offline-queue").hidden, true);
    assert.equal(ui.$("submit-job").disabled, true);

    await ui.refresh(status({ ready: true, state: "idle", updated_at: heartbeat, last_reported_state: "blocked",
      last_reported_message: "Unlock the worker's interactive Default desktop." }));
    assert.equal(ui.$("submit-job").disabled, false, "Only current readiness, not historical state, controls normal submission");
    assert.equal(ui.$("worker-state").dataset.state, "ready");
    await ui.refresh(status({ ready: false, state: "stale", updated_at: heartbeat }));
    assert.ok(ui.$("worker-history").textContent.includes(date(heartbeat)));
    assert.doesNotMatch(ui.$("worker-history").textContent, /上次上报诊断|上次上报状态/);
    await ui.refresh(status({ ready: false, state: "absent", last_reported_state: null, last_reported_message: null }));
    assert.equal(ui.$("worker-history").hidden, true);
    await ui.refresh(status({ ready: false, state: "stale", last_reported_state: {}, last_reported_message: 42 }));
    assert.equal(ui.$("worker-history").hidden, true);
    await ui.refresh(stale);
    await ui.$("logout").click();
    assert.equal(ui.$("worker-history").hidden, true);
    assert.equal(ui.$("worker-history").textContent, "");
  },

  async "acknowledgement-reset"() {
    const offline = status({ ready: false, state: "stale", updated_at: "2026-09-10T15:26:00Z" });
    const ui = await portal(offline);
    await ui.select();
    await ui.acknowledge();
    await ui.refresh({ ...offline, worker: { ...offline.worker, updated_at: "2026-09-10T15:26:10Z" } });
    assert.equal(ui.$("acknowledge-offline-queue").checked, true, "An ordinary same-availability poll must not discard consent");
    await ui.select("another.pbix");
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    assert.equal(ui.$("submit-job").disabled, true);
    await ui.acknowledge();
    await ui.$("clear-file").click();
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    await ui.select();
    await ui.acknowledge();
    await ui.$("export-options").emit("change");
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    await ui.acknowledge();
    await ui.$("direction").emit("change");
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    for (const next of [status(), offline, status({ ready: false, state: "blocked" }), status({ ready: false, state: "blocked" }, 541),
      status({ ready: false, state: "blocked", code: "SESSION_LOST" }, 541)]) {
      await ui.acknowledge();
      await ui.refresh(next);
      assert.equal(ui.$("acknowledge-offline-queue").checked, false, JSON.stringify(next.worker));
    }
    assert.match(ui.$("offline-queue-help").textContent, /541 秒/);
    await ui.acknowledge();
    ui.enqueue("GET", "/api/status", () => { throw new Error("status unavailable"); });
    await ui.refresh();
    assert.equal(ui.$("offline-queue").hidden, true);
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    assert.equal(ui.$("submit-job").disabled, true);
    assert.match(ui.$("worker-state").textContent, /未更新/);
    await ui.acknowledge();
    await ui.submit();
    assert.equal(ui.posts().length, 0, "No checkbox bypass when the API status cannot be refreshed");
    await ui.refresh(offline);
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    await ui.acknowledge();
    ui.navigator.onLine = false;
    await ui.window.emit("offline");
    assert.equal(ui.$("offline-queue").hidden, true);
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    await ui.acknowledge();
    await ui.submit();
    assert.equal(ui.posts().length, 0);
    ui.navigator.onLine = true;
    await ui.window.emit("online");
    await flush();
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    await ui.acknowledge();
    await ui.$("logout").click();
    assert.equal(ui.$("login").hidden, false);
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    ui.$("access-token").value = "test-login-only";
    await ui.$("login-form").emit("submit");
    await flush();
    assert.equal(ui.$("app").hidden, false);
    await ui.select();
    assert.equal(ui.$("acknowledge-offline-queue").checked, false);
    assert.equal(ui.$("submit-job").disabled, true);
  },

  async "submission"() {
    const ui = await portal();
    await ui.select();
    const pendingStatus = deferred();
    ui.enqueue("GET", "/api/status", () => pendingStatus.promise);
    const first = ui.submit();
    assert.equal(ui.$("submit-job").disabled, true);
    assert.equal([...ui.timers.values()].some((timer) => timer.delay === 5000), false, "Periodic polling must not supersede a slow submit preflight");
    await ui.submit();
    assert.equal(ui.posts().length, 0);
    assert.equal(ui.requests.filter((request) => request.url === "/api/status").length, 2, "Only one submit preflight is issued");
    pendingStatus.resolve(response(status()));
    await first;
    assert.equal(ui.posts().length, 1);
    const post = ui.posts()[0];
    assert.equal(post.headers["X-CSRF-Token"], session.csrf_token);
    assert.equal(post.credentials, "same-origin");
    assert.deepEqual(post.body.entries.map((entry) => entry[0]), ["file", "direction", "export_mode"]);
    await ui.submit();
    assert.equal(ui.posts().length, 1, "Successful submission clears the old selection");

    for (const next of [status({ ready: false, state: "busy" }), status({ state: "busy" }), status({ ready: true, state: "stale" })]) {
      const changed = await portal();
      await changed.select();
      changed.server.status = next;
      await changed.submit();
      assert.equal(changed.posts().length, 0, "Preflight must recheck current availability, not trust the button");
      assert.equal(changed.$("submit-job").disabled, true);
      assert.equal(changed.document.getElementById("confirm-resubmit"), null);
    }
    const expiredConsent = await portal(status({ ready: false, state: "stale" }));
    await expiredConsent.select();
    await expiredConsent.acknowledge();
    expiredConsent.server.status = status({ ready: false, state: "stale" }, 73);
    await expiredConsent.submit();
    assert.equal(expiredConsent.posts().length, 0);
    assert.equal(expiredConsent.$("acknowledge-offline-queue").checked, false);
    assert.match(expiredConsent.$("offline-queue-help").textContent, /73 秒/);
    await expiredConsent.acknowledge();
    await expiredConsent.submit();
    assert.equal(expiredConsent.posts().length, 1);

    const failedGet = await portal();
    await failedGet.select();
    failedGet.enqueue("GET", "/api/status", () => { throw new Error("preflight network loss"); });
    await failedGet.submit();
    assert.equal(failedGet.posts().length, 0);
    assert.equal(failedGet.document.getElementById("confirm-resubmit"), null);
    assert.match(failedGet.$("upload-message").textContent, /尚未上传或创建任务/);
    assert.equal(failedGet.$("offline-queue").hidden, true);
    await failedGet.refresh();
    assert.equal(failedGet.$("submit-job").disabled, false, "A failed GET must not enter uncertain-POST recovery");
    await failedGet.submit();
    assert.equal(failedGet.posts().length, 1);

    const getTimeout = await portal();
    await getTimeout.select();
    getTimeout.enqueue("GET", "/api/status", ({ signal }) => new Promise((_, reject) => {
      signal.addEventListener("abort", () => reject(new Error("request aborted")));
    }));
    const getAttempt = getTimeout.submit();
    const statusTimeout = [...getTimeout.timers.values()].find((timer) => timer.delay === 30000);
    assert.ok(statusTimeout);
    statusTimeout.callback();
    await getAttempt;
    assert.equal(getTimeout.posts().length, 0);
    assert.equal(getTimeout.document.getElementById("confirm-resubmit"), null);
    assert.match(getTimeout.$("upload-message").textContent, /尚未上传或创建任务/);

    const newLimit = await portal();
    await newLimit.select();
    newLimit.server.status = { ...status(), limits: { ...status().limits, archive_bytes: 1 } };
    await newLimit.submit();
    assert.equal(newLimit.posts().length, 0, "Preflight also revalidates the selected file against current limits");
    assert.match(newLimit.$("upload-message").textContent, /上传大小超过/);
  },

  async "submission-races"() {
    const ui = await portal();
    await ui.select();
    const olderStatus = deferred();
    const olderJobs = deferred();
    ui.enqueue("GET", "/api/status", () => olderStatus.promise);
    ui.enqueue("GET", "/api/jobs", () => olderJobs.promise);
    await ui.$("refresh-jobs").click();
    ui.server.status = status({ ready: false, state: "stale" });
    await ui.submit();
    assert.equal(ui.posts().length, 0);
    olderStatus.resolve(response(status()));
    olderJobs.resolve(response({ ok: true, jobs: [] }));
    await flush();
    assert.equal(ui.$("worker-state").dataset.state, "offline", "An older poll cannot overwrite preflight readiness");
    assert.equal(ui.$("submit-job").disabled, true);

    const newer = await portal();
    await newer.select();
    const preflight = deferred();
    newer.enqueue("GET", "/api/status", () => preflight.promise);
    const submission = newer.submit();
    await newer.refresh(status({ ready: false, state: "blocked" }));
    preflight.resolve(response(status()));
    await submission;
    assert.equal(newer.posts().length, 0, "An outdated preflight cannot override a newer poll");
    assert.equal(newer.$("worker-state").dataset.state, "offline");

    const versionedJobs = await portal();
    await versionedJobs.select();
    const oldList = deferred();
    versionedJobs.enqueue("GET", "/api/jobs", () => oldList.promise);
    await versionedJobs.$("refresh-jobs").click();
    await versionedJobs.submit();
    assert.equal(versionedJobs.$("jobs-list").children.length, 1);
    oldList.resolve(response({ ok: true, jobs: [] }));
    await flush();
    assert.equal(versionedJobs.$("jobs-list").children.length, 1, "A pre-mutation list cannot erase a newly created task");

    const loggedOut = await portal();
    await loggedOut.select();
    const sessionRace = deferred();
    loggedOut.enqueue("GET", "/api/status", () => sessionRace.promise);
    const oldSubmit = loggedOut.submit();
    await loggedOut.$("logout").click();
    sessionRace.resolve(response(status()));
    await oldSubmit;
    assert.equal(loggedOut.posts().length, 0, "Preflight must not submit after logout");
    assert.equal(loggedOut.$("login").hidden, false);
  },

  async "uncertain"() {
    for (const failure of [
      () => { throw new Error("connection lost after POST"); },
      () => response({ ok: false, error: { code: "SERVICE_ERROR", message: "internal" } }, 503),
      () => ({ ok: true, status: 201, json: async () => { throw new Error("invalid JSON"); } }),
      () => response({ ok: true }, 201),
    ]) {
      const ui = await portal(status({ ready: false, state: "stale" }));
      await ui.select();
      await ui.acknowledge();
      ui.enqueue("POST", "/api/jobs", failure);
      await ui.submit();
      assert.equal(ui.posts().length, 1);
      assert.match(ui.$("upload-message").textContent, /尚未确认任务是否创建/);
      assert.equal(ui.$("submit-job").disabled, true);
      assert.equal(ui.$("acknowledge-offline-queue").checked, false);
      assert.equal(ui.$("confirm-resubmit").disabled, true);
      await ui.acknowledge();
      await ui.submit();
      assert.equal(ui.posts().length, 1);
      await ui.$("confirm-resubmit").emit("click");
      await ui.submit();
      assert.equal(ui.posts().length, 1, "Resubmit confirmation cannot bypass the required list check");
      await ui.refresh();
      assert.equal(ui.$("confirm-resubmit").disabled, false);
      assert.equal(ui.$("submit-job").disabled, true);
      await ui.$("confirm-resubmit").click();
      assert.equal(ui.$("acknowledge-offline-queue").checked, false);
      assert.equal(ui.$("submit-job").disabled, true);
      await ui.submit();
      assert.equal(ui.posts().length, 1);
      await ui.acknowledge();
      await ui.submit();
      assert.equal(ui.posts().length, 2);
    }
    const pending = await portal();
    await pending.select();
    const postResponse = deferred();
    pending.enqueue("POST", "/api/jobs", () => postResponse.promise);
    const submission = pending.submit();
    await flush();
    assert.equal(pending.posts().length, 1);
    assert.ok([...pending.timers.values()].some((timer) => timer.delay === 5000), "Task polling resumes during a potentially long upload");
    await pending.submit();
    assert.equal(pending.posts().length, 1, "An in-flight POST cannot be duplicated");
    postResponse.resolve(response({ ok: true, job: job() }, 201));
    await submission;
    assert.equal(pending.$("submit-job").disabled, true);

    const timedOut = await portal();
    await timedOut.select();
    timedOut.enqueue("POST", "/api/jobs", ({ signal }) => new Promise((_, reject) => {
      signal.addEventListener("abort", () => reject(new Error("request aborted")));
    }));
    const timedAttempt = timedOut.submit();
    await flush();
    const uploadTimeout = [...timedOut.timers.values()].find((timer) => timer.delay === 600000);
    assert.ok(uploadTimeout);
    uploadTimeout.callback();
    await timedAttempt;
    assert.equal(timedOut.posts().length, 1);
    assert.match(timedOut.$("upload-message").textContent, /尚未确认任务是否创建/);
    assert.match(timedOut.$("upload-diagnostic-body").textContent, /TIMEOUT/);
    await timedOut.submit();
    assert.equal(timedOut.posts().length, 1);
  },

  async "deadlines-errors"() {
    const timeout = job({
      status: "failed", finished_at: "2026-09-11T04:03:00Z", error: {
        code: "QUEUE_TIMEOUT", message: "No interactive worker claimed the job before its queue deadline.",
      },
    });
    const ui = await portal(status(), [timeout]);
    let row = ui.$("jobs-list").children[0];
    const text = row.querySelector(".job-message").textContent;
    assert.match(text, /截止时间前没有转换节点领取任务/);
    assert.match(text, /Power BI Desktop 转换尚未开始/);
    assert.match(text, /不能据此认定源文件损坏/);
    assert.match(text, /联系管理员恢复节点后创建新任务/);
    assert.match(text, /此失败任务不会自动转为成功/);
    assert.ok(text.includes(date(timeout.queue_deadline)));
    assert.equal(row.querySelector(".badge").textContent, "失败");
    const details = row.querySelector("details").textContent;
    assert.match(details, /失败任务的最后记录阶段：排队（queued）/);
    assert.match(details, /QUEUE_TIMEOUT/);
    assert.ok(details.includes(timeout.error.message));
    for (const [label, field] of [["创建时间", "created_at"], ["排队截止时间", "queue_deadline"], ["最后更新时间", "updated_at"], ["结束时间", "finished_at"]]) {
      assert.ok(details.includes(label + "：" + date(timeout[field])), field);
    }
    assert.match(details, /开始时间：未记录/);
    ui.server.jobs = [{ ...timeout, started_at: "2026-09-11T04:01:00Z" }];
    await ui.refresh();
    row = ui.$("jobs-list").children[0];
    assert.match(row.querySelector(".job-message").textContent, /同时存在开始时间/);
    assert.match(row.querySelector(".job-message").textContent, /无法确认 Power BI Desktop 是否已开始转换/);
    assert.doesNotMatch(row.querySelector(".job-message").textContent, /转换尚未开始/);
    assert.ok(row.querySelector("details").textContent.includes("开始时间：" + date(ui.server.jobs[0].started_at)));
    for (const deadline of [timeout.queue_deadline, null]) {
      ui.server.jobs = [job({ queue_deadline: deadline, queue_position: 4 })];
      await ui.refresh();
      const queued = ui.$("jobs-list").children[0].querySelector(".job-message").textContent;
      assert.match(queued, /队列位置 4/);
      assert.match(queued, /否则将超时失败/);
      assert.match(queued, /联系管理员恢复/);
      if (deadline) assert.ok(queued.includes(date(deadline)));
      else assert.match(queued, /排队并非无限等待/);
    }
    const localizations = [
      ["DATA_CACHE_REQUIRED", /手动加载数据并保存工程/, /工程可信且具备数据源权限/, /\.pbi\\cache\.abf/, /再创建新任务/, /不会自动刷新数据/],
      ["PORTABLE_CACHE_MISSING", /未导出非空的任务专属数据缓存/, /管理员检查源 PBIX/, /可编辑工程（不含数据缓存）/, /不保证直接离线转回/, /不会自动刷新数据/],
      ["WORKER_TIMEOUT", /运行已超时/, /心跳已过期/, /管理员检查节点/],
      ["INTERACTIVE_SESSION_LOST", /会话已断开或锁定/, /解锁/, /节点就绪后创建新任务/],
      ["WORKER_INTERNAL_ERROR", /内部错误/, /私有日志/, /确认恢复后创建新任务/],
      ["DESKTOP_UNEXPECTED_SAVE_PROMPT", /意外的保存确认/, /安全处理方式/, /不要反复提交/],
    ];
    for (const [code, ...patterns] of localizations) {
      ui.server.jobs = [job({ status: "failed", error: { code, message: "Raw worker diagnostic" } })];
      await ui.refresh();
      const failed = ui.$("jobs-list").children[0];
      for (const pattern of patterns) assert.match(failed.querySelector(".job-message").textContent, pattern);
      assert.ok(failed.querySelector("details").textContent.includes(code));
      assert.ok(failed.querySelector("details").textContent.includes("Raw worker diagnostic"));
    }
  },

  async "warnings-retention"() {
    const unknown = '<img src=x onerror="globalThis.compromised=true">unknown warning & data';
    const diagnostic = '<script>globalThis.compromised=true</script>unknown backend diagnostic';
    const failed = job({
      status: "failed", error: { code: "UNKNOWN_WORKER_CODE", message: diagnostic }, warnings: [...knownWarnings, unknown, "__proto__", 42],
      retention_deadline: "2026-09-12T04:03:00Z", requires_data_reload: true,
    });
    const ui = await portal(status(), [failed]);
    let row = ui.$("jobs-list").children[0];
    const details = row.querySelector("details");
    const safety = row.querySelector(".job-safety");
    assert.ok(safety);
    assert.equal(safety.parentElement, row, "Safety notes must not be nested in error diagnostics");
    assert.equal(safety.querySelector("summary").textContent, "安全与数据说明（非错误原因）");
    assert.match(safety.textContent, /不可信 M 查询的安全沙箱/);
    assert.match(safety.textContent, /不提供不同客户安全域之间的隔离/);
    assert.match(safety.textContent, /内嵌数据、敏感文本和连接信息/);
    assert.match(safety.textContent, /均不会自动脱敏/);
    assert.match(safety.textContent, /不会自动刷新数据/);
    for (const raw of knownWarnings) assert.equal(safety.textContent.includes(raw), false);
    assert.ok(safety.querySelectorAll("p").some((line) => line.textContent === unknown));
    assert.ok(safety.querySelectorAll("p").some((line) => line.textContent === "__proto__"));
    assert.equal(safety.querySelectorAll("img").length, 0);
    assert.ok(details.textContent.includes(diagnostic), "Unknown backend diagnostics must remain available as literal text");
    assert.equal(details.querySelectorAll("script").length, 0);
    assert.equal(details.textContent.includes(unknown), false);
    assert.equal(details.textContent.includes("不会自动脱敏"), false);
    assert.match(details.textContent, /任务文件保留至/);
    assert.doesNotMatch(details.textContent, /结果保留至/);
    assert.match(row.querySelector(".job-message").textContent, /UNKNOWN_WORKER_CODE/);
    details.open = true;
    safety.open = true;
    safety.querySelector("summary").focus();
    ui.server.jobs = [{ ...failed, updated_at: "2026-09-11T04:04:00Z" }];
    await ui.refresh();
    row = ui.$("jobs-list").children[0];
    assert.equal(row.querySelector("details").open, true);
    assert.equal(row.querySelector(".job-safety").open, true);
    assert.equal(ui.document.activeElement, row.querySelector(".job-safety").querySelector("summary"));
    for (const taskStatus of ["cancelled", "succeeded", "failed"]) {
      ui.server.jobs = [{ ...failed, status: taskStatus, artifact_status: "expired" }];
      await ui.refresh();
      row = ui.$("jobs-list").children[0];
      if (taskStatus === "succeeded") {
        assert.match(row.textContent, /结果保留至/);
        assert.equal(row.querySelector(".badge").textContent, "结果过期");
      } else {
        assert.match(row.textContent, /任务文件保留至/);
        assert.match(row.querySelector(".badge").textContent, /任务文件过期/);
        assert.doesNotMatch(row.textContent, /结果保留至/);
      }
    }
  },

  async "ongoing-tasks"() {
    const queued = job();
    const completed = job({ job_id: "complete", status: "succeeded", phase: "completed", artifact_status: "available",
      artifacts: [{ kind: "pbip", status: "available", filename: "report.pbip.zip", bytes: 200 }] });
    const ui = await portal(status({ ready: false, state: "stale" }), [queued, completed]);
    await ui.select();
    assert.equal(ui.$("submit-job").disabled, true);
    let rows = ui.$("jobs-list").children;
    const cancel = rows[0].querySelector('button[data-action="cancel"]');
    assert.equal(cancel.disabled, false);
    const download = rows[1].querySelector("a");
    assert.equal(download.href, "/api/jobs/complete/artifacts/pbip");
    const click = await download.emit("click");
    assert.equal(click.defaultPrevented, false);
    await cancel.click();
    assert.equal(ui.$("jobs-list").children[0].querySelector(".badge").textContent, "已取消");
    assert.equal(ui.requests.filter((request) => request.url.endsWith("/cancel")).length, 1);
    ui.server.jobs = [{ ...queued, status: "failed", error: { code: "QUEUE_TIMEOUT", message: "Raw deadline diagnostic" } }, completed];
    const timer = [...ui.timers.entries()].find(([, timer]) => timer.delay === 5000);
    assert.ok(timer, "Task polling remains scheduled while the worker is unavailable");
    ui.timers.delete(timer[0]);
    await timer[1].callback();
    rows = ui.$("jobs-list").children;
    assert.equal(rows[0].querySelector(".badge").textContent, "失败");
    assert.match(rows[0].querySelector(".job-message").textContent, /转换尚未开始/);
    assert.ok(rows[1].querySelector("a"));
    assert.equal(ui.posts().length, 0);
  },

  async "customer-names"() {
    const name = "客户 工程.v2";
    const artifacts = [
      {kind: "pbix", status: "available", filename: name + ".pbix", bytes: 200},
      {kind: "verification", status: "available", filename: name + ".verification.json", bytes: 100},
    ];
    const completed = job({status: "succeeded", phase: "completed", direction: "pbip_to_pbix",
      source: {name}, artifact_status: "available", artifacts});
    const ui = await portal(status(), [completed]);
    const row = ui.$("jobs-list").children[0];
    assert.equal(row.querySelector(".job-title").textContent, name);
    const links = row.querySelectorAll("a");
    for (let index = 0; index < artifacts.length; index++) {
      assert.equal(links[index].getAttribute("download"), artifacts[index].filename);
      assert.ok(row.querySelector("details").textContent.includes(artifacts[index].filename));
      assert.equal(links[index].href, "/api/jobs/job-1/artifacts/" + artifacts[index].kind);
    }
    assert.equal(ui.posts().length, 0);
  },

  async "waiting-worker"() {
    const waiting = status({ready: false, state: "waiting_for_session",
      last_reported_state: "waiting_for_session", last_reported_message: "Unlock the worker's interactive Default desktop."});
    const ui = await portal(waiting);
    await ui.select();
    assert.match(ui.$("worker-state").textContent, /已暂停，等待桌面恢复/);
    assert.equal(ui.$("worker-state").dataset.state, "offline");
    assert.match(ui.$("worker-help").textContent, /恢复连接且解锁后会自动接单/);
    assert.match(ui.$("worker-help").textContent, /不会自动登录或解锁/);
    assert.match(ui.$("worker-help").textContent, /不会重跑旧失败任务/);
    assert.match(ui.$("worker-help").textContent, /截止时间仍有效/);
    assert.match(ui.$("worker-history").textContent, /等待桌面恢复（waiting_for_session）/);
    assert.equal(ui.$("submit-job").disabled, true);
    await ui.submit();
    assert.equal(ui.posts().length, 0);
    await ui.refresh(status());
    assert.equal(ui.$("submit-job").disabled, false);
    assert.doesNotMatch(ui.$("worker-help").textContent, /工作进程仍在检测/);
    await ui.refresh({...waiting, worker: {...waiting.worker, state: "stale"}});
    assert.equal(ui.$("submit-job").disabled, true);
    assert.match(ui.$("worker-state").textContent, /心跳已过期/);
    assert.doesNotMatch(ui.$("worker-help").textContent, /工作进程仍在检测/);
  },
};

(async () => {
  const selected = process.argv[2] ? [process.argv[2]] : Object.keys(groups);
  for (const group of selected) {
    assert.ok(Object.hasOwn(groups, group), "Unknown test group: " + group);
    await groups[group]();
    console.log("PASS " + group);
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
