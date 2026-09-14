#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";

const CHROME_CANDIDATES = [
  process.env.CODEX_WEB_CHROME_BIN,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "google-chrome",
  "chromium",
  "chrome",
].filter(Boolean);

const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36";

function readStdin() {
  return new Promise((resolve, reject) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      input += chunk;
    });
    process.stdin.on("end", () => resolve(input));
    process.stdin.on("error", reject);
  });
}

function findChromeBinary() {
  for (const candidate of CHROME_CANDIDATES) {
    if (candidate.includes(path.sep)) {
      if (fs.existsSync(candidate)) {
        return candidate;
      }
      continue;
    }
    return candidate;
  }
  throw new Error("could not find a Chrome binary; set CODEX_WEB_CHROME_BIN");
}

function parseCookieHeader(cookieText) {
  return cookieText
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const eq = part.indexOf("=");
      return eq === -1
        ? null
        : {
            name: part.slice(0, eq).trim(),
            value: part.slice(eq + 1).trim(),
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
      if (match) {
        clearTimeout(timeout);
        child.stderr.off("data", onData);
        resolve(match[1]);
      }
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
    this.events = [];
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", (event) => {
        reject(event.error || new Error("websocket connection failed"));
      }, { once: true });
      this.ws.addEventListener("message", (event) => {
        this.onMessage(event.data);
      });
      this.ws.addEventListener("close", () => {
        const error = new Error("websocket closed");
        for (const { reject: rejectPending } of this.pending.values()) {
          rejectPending(error);
        }
        this.pending.clear();
      });
    });
  }

  onMessage(raw) {
    const message = JSON.parse(raw);
    if (message.id) {
      const pending = this.pending.get(message.id);
      if (!pending) {
        return;
      }
      this.pending.delete(message.id);
      if (message.error) {
        pending.reject(new Error(message.error.message || "cdp request failed"));
      } else {
        pending.resolve(message.result || {});
      }
      return;
    }

    const survivors = [];
    this.events.push(message);
    if (this.events.length > 1000) {
      this.events.shift();
    }
    for (const waiter of this.eventWaiters) {
      if (
        waiter.method === message.method &&
        (waiter.sessionId === undefined || waiter.sessionId === message.sessionId)
      ) {
        waiter.resolve(message.params || {});
        clearTimeout(waiter.timeout);
      } else {
        survivors.push(waiter);
      }
    }
    this.eventWaiters = survivors;
  }

  eventsFor(method, sessionId) {
    return this.events.filter((event) => {
      return (
        event.method === method &&
        (sessionId === undefined || event.sessionId === sessionId)
      );
    });
  }

  async send(method, params = {}, sessionId) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) {
      payload.sessionId = sessionId;
    }
    const response = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.ws.send(JSON.stringify(payload));
    return response;
  }

  waitFor(method, sessionId, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.eventWaiters = this.eventWaiters.filter((entry) => entry !== waiter);
        reject(new Error(`timed out waiting for ${method}`));
      }, timeoutMs);
      const waiter = { method, sessionId, timeout, resolve, reject };
      this.eventWaiters.push(waiter);
    });
  }

  async close() {
    if (!this.ws) {
      return;
    }
    this.ws.close();
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
  }
}

async function launchChrome() {
  const chrome = findChromeBinary();
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-analytics-"));
  const child = spawn(
    chrome,
    [
      "--headless=new",
      "--disable-gpu",
      "--disable-blink-features=AutomationControlled",
      "--no-first-run",
      "--no-default-browser-check",
      "--lang=en-US",
      "--window-size=1440,960",
      `--user-data-dir=${userDataDir}`,
      "--remote-debugging-port=0",
      "about:blank",
    ],
    {
      stdio: ["ignore", "ignore", "pipe"],
    },
  );

  try {
    const browserWsUrl = await waitForDevToolsWs(child);
    return { child, browserWsUrl, userDataDir };
  } catch (error) {
    child.kill("SIGKILL");
    fs.rmSync(userDataDir, { recursive: true, force: true });
    throw error;
  }
}

