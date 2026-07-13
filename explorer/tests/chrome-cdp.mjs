import { access, mkdtemp, rm } from "node:fs/promises";
import { constants } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn, spawnSync } from "node:child_process";

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser"
].filter(Boolean);

async function executable(path) {
  try {
    await access(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

export async function findChrome() {
  for (const candidate of CHROME_CANDIDATES) {
    if (await executable(candidate)) return candidate;
  }
  throw new Error("Chrome or Chromium is required for the real-browser gate; set CHROME_PATH to an executable browser");
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

class CdpClient {
  constructor(url) {
    this.url = url;
    this.socket = null;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
  }

  async connect() {
    this.socket = new WebSocket(this.url);
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("Timed out connecting to Chrome DevTools")), 10000);
      this.socket.addEventListener("open", () => {
        clearTimeout(timeout);
        resolve();
      }, { once: true });
      this.socket.addEventListener("error", () => {
        clearTimeout(timeout);
        reject(new Error("Chrome DevTools WebSocket connection failed"));
      }, { once: true });
    });
    this.socket.addEventListener("message", (event) => this.receive(event.data));
    this.socket.addEventListener("close", () => {
      for (const pending of this.pending.values()) pending.reject(new Error("Chrome DevTools connection closed"));
      this.pending.clear();
    });
    return this;
  }

  receive(raw) {
    const message = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw));
    if (message.id) {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message || "Chrome DevTools command failed"));
      else pending.resolve(message.result || {});
      return;
    }
    for (const listener of this.listeners.get(message.method) || []) listener(message.params || {});
  }

  on(method, listener) {
    if (!this.listeners.has(method)) this.listeners.set(method, new Set());
    this.listeners.get(method).add(listener);
    return () => this.listeners.get(method)?.delete(listener);
  }

  command(method, params = {}, timeoutMs = 15000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Chrome DevTools command timed out: ${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timeout);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timeout);
          reject(error);
        }
      });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    if (this.socket && this.socket.readyState < 2) this.socket.close();
  }
}

async function devtoolsTarget(port) {
  let lastError = null;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const targets = await response.json();
      const page = targets.find((target) => target.type === "page" && target.webSocketDebuggerUrl);
      if (page) return page;
    } catch (error) {
      lastError = error;
    }
    await wait(50);
  }
  throw lastError || new Error("Chrome did not expose a page target");
}

export async function launchChrome() {
  const executablePath = await findChrome();
  const userDataDir = await mkdtemp(join(tmpdir(), "govuk-okf-chrome-"));
  const child = spawn(executablePath, [
    "--headless=new",
    "--remote-debugging-port=0",
    `--user-data-dir=${userDataDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-features=OptimizationHints,Translate",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "about:blank"
  ], { stdio: ["ignore", "ignore", "pipe"] });
  child.stderr.setEncoding("utf8");
  const port = await new Promise((resolve, reject) => {
    let stderr = "";
    const timeout = setTimeout(() => reject(new Error("Chrome did not publish a DevTools endpoint")), 15000);
    child.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
      const match = /DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//.exec(stderr);
      if (!match) return;
      clearTimeout(timeout);
      resolve(Number(match[1]));
    });
    child.once("exit", (code) => {
      if (code !== null) {
        clearTimeout(timeout);
        reject(new Error(`Chrome exited before startup with code ${code}: ${stderr.slice(-1000)}`));
      }
    });
  });
  const target = await devtoolsTarget(port);
  const client = await new CdpClient(target.webSocketDebuggerUrl).connect();
  const network = [];
  const requests = new Map();
  const responses = new Map();
  const consoleErrors = [];
  client.on("Network.requestWillBeSent", (event) => requests.set(event.requestId, {
    url: event.request.url,
    range: String(event.request.headers?.Range || event.request.headers?.range || "")
  }));
  client.on("Network.responseReceived", (event) => responses.set(event.requestId, {
    status: Number(event.response.status || 0),
    content_range: String(event.response.headers?.["content-range"] || event.response.headers?.["Content-Range"] || "")
  }));
  client.on("Network.loadingFinished", (event) => {
    const request = requests.get(event.requestId) || {};
    const response = responses.get(event.requestId) || {};
    network.push({
      url: request.url || "",
      range: request.range || "",
      status: response.status || 0,
      content_range: response.content_range || "",
      encoded_bytes: Number(event.encodedDataLength || 0)
    });
    requests.delete(event.requestId);
    responses.delete(event.requestId);
  });
  client.on("Runtime.exceptionThrown", (event) => consoleErrors.push(event.exceptionDetails?.text || "Uncaught browser exception"));
  await Promise.all([
    client.command("Page.enable"),
    client.command("Runtime.enable"),
    client.command("Network.enable"),
    client.command("Performance.enable")
  ]);

  async function evaluate(expression) {
    const result = await client.command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text || "Browser evaluation failed");
    return result.result?.value;
  }

  async function waitFor(expression, timeoutMs = 15000) {
    const started = Date.now();
    let lastError = null;
    while (Date.now() - started < timeoutMs) {
      try {
        const value = await evaluate(expression);
        if (value) return value;
      } catch (error) {
        lastError = error;
      }
      await wait(25);
    }
    throw lastError || new Error(`Timed out waiting for browser condition: ${expression}`);
  }

  async function navigate(url, readyExpression = "document.readyState === 'complete'") {
    network.splice(0);
    await client.command("Page.navigate", { url });
    await waitFor("document.readyState === 'complete'");
    if (readyExpression) await waitFor(readyExpression, 20000);
  }

  async function key(key, code, windowsVirtualKeyCode) {
    await client.command("Input.dispatchKeyEvent", { type: "rawKeyDown", key, code, windowsVirtualKeyCode });
    await client.command("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode });
  }

  return {
    client,
    consoleErrors,
    executablePath,
    network,
    version: spawnSync(executablePath, ["--version"], { encoding: "utf8" }).stdout.trim(),
    evaluate,
    key,
    navigate,
    waitFor,
    async close() {
      client.close();
      child.kill("SIGTERM");
      await Promise.race([
        new Promise((resolve) => child.once("exit", resolve)),
        wait(3000).then(() => child.kill("SIGKILL"))
      ]);
      await rm(userDataDir, { recursive: true, force: true });
    }
  };
}
