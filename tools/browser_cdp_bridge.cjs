// ローカルUIの実ブラウザー検査専用のChromium操作ブリッジ（GH-01）。
//
// 用途を限定する:
// * 呼出し元は tools/run_ui_browser_check.py だけ。stdinの1行JSONを1命令として順に実行し、
//   stdoutへ1行JSONで結果を返す。
// * 開けるURLは http://127.0.0.1:<port>/ だけ。外部URL・file:// は拒否する。
// * npm依存を持たない。Node 22以上の組込みWebSocketでChrome DevTools Protocolを直接使う。
//   Chromium実行体は呼出し元が明示する。探索・ダウンロードはしない。
// * 画面の値はtextContent等で読むだけ。Session Tokenや本文をstdout/stderrへ書かない。

"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const readline = require("node:readline");

const LOOPBACK = /^http:\/\/127\.0\.0\.1:\d{1,5}\//;
const COMMAND_TIMEOUT_MS = 30000;
//: Chromium停止の猶予。SIGTERMの後これだけ待ち、終わらなければSIGKILLする。
const STOP_GRACE_MS = 5000;

if (typeof WebSocket !== "function") {
  process.stdout.write(JSON.stringify({ fatal: "NODE_WEBSOCKET_UNAVAILABLE" }) + "\n");
  process.exit(3);
}

const [chromium, profileDir, launchTimeout, ...extraFlags] = process.argv.slice(2);
//: Chromiumの起動とCDP接続それぞれの上限。呼出し元が明示する。
const LAUNCH_TIMEOUT_MS = Number(launchTimeout);
if (!chromium || !profileDir || !Number.isInteger(LAUNCH_TIMEOUT_MS) ||
    LAUNCH_TIMEOUT_MS < 100 || LAUNCH_TIMEOUT_MS > 120000) {
  process.stdout.write(JSON.stringify({ fatal: "USAGE: browser_cdp_bridge.cjs <chromium> <profile-dir> <launch-timeout-ms> [flags...]" }) + "\n");
  process.exit(2);
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = [];
    socket.addEventListener("message", (event) => this.receive(JSON.parse(event.data)));
  }

  receive(message) {
    if (message.id !== undefined) {
      const waiter = this.pending.get(message.id);
      if (!waiter) { return; }
      this.pending.delete(message.id);
      clearTimeout(waiter.timer);
      if (message.error) {
        waiter.reject(new Error(waiter.method + ": " + message.error.message));
      } else {
        waiter.resolve(message.result || {});
      }
      return;
    }
    for (const handler of this.handlers) {
      handler(message.method, message.params || {}, message.sessionId);
    }
  }

  send(method, params, sessionId) {
    const id = this.nextId++;
    const message = { id, method, params: params || {} };
    if (sessionId) { message.sessionId = sessionId; }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(method + ": timeout"));
      }, COMMAND_TIMEOUT_MS);
      this.pending.set(id, { resolve, reject, timer, method });
      this.socket.send(JSON.stringify(message));
    });
  }

  on(handler) { this.handlers.push(handler); }
}

function exited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function waitExit(child, ms) {
  if (exited(child)) { return Promise.resolve(true); }
  return new Promise((resolve) => {
    const onExit = () => { clearTimeout(timer); resolve(true); };
    const timer = setTimeout(() => { child.removeListener("exit", onExit); resolve(exited(child)); }, ms);
    child.once("exit", onExit);
  });
}

// 起動したChromiumを必ず終わらせ、回収（exit観測）まで待つ。SIGTERM → 猶予 → SIGKILL。
async function stopChild(child) {
  if (!child || child.pid === undefined || exited(child)) { return; }
  try { child.kill("SIGTERM"); } catch (_) { /* 既に終了 */ }
  if (await waitExit(child, STOP_GRACE_MS)) { return; }
  try { child.kill("SIGKILL"); } catch (_) { /* 既に終了 */ }
  if (!(await waitExit(child, STOP_GRACE_MS))) {
    throw new Error("CHROMIUM_NOT_REAPED pid=" + child.pid);
  }
}

