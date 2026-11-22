import { runPageJob } from "./page.js";

const defaultPort = 12450;
const siteOrigin = "https://codeforces.com";
const alarmName = "check-server";
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let running = false;
let inFlight = false;
let warmTimer;
let activity = "waiting";
let loginOutcome;
let previousEndpoint;

async function ensureAlarm() {
  if (!(await chrome.alarms.get(alarmName))) {
    await chrome.alarms.create(alarmName, { periodInMinutes: 0.5 });
  }
}

chrome.runtime.onInstalled.addListener(() => {
  void ensureAlarm();
  void chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
});
chrome.runtime.onStartup.addListener(() => {
  void ensureAlarm();
  void poll();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === alarmName) void poll();
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || message.type !== "poll") return false;
  void ensureAlarm();
  poll().then((state) => reply({ state }), () => reply({ state: "no_tunnel" }));
  return true;
});

function bridgeRequest(port, path, key, body) {
  const headers = { Authorization: `Bearer ${key}` };
  if (body) headers["Content-Type"] = "application/json";
  return fetch(`http://127.0.0.1:${port}${path}`, {
    method: body ? "POST" : "GET",
    headers,
    body: body ? JSON.stringify(body) : undefined,
    credentials: "omit",
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(5000),
  });
}

async function poll() {
  if (running) return activity;
  running = true;
  activity = "waiting";
  try {
    const { pairingCode, bridgePort } = await chrome.storage.local.get(["pairingCode", "bridgePort"]);
    if (!pairingCode) return "idle";
    const port = Number.isInteger(bridgePort) && bridgePort > 0 && bridgePort <= 65535 ? bridgePort : defaultPort;
    const endpoint = `${port}:${pairingCode}`;
    if (endpoint !== previousEndpoint) {
      loginOutcome = undefined;
      previousEndpoint = endpoint;
    }
    const response = await bridgeRequest(port, "/job", pairingCode);
    if (response.status === 204) return "idle";
    if (response.status === 401) return "unauthorized";
    if (!response.ok) return "no_tunnel";
    const job = await response.json();
    loginOutcome = undefined;
    // Release the popup while the user logs in. Keep one claimed job at a time.
    void finish(job, pairingCode, port);
    return "waiting";
  } catch {
    return loginOutcome || "no_tunnel";
  } finally {
    if (!inFlight) running = false;
  }
}

async function finish(job, key, port) {
  inFlight = true;
  try {
    const result = await dispatch(job);
    if (job.kind === "login" && result.status === "ok") activity = "confirming_login";
    const response = await bridgeRequest(port, "/result", key, { id: job.id, result });
    if (job.kind === "login") {
      loginOutcome = response.status === 204
        ? result.status === "ok" ? "login_confirmed" : "login_expired"
        : "confirmation_lost";
    }
  } catch {
    // A lost result after a submit remains unknown to the server. Never replay a job.
    if (job.kind === "login") loginOutcome = "confirmation_lost";
  } finally {
    inFlight = false;
    running = false;
    if (job.kind === "preflight") watchForConfirmation();
  }
}

function watchForConfirmation() {
  clearInterval(warmTimer);
  const end = Date.now() + 24_000;
  // Poll quickly while the terminal displays the confirmation prompt.
  warmTimer = setInterval(() => {
    if (Date.now() >= end) clearInterval(warmTimer);
    else void poll();
  }, 1_000);
}

async function waitForPage(tabId, deadline) {
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return tab;
    await wait(350);
  }
  return null;
}

async function inspect(tab, kind, payload, deadline) {
  if (!tab?.url) return { status: "permission" };
  if (new URL(tab.url).origin !== siteOrigin) return { status: "otherSite" };
  const [execution] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: runPageJob,
    args: [kind, payload, deadline],
  });
  return execution?.result || { status: kind === "submit" ? "unknown" : "structure" };
}

async function inspectLogin(tab, payload, deadline) {
  try {
    return await inspect(tab, "login", payload, deadline);
  } catch (error) {
    const permission = /permission|extensions gallery|cannot access (contents|a chrome)/i.test(error.message);
    return { status: permission ? "permission" : "inspectionFailed" };
  }
}

const loginActivity = {
  verification: "login_verification",
  wrongAccount: "login_wrong_account",
  authentication: "login_needed",
  permission: "login_permission",
  otherSite: "login_other_site",
  accountMarkup: "login_markup",
  inspectionFailed: "login_inspection_failed",
  structure: "login_inspection_failed",
};

async function waitForLogin(tabId, payload, deadline) {
  while (Date.now() < deadline - 2_000) {
    const opened = await chrome.tabs.get(tabId).catch(() => null);
    const ownTab = opened?.status === "complete"
      ? await inspectLogin(opened, payload, deadline) : null;
    if (ownTab?.status === "ok") return ownTab;
    activity = loginActivity[ownTab?.status] || "login_loading";
    if (ownTab?.status === "verification") {
      await wait(1_300);
      continue;
    }
    // A user may have finished signing in on a different Codeforces tab in this profile.
    const tabs = await chrome.tabs.query({ url: siteOrigin + "/*", status: "complete" }).catch(() => []);
    for (const tab of tabs) {
      if (tab.id === tabId || (opened && tab.incognito !== opened.incognito)) continue;
      const state = await inspectLogin(tab, payload, deadline);
      if (state?.status === "ok") return state;
      if (state?.status === "wrongAccount") activity = "login_wrong_account";
      else if (activity === "login_needed" && state?.status === "accountMarkup") activity = "login_markup";
    }
    await wait(1_300);
  }
  return { status: "authentication" };
}

async function dispatch(job) {
  if (!["login", "problems", "preflight", "submit"].includes(job.kind)
      || typeof job.remainingMs !== "number") return { status: "structure" };
  const payload = job.payload;
  if (job.kind !== "login" && !/^\/(contest|gym)\/[1-9]\d{0,9}$/.test(payload.path)) {
    return { status: "structure" };
  }
  const deadline = Date.now() + job.remainingMs;
  const path = job.kind === "login"
    ? "/enter"
    : job.kind === "problems" ? payload.path : payload.path + "/submit";
  let tabId;
  let keepTab = job.kind === "login";
  let started = false;
  try {
    if (job.kind === "login" && payload.useOpenTab) {
      const tabs = await chrome.tabs.query({ url: siteOrigin + "/*" }).catch(() => []);
      const existing = tabs.find((tab) => tab.url && new URL(tab.url).pathname === "/enter")
        || tabs.find((tab) => tab.active);
      if (existing) return await waitForLogin(existing.id, payload, deadline);
    }
    tabId = (await chrome.tabs.create({ url: siteOrigin + path + "?locale=en", active: keepTab })).id;
    if (job.kind === "login") return await waitForLogin(tabId, payload, deadline);
    const tab = await waitForPage(tabId, Math.min(deadline, Date.now() + 17_000));
    if (!tab) return { status: "network" };
    started = true;
    const result = await inspect(tab, job.kind, payload, deadline);
    keepTab = ["verification", "authentication", "access", "rejected", "unknown", "unknownVerification"].includes(result.status);
    if (keepTab) {
      if (result.status === "unknownVerification") {
        await chrome.tabs.update(tabId, { url: siteOrigin + payload.path + "/my", active: true });
      } else {
        await chrome.tabs.update(tabId, { active: true });
      }
    }
    return result;
  } catch {
    return { status: started && job.kind === "submit" ? "unknown" : "network" };
  } finally {
    if (tabId !== undefined && !keepTab) await chrome.tabs.remove(tabId).catch(() => {});
  }
}
