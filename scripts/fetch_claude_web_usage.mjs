#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";

const CHROME_CANDIDATES = [
  process.env.CLAUDE_WEB_CHROME_BIN,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "google-chrome",
  "chromium",
  "chrome",
].filter(Boolean);

function readStdin() {
  return new Promise((resolve, reject) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { input += chunk; });
    process.stdin.on("end", () => resolve(input));
    process.stdin.on("error", reject);
  });
}

function findChromeBinary() {
  for (const candidate of CHROME_CANDIDATES) {
    if (!candidate.includes(path.sep) || fs.existsSync(candidate)) {
      return candidate;
    }
  }
  throw new Error("could not find a Chrome binary; set CLAUDE_WEB_CHROME_BIN");
}

function parseCookieHeader(cookieText) {
  return cookieText
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const separator = part.indexOf("=");
      return separator < 0 ? null : {
        name: part.slice(0, separator).trim(),
        value: part.slice(separator + 1).trim(),
      };
    })
    .filter((cookie) => cookie && cookie.name);
}

function waitForDevToolsWs(child) {
  return new Promise((resolve, reject) => {
    let stderr = "";
    const timeout = setTimeout(() => {
      reject(new Error("timed out waiting for Chrome DevTools endpoint"));
    }, 10000);
    const onData = (chunk) => {
      stderr += chunk.toString();
      const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (!match) return;
      clearTimeout(timeout);
      child.stderr.off("data", onData);
      resolve(match[1]);
    };
    child.stderr.on("data", onData);
    child.once("exit", (code, signal) => {
      clearTimeout(timeout);
      reject(new Error(`Chrome exited before DevTools was ready (${code ?? signal ?? "unknown"})`));
    });
  });
}

class CDPClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map();
    this.eventWaiters = [];
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", (event) => {
        reject(event.error || new Error("websocket connection failed"));
      }, { once: true });
      this.ws.addEventListener("message", (event) => this.onMessage(event.data));
    });
  }

  onMessage(raw) {
    const message = JSON.parse(raw);
    if (message.id) {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) {
        pending.reject(new Error(message.error.message || "CDP request failed"));
      } else {
        pending.resolve(message.result || {});
      }
      return;
    }
    const survivors = [];
    for (const waiter of this.eventWaiters) {
      if (waiter.method === message.method &&
          (waiter.sessionId === undefined || waiter.sessionId === message.sessionId)) {
        clearTimeout(waiter.timeout);
        waiter.resolve(message.params || {});
      } else {
        survivors.push(waiter);
      }
    }
    this.eventWaiters = survivors;
  }

  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    const response = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.ws.send(JSON.stringify(payload));
    return response;
  }

  waitFor(method, sessionId, timeoutMs = 20000) {
    return new Promise((resolve, reject) => {
      const waiter = { method, sessionId, resolve, reject, timeout: null };
      waiter.timeout = setTimeout(() => {
        this.eventWaiters = this.eventWaiters.filter((entry) => entry !== waiter);
        reject(new Error(`timed out waiting for ${method}`));
      }, timeoutMs);
      this.eventWaiters.push(waiter);
    });
  }

  async close() {
    if (!this.ws) return;
    this.ws.close();
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

async function launchChrome() {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "claude-usage-"));
  const child = spawn(findChromeBinary(), [
    "--headless=new",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--lang=en-US",
    "--window-size=1200,900",
    `--user-data-dir=${userDataDir}`,
    "--remote-debugging-port=0",
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });

  try {
    return { child, userDataDir, browserWsUrl: await waitForDevToolsWs(child) };
  } catch (error) {
    child.kill("SIGKILL");
    fs.rmSync(userDataDir, { recursive: true, force: true });
    throw error;
  }
}

async function fetchClaudeBundle(client, sessionId, organizationId) {
  const expression = `(() => {
    const organizationId = ${JSON.stringify(organizationId)};
    const base = "/api/organizations/" + organizationId;
    const fetchJson = async (path) => {
      const response = await fetch(path, {
        credentials: "include",
        headers: { accept: "application/json, text/plain, */*" },
      });
      const text = await response.text();
      if (!response.ok) {
        throw new Error(path + " returned HTTP " + response.status + ": " + text.slice(0, 160));
      }
      return JSON.parse(text);
    };
    const optional = async (path) => {
      try {
        return { data: await fetchJson(path), error: null };
      } catch (error) {
        return { data: null, error: error instanceof Error ? error.message : String(error) };
      }
    };
    return Promise.all([
      fetchJson(base + "/usage"),
      optional(base + "/overage_spend_limit"),
      optional(base + "/prepaid/credits"),
    ]).then(([usage, overage, prepaid]) => ({ usage, overage, prepaid }));
  })()`;
  const response = await client.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, sessionId);
  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.text || "Claude browser fetch failed");
  }
  return response.result?.value;
}

async function main() {
  const rawInput = (await readStdin()).trim();
  if (!rawInput) throw new Error("missing JSON payload on stdin");
  const payload = JSON.parse(rawInput);
  if (!payload.cookie) throw new Error("missing cookie header");
  if (!payload.organization_id) throw new Error("missing organization id");

  const chrome = await launchChrome();
  const browser = new CDPClient(chrome.browserWsUrl);
  try {
    await browser.connect();
    const { targetId } = await browser.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await browser.send("Target.attachToTarget", { targetId, flatten: true });
    await browser.send("Page.enable", {}, sessionId);
    await browser.send("Runtime.enable", {}, sessionId);
    await browser.send("Network.enable", {}, sessionId);
    if (payload.user_agent) {
      await browser.send("Network.setUserAgentOverride", {
        userAgent: payload.user_agent,
        acceptLanguage: "en-US,en;q=0.9",
        platform: "macOS",
      }, sessionId);
    }
    await browser.send("Page.addScriptToEvaluateOnNewDocument", {
      source: `
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = window.chrome || { runtime: {} };
      `,
    }, sessionId);

    const seen = new Set();
    for (const cookie of parseCookieHeader(payload.cookie)) {
      if (seen.has(cookie.name)) continue;
      seen.add(cookie.name);
      await browser.send("Network.setCookie", {
        url: "https://claude.ai/",
        name: cookie.name,
        value: cookie.value,
        secure: true,
        path: "/",
      }, sessionId);
    }

    const loaded = browser.waitFor("Page.loadEventFired", sessionId);
    await browser.send("Page.navigate", {
      url: payload.page_url || "https://claude.ai/new#settings/usage",
    }, sessionId);
    await loaded;
    const bundle = await fetchClaudeBundle(browser, sessionId, payload.organization_id);
    process.stdout.write(JSON.stringify(bundle));
  } finally {
    await browser.close().catch(() => {});
    chrome.child.kill("SIGKILL");
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        fs.rmSync(chrome.userDataDir, { recursive: true, force: true });
        break;
      } catch (_) {
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
    }
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(1);
});