// 失敗時は、起動したProcessを止めてから**元の理由**で失敗させる。後始末の失敗は付記する。
function launch() {
  const flags = [
    "--headless=new",
    "--remote-debugging-port=0",
    "--user-data-dir=" + profileDir,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--no-proxy-server",
    "--disable-gpu",
    "--mute-audio",
    "--lang=ja-JP",
    ...extraFlags,
    "about:blank",
  ];
  let child;
  try {
    // Chromiumのcrash reporter等はXDG_CONFIG_HOME/XDG_CACHE_HOMEの下へ書く。既定では利用者の
    // ~/.config/chromium を使うので、呼出し元が用意した新規profileの下へ閉じ込める。
    // （crashpad handlerはsetsidでProcess Groupの外へ出る。profile Pathが引数に入るので、
    //   呼出し元はそれで身元を確かめて、終わったことを確認できる。）
    const env = Object.assign({}, process.env, {
      XDG_CONFIG_HOME: profileDir + "/xdg-config",
      XDG_CACHE_HOME: profileDir + "/xdg-cache",
    });
    child = spawn(chromium, flags, { stdio: ["ignore", "ignore", "pipe"], env });
  } catch (error) {
    return Promise.reject(new Error("CHROMIUM_SPAWN_FAILED: " + (error.code || error.message)));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let buffer = "";
    const fail = (reason) => {
      if (settled) { return; }
      settled = true;
      clearTimeout(timer);
      stopChild(child).then(
        () => reject(new Error(reason)),
        (cleanup) => reject(new Error(reason + " (cleanup: " + cleanup.message + ")")),
      );
    };
    const timer = setTimeout(() => fail("CHROMIUM_START_TIMEOUT"), LAUNCH_TIMEOUT_MS);
    child.on("error", (error) => fail("CHROMIUM_SPAWN_FAILED: " + error.code));
    child.on("exit", (code, signal) => fail("CHROMIUM_EXITED: " + (code === null ? signal : code)));
    child.stderr.on("data", (chunk) => {
      // 起動後も読み続ける（Pipeが詰まってChromiumが止まらないように）。
      if (settled) { return; }
      buffer = (buffer + chunk.toString("utf8")).slice(-65536);
      const match = buffer.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) {
        settled = true;
        clearTimeout(timer);
        resolve({ child, endpoint: match[1] });
      }
    });
  });
}

function connect(endpoint) {
  return new Promise((resolve, reject) => {
    let socket;
    try {
      socket = new WebSocket(endpoint);
    } catch (_) {
      reject(new Error("CDP_CONNECT_FAILED"));
      return;
    }
    let settled = false;
    const settle = (outcome) => {
      if (settled) { return false; }
      settled = true;
      clearTimeout(timer);
      outcome();
      return true;
    };
    // 理由を先に確定する。接続中の close() はその場で error を発火するため、
    // 後に呼ぶと「接続timeout」が「接続失敗」に上書きされる。
    const timer = setTimeout(() => {
      if (settle(() => reject(new Error("CDP_CONNECT_TIMEOUT")))) {
        try { socket.close(); } catch (_) { /* 未接続 */ }
      }
    }, LAUNCH_TIMEOUT_MS);
    socket.addEventListener("open", () => { settle(() => resolve(socket)); }, { once: true });
    socket.addEventListener("error", () => { settle(() => reject(new Error("CDP_CONNECT_FAILED"))); }, { once: true });
  });
}

const tabs = new Map();
let cdp = null;
let browserProcess = null;

function tabOf(name) {
  const tab = tabs.get(name);
  if (!tab) { throw new Error("unknown tab " + name); }
  return tab;
}

function onEvent(method, params, sessionId) {
  const tab = Array.from(tabs.values()).find((item) => item.sessionId === sessionId);
  if (!tab) { return; }
  if (method === "Page.loadEventFired") {
    const waiters = tab.loadWaiters.splice(0);
    waiters.forEach((resolve) => resolve());
  } else if (method === "Runtime.consoleAPICalled") {
    const text = (params.args || []).map((arg) => arg.value !== undefined ? String(arg.value) : (arg.description || "")).join(" ");
    tab.events.push({ kind: "console", level: params.type, text });
  } else if (method === "Runtime.exceptionThrown") {
    const details = params.exceptionDetails || {};
    tab.events.push({ kind: "exception", level: "error", text: (details.exception && details.exception.description) || details.text || "" });
  } else if (method === "Log.entryAdded") {
    const entry = params.entry || {};
    tab.events.push({ kind: "log", level: entry.level, source: entry.source, text: entry.text || "" });
  } else if (method === "Network.requestWillBeSent") {
    const request = params.request || {};
    tab.requests.push({ method: request.method, path: new URL(request.url).pathname, at: Date.now() });
  } else if (method === "Fetch.requestPaused") {
    handlePaused(tab, params);
  }
}

function handlePaused(tab, params) {
  const path = new URL(params.request.url).pathname;
  const rule = tab.rules.find((item) => item.path === path && item.method === params.request.method && item.remaining > 0);
  const requestId = params.requestId;
  if (!rule) {
    cdp.send("Fetch.continueRequest", { requestId }, tab.sessionId).catch(() => {});
    return;
  }
  rule.remaining -= 1;
  tab.intercepted.push({ path, method: params.request.method, mode: rule.mode, response_status: params.responseStatusCode || null });
  if (rule.mode === "fail_after_response") {
    // サーバーは処理を終えている。応答だけをブラウザーへ届けず通信切断として扱わせる。
    cdp.send("Fetch.failRequest", { requestId, errorReason: "ConnectionReset" }, tab.sessionId).catch(() => {});
  } else {
    setTimeout(() => {
      cdp.send("Fetch.continueRequest", { requestId }, tab.sessionId).catch(() => {});
    }, rule.ms);
  }
}