async function fetchAnalyticsBundle(client, sessionId, config) {
  const expression = `(() => {
    const config = ${JSON.stringify(config)};
    const buildQuery = (extra = {}) => {
      const params = new URLSearchParams({
        start_date: config.date_range.start_date,
        end_date: config.date_range.end_date,
        group_by: config.group_by,
        ...extra,
      });
      return params.toString();
    };
    const fetchJson = async (path, extra = {}) => {
      const response = await fetch(path + "?" + buildQuery(extra), {
        credentials: "include",
        headers: {
          accept: "application/json, text/plain, */*",
          authorization: "Bearer " + config.access_token,
        },
      });
      const text = await response.text();
      if (!response.ok) {
        throw new Error(path + " returned HTTP " + response.status + ": " + text.slice(0, 200));
      }
      return JSON.parse(text);
    };
    const fetchRawJson = async (path) => {
      const response = await fetch(path, {
        credentials: "include",
        headers: {
          accept: "application/json, text/plain, */*",
          authorization: "Bearer " + config.access_token,
        },
      });
      const text = await response.text();
      if (!response.ok) {
        throw new Error(path + " returned HTTP " + response.status + ": " + text.slice(0, 200));
      }
      return JSON.parse(text);
    };
    const fetchDataset = async (path, extra = {}) => {
      try {
        return await fetchJson(path, extra);
      } catch (error) {
        return {
          data: [],
          error: error instanceof Error ? error.message : String(error),
        };
      }
    };
    const fetchRawDataset = async (path) => {
      try {
        return await fetchRawJson(path);
      } catch (error) {
        return {
          data: [],
          error: error instanceof Error ? error.message : String(error),
        };
      }
    };
    return Promise.all([
      fetchDataset("/backend-api/wham/analytics/daily-workspace-usage-counts"),
      fetchDataset("/backend-api/wham/analytics/daily-sessions-messages-counts", {
        include_emails: config.include_emails ? "true" : "false",
      }),
      fetchDataset("/backend-api/wham/analytics/daily-code-review-metrics"),
      fetchRawDataset("/backend-api/wham/usage"),
      fetchRawDataset("/backend-api/wham/usage/credit-usage-events"),
    ]).then(([workspace, sessions, reviews, usage, creditEvents]) => ({
      window_days: config.window_days,
      group_by: config.group_by,
      date_range: config.date_range,
      include_emails: config.include_emails,
      daily_workspace_usage_counts: workspace,
      daily_sessions_messages_counts: sessions,
      daily_code_review_metrics: reviews,
      usage,
      credit_usage_events: creditEvents,
    }));
  })()`;

  const response = await client.send(
    "Runtime.evaluate",
    {
      expression,
      awaitPromise: true,
      returnByValue: true,
    },
    sessionId,
  );

  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.text || "runtime evaluation failed");
  }

  return response.result?.value;
}