async function evaluate(tab, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  }, tab.sessionId);
  if (result.exceptionDetails) {
    throw new Error("evaluate: " + ((result.exceptionDetails.exception && result.exceptionDetails.exception.description) || result.exceptionDetails.text));
  }
  return result.result ? result.result.value : undefined;
}

async function setViewport(tab, command) {
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: command.width || 1280,
    height: command.height || 900,
    deviceScaleFactor: 1,
    mobile: false,
  }, tab.sessionId);
  await cdp.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-color-scheme", value: command.dark ? "dark" : "light" }],
  }, tab.sessionId);
}

function waitLoad(tab) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("load timeout")), COMMAND_TIMEOUT_MS);
    tab.loadWaiters.push(() => { clearTimeout(timer); resolve(); });
  });
}

async function locate(tab, selector) {
  return evaluate(tab, `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) { return null; }
    el.scrollIntoView({block: "center", inline: "center"});
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    return {x, y, width: r.width, height: r.height, disabled: !!el.disabled,
      hit: !!hit && (hit === el || el.contains(hit) || (hit.control === el))};
  })()`);
}

async function mouseClick(tab, box, clickCount) {
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: box.x, y: box.y }, tab.sessionId);
  for (let count = 1; count <= clickCount; count++) {
    await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: count }, tab.sessionId);
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x, y: box.y, button: "left", clickCount: count }, tab.sessionId);
  }
}

const KEYS = {
  ArrowRight: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 },
  Tab: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
  Space: { key: " ", code: "Space", windowsVirtualKeyCode: 32, text: " " },
  Enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" },
};

const handlers = {
  async open(command) {
    if (!LOOPBACK.test(command.url)) { throw new Error("only http://127.0.0.1 URLs may be opened"); }
    if (tabs.has(command.tab)) { throw new Error("tab already exists"); }
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    const tab = { name: command.tab, targetId, sessionId, events: [], requests: [], intercepted: [], rules: [], loadWaiters: [] };
    tabs.set(command.tab, tab);
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Runtime.enable", {}, sessionId);
    await cdp.send("Log.enable", {}, sessionId);
    await cdp.send("Network.enable", {}, sessionId);
    await setViewport(tab, command);
    const loaded = waitLoad(tab);
    await cdp.send("Page.navigate", { url: command.url }, sessionId);
    await loaded;
    return { opened: true };
  },
  async reload(command) {
    const tab = tabOf(command.tab);
    const loaded = waitLoad(tab);
    await cdp.send("Page.reload", { ignoreCache: true }, tab.sessionId);
    await loaded;
    return { reloaded: true };
  },
  async activate(command) {
    await cdp.send("Page.bringToFront", {}, tabOf(command.tab).sessionId);
    return { activated: true };
  },
  async viewport(command) {
    await setViewport(tabOf(command.tab), command);
    return { width: command.width, height: command.height, dark: !!command.dark };
  },
  async eval(command) {
    return evaluate(tabOf(command.tab), command.expr);
  },
  async click(command) {
    const tab = tabOf(command.tab);
    const box = await locate(tab, command.selector);
    if (!box) { throw new Error("element not found: " + command.selector); }
    await mouseClick(tab, box, command.count || 1);
    return box;
  },
  async key(command) {
    const tab = tabOf(command.tab);
    const spec = KEYS[command.key];
    if (!spec) { throw new Error("unsupported key " + command.key); }
    const modifiers = command.shift ? 8 : 0;
    await cdp.send("Input.dispatchKeyEvent", Object.assign({ type: "keyDown", modifiers }, spec), tab.sessionId);
    await cdp.send("Input.dispatchKeyEvent", Object.assign({ type: "keyUp", modifiers }, spec, { text: undefined }), tab.sessionId);
    return { key: command.key };
  },
  async intercept(command) {
    const tab = tabOf(command.tab);
    tab.rules.push({ path: command.path, method: command.method || "POST", mode: command.mode, ms: command.ms || 0, remaining: command.times || 1 });
    const patterns = tab.rules.map((rule) => ({ urlPattern: "*" + rule.path, requestStage: "Response" }));
    await cdp.send("Fetch.enable", { patterns }, tab.sessionId);
    return { rules: tab.rules.length };
  },
  async clear_intercepts(command) {
    const tab = tabOf(command.tab);
    tab.rules = [];
    await cdp.send("Fetch.disable", {}, tab.sessionId);
    return { cleared: true };
  },
  async artifact_downloads(command) {
    // Test downloads stay inside the explicitly created browser profile.
    const directory = fs.realpathSync(profileDir) + "/artifact-downloads";
    fs.mkdirSync(directory, { recursive: true });
    await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: directory });
    if (!LOOPBACK.test(command.origin + "/")) { throw new Error("clipboard origin must be loopback"); }
    await cdp.send("Browser.grantPermissions", { origin: command.origin,
      permissions: ["clipboardReadWrite", "clipboardSanitizedWrite"] });
    return { directory };
  },
  async observations(command) {
    const tab = tabOf(command.tab);
    const result = { events: tab.events.splice(0), requests: tab.requests.splice(0), intercepted: tab.intercepted.splice(0) };
    return result;
  },
  async screenshot(command) {
    const tab = tabOf(command.tab);
    // 背景タブは描画が止まり撮影が終わらない。撮影対象だけを前面へ出す。
    await cdp.send("Page.bringToFront", {}, tab.sessionId);
    const params = { format: "png" };
    if (command.full) {
      const metrics = await cdp.send("Page.getLayoutMetrics", {}, tab.sessionId);
      const size = metrics.cssContentSize || metrics.contentSize;
      params.captureBeyondViewport = true;
      params.clip = { x: 0, y: 0, width: Math.ceil(size.width), height: Math.min(Math.ceil(size.height), 16000), scale: 1 };
    }
    const shot = await cdp.send("Page.captureScreenshot", params, tab.sessionId);
    fs.writeFileSync(command.path, Buffer.from(shot.data, "base64"), { flag: "wx" });
    return { path: command.path };
  },
  async close_tab(command) {
    const tab = tabOf(command.tab);
    await cdp.send("Target.closeTarget", { targetId: tab.targetId });
    tabs.delete(command.tab);
    return { closed: true };
  },
  async version() {
    const info = await cdp.send("Browser.getVersion", {});
    return { product: info.product, protocol: info.protocolVersion, node: process.version };
  },
};

let closing = null;
function shutdown() {
  if (closing) { return closing; }
  closing = (async () => {
    if (cdp) {
      // 応答しないBrowserを待ち続けない。閉じられなければ下でProcessごと止める。
      await Promise.race([
        cdp.send("Browser.close", {}).catch(() => {}),
        new Promise((resolve) => setTimeout(resolve, 3000)),
      ]);
      try { cdp.socket.close(); } catch (_) { /* 切断済み */ }
    }
    await stopChild(browserProcess);
  })();
  return closing;
}

// 元の失敗理由を先に確定し、後始末（Chromiumの停止・回収）の失敗は付記してから終了する。
async function fatalExit(reason, code) {
  let detail = String(reason);
  try {
    await shutdown();
  } catch (cleanup) {
    detail += " (cleanup: " + cleanup.message + ")";
  }
  process.stdout.write(JSON.stringify({ fatal: detail }) + "\n", () => process.exit(code));
}

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => { fatalExit("BRIDGE_" + signal, 1); });
}

async function main() {
  let started;
  try {
    started = await launch();
  } catch (error) {
    // launch() は起動したChromiumを止めてから失敗する。
    process.stdout.write(JSON.stringify({ fatal: error.message }) + "\n", () => process.exit(3));
    return;
  }
  browserProcess = started.child;
  let socket;
  try {
    socket = await connect(started.endpoint);
  } catch (error) {
    await fatalExit(error.message, 3);
    return;
  }
  cdp = new Cdp(socket);
  cdp.on(onEvent);
  process.stdout.write(JSON.stringify({ ready: true }) + "\n");

  const input = readline.createInterface({ input: process.stdin });
  for await (const line of input) {
    if (!line.trim()) { continue; }
    const command = JSON.parse(line);
    if (command.op === "quit") {
      try {
        await shutdown();
      } catch (error) {
        process.stdout.write(JSON.stringify({ id: command.id, ok: false, error: error.message }) + "\n", () => process.exit(1));
        return;
      }
      process.stdout.write(JSON.stringify({ id: command.id, ok: true, result: { quit: true } }) + "\n", () => process.exit(0));
      return;
    }
    try {
      const handler = handlers[command.op];
      if (!handler) { throw new Error("unknown op " + command.op); }
      const result = await handler(command);
      process.stdout.write(JSON.stringify({ id: command.id, ok: true, result: result === undefined ? null : result }) + "\n");
    } catch (error) {
      process.stdout.write(JSON.stringify({ id: command.id, ok: false, error: String(error.message || error) }) + "\n");
    }
  }
  // 呼出し元がstdinを閉じた（終了・異常終了）。Chromiumを残さない。
  try {
    await shutdown();
  } catch (error) {
    process.stdout.write(JSON.stringify({ fatal: error.message }) + "\n", () => process.exit(1));
    return;
  }
  process.exit(0);
}

main().catch((error) => fatalExit(String(error.message || error), 1));