async function main() {
  const rawInput = (await readStdin()).trim();
  if (!rawInput) {
    throw new Error("missing JSON payload on stdin");
  }
  const payload = JSON.parse(rawInput);
  if (!payload.cookie) {
    throw new Error("missing cookie header");
  }
  if (!payload.analytics_page_url) {
    throw new Error("missing analytics_page_url");
  }

  const chrome = await launchChrome();
  const browser = new CDPClient(chrome.browserWsUrl);

  try {
    await browser.connect();
    const { targetId } = await browser.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await browser.send("Target.attachToTarget", { targetId, flatten: true });

    await browser.send("Page.enable", {}, sessionId);
    await browser.send("Runtime.enable", {}, sessionId);
    await browser.send("Network.enable", {}, sessionId);
    await browser.send(
      "Network.setUserAgentOverride",
      {
        userAgent: payload.user_agent || DEFAULT_USER_AGENT,
        acceptLanguage: "en-US,en;q=0.9",
        platform: "macOS",
      },
      sessionId,
    );
    await browser.send(
      "Page.addScriptToEvaluateOnNewDocument",
      {
        source: `
          Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          window.chrome = window.chrome || { runtime: {} };
          Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
          Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        `,
      },
      sessionId,
    );

    const seen = new Set();
    for (const cookie of parseCookieHeader(payload.cookie)) {
      const key = `${cookie.name}=${cookie.value}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      await browser.send(
        "Network.setCookie",
        {
          url: payload.origin || "https://chatgpt.com/",
          name: cookie.name,
          value: cookie.value,
          secure: true,
          path: "/",
        },
        sessionId,
      );
    }

    const load = browser.waitFor("Page.loadEventFired", sessionId, 20000);
    await browser.send("Page.navigate", { url: payload.analytics_page_url }, sessionId);
    await load;

    if (payload.debug_network) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const requests = browser
        .eventsFor("Network.requestWillBeSent", sessionId)
        .map((event) => event.params?.request?.url)
        .filter((url) => typeof url === "string")
        .filter((url) => url.includes("/backend-api/") || url.includes("/api/auth/session"));
      process.stdout.write(JSON.stringify({ requests }, null, 2));
      return;
    }

    if (payload.debug_paths) {
      const pathResponse = await browser.send(
        "Runtime.evaluate",
        {
          expression: `Promise.all(
            Array.from(document.querySelectorAll('script[src]'))
              .map((node) => node.src)
              .filter(Boolean)
              .slice(0, 40)
              .map(async (src) => {
                try {
                  const text = await fetch(src, { credentials: 'include' }).then((response) => response.text());
                  return Array.from(
                    new Set(
                      [...text.matchAll(/\\/backend-api\\/wham\\/analytics\\/[a-z0-9-]+/gi)].map((match) => match[0]),
                    ),
                  );
                } catch (_error) {
                  return [];
                }
              }),
          ).then((lists) => Array.from(new Set(lists.flat())).sort())`,
          awaitPromise: true,
          returnByValue: true,
        },
        sessionId,
      );
      process.stdout.write(JSON.stringify({ paths: pathResponse.result?.value || [] }, null, 2));
      return;
    }

    if (payload.debug_page) {
      const pageResponse = await browser.send(
        "Runtime.evaluate",
        {
          expression: `({
            href: location.href,
            title: document.title,
            body: (document.body && document.body.innerText || '').slice(0, 500),
          })`,
          returnByValue: true,
        },
        sessionId,
      );
      process.stdout.write(JSON.stringify(pageResponse.result?.value || {}, null, 2));
      return;
    }

    const sessionResponse = await browser.send(
      "Runtime.evaluate",
      {
        expression: `fetch("/api/auth/session", { credentials: "include" })
          .then(async (response) => ({ status: response.status, text: await response.text() }))`,
        awaitPromise: true,
        returnByValue: true,
      },
      sessionId,
    );
    if (sessionResponse.exceptionDetails) {
      throw new Error(sessionResponse.exceptionDetails.text || "session fetch failed");
    }

    const sessionPayload = sessionResponse.result?.value || {};
    if (sessionPayload.status !== 200) {
      throw new Error(`/api/auth/session returned HTTP ${sessionPayload.status}`);
    }

    const parsedSession = JSON.parse(sessionPayload.text || "{}");
    if (!parsedSession.accessToken) {
      throw new Error("missing accessToken in /api/auth/session response");
    }

    const bundle = await fetchAnalyticsBundle(browser, sessionId, {
      ...payload,
      access_token: parsedSession.accessToken,
    });
    process.stdout.write(JSON.stringify(bundle));
  } finally {
    await browser.close().catch(() => {});
    chrome.child.kill("SIGKILL");
    // Wait briefly for the OS to release file handles after SIGKILL,
    // then retry cleanup to avoid ENOTEMPTY on macOS.
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
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exit(1);
});
