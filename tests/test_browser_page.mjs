import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import { runPageJob } from "../browser_extension/page.js";

const base = "https://codeforces.com";
const path = "/gym/104172";
const handle = "testuser";
const language = "C++23 (GCC 14-64, msys2)";
const cssScript = "script:not([src])";

function setPage(t, page, href = base + path + "/submit") {
  t.mock.method(globalThis, "FormData", function FakeFormData(form) {
    return new Map(form.defaults);
  });
  const previous = {
    location: globalThis.location,
    document: globalThis.document,
    DOMParser: globalThis.DOMParser,
  };
  globalThis.location = { origin: new URL(href).origin, href };
  globalThis.document = page;
  globalThis.DOMParser = class {
    parseFromString(value) {
      return value;
    }
  };
  t.after(() => Object.assign(globalThis, previous));
}

function page({
  title = "Codeforces", account = handle, challenge = false, detectionScript = false, form, success = false,
  rows = [], topRightProfile, signInLink = false,
} = {}) {
  const scripts = account ? [{ textContent: `Codeforces.handle = "${account}";` }] : [];
  if (success) scripts.push({ textContent: 'Codeforces.showMessage("Your solution has been submitted successfully");' });
  return {
    title,
    forms: form ? [form] : [],
    querySelector(selector) {
      if (selector.startsWith("#challenge-form")) {
        const matchedDetection = detectionScript && selector.includes("script[src*='/cdn-cgi/challenge-platform/']");
        return challenge || matchedDetection ? {} : null;
      }
      if (selector === ".lang-chooser" && topRightProfile) {
        return {
          querySelector: () => signInLink ? {} : null,
          querySelectorAll: () => [{
            getAttribute: () => "/profile/" + topRightProfile,
            textContent: topRightProfile,
          }],
        };
      }
      if (selector === "form input[name=handleOrEmail], .lang-chooser a[href^='/enter']") {
        return signInLink ? {} : null;
      }
      if (selector === "table.status-frame-datatable") return { querySelector: () => null };
      return null;
    },
    querySelectorAll(selector) {
      if (selector === cssScript) return scripts;
      if (selector === "tr[data-submission-id]") {
        return rows.map((id) => ({
          getAttribute: () => String(id),
          querySelectorAll: () => [{ getAttribute: () => path + "/problem/A" }],
          cells: [{ textContent: language }],
        }));
      }
      return [];
    },
  };
}

function makeForm({ enctype = "application/x-www-form-urlencoded", otherOption = false } = {}) {
  const option = (value, textContent) => ({ value, textContent, closest: () => null });
  function field(name, selector, options = [], value = "") {
    return { name, options, value, matches: (s) => s === selector };
  }
  const languageOptions = [option("95", language), option("73", "GNU G++20")];
  if (otherOption) languageOptions.push(option("91", "GNU G++23"));
  const elements = [
    field("csrf_token", "input[type=hidden]", [], "fake-csrf"),
    field("action", "input[type=hidden]", [], "submitSolutionFormSubmitted"),
    field("contestId", "input[type=hidden]", [], "104172"),
    field("programTypeId", "select", languageOptions),
    field("submittedProblemIndex", "select", [option("A", "A")]),
    field("source", "textarea"),
  ];
  elements.namedItem = (name) => elements.find((element) => element.name === name);
  const attributes = { method: "post", enctype, action: base + path + "/dynamic?token=fake-csrf" };
  return {
    elements, action: elements.namedItem("action"),
    defaults: [
      ["action", "submitSolutionFormSubmitted"], ["csrf_token", "fake-csrf"], ["dynamicField", "new value"],
    ],
    getAttribute: (name) => attributes[name] ?? null,
    querySelector: () => null,
  };
}

function decodedResponse(pageObject, { status = 200, type = "basic", challenge = false } = {}) {
  return {
    type, status, ok: status >= 200 && status < 300,
    url: base + path + "/my?locale=en",
    headers: { get: () => challenge ? "challenge" : null },
    text: async () => pageObject,
  };
}

const deadline = () => Date.now() + 15_000;
const metadata = { path, handle, index: "A", language };
const source = Buffer.from("\ufeff// 源码\r\nint main() {}\n");
const submission = {
  ...metadata, source: source.toString("base64"),
  sha256: createHash("sha256").update(source).digest("hex"),
};

test("manual login reports only the verified handle and stops on a challenge", async (t) => {
  setPage(t, page({ account: handle, detectionScript: true }), base + "/enter");
  assert.deepEqual(await runPageJob("login", {}, deadline()), { status: "ok", handle });
  assert.deepEqual(await runPageJob("login", { handle: "other" }, deadline()), { status: "wrongAccount" });
  globalThis.document = page({ challenge: true, account: null });
  assert.deepEqual(await runPageJob("login", {}, deadline()), { status: "verification" });
});

test("Codeforces account link in the top-right area verifies login without a logout URL", async (t) => {
  setPage(t, page({ account: null, topRightProfile: handle }), base + "/settings/general");
  assert.deepEqual(await runPageJob("login", { handle }, deadline()), { status: "ok", handle });
  globalThis.document = page({ account: null, signInLink: true });
  assert.deepEqual(await runPageJob("login", { handle }, deadline()), { status: "authentication" });
  globalThis.document = page({ account: null });
  assert.deepEqual(await runPageJob("login", { handle }, deadline()), { status: "accountMarkup" });
});

test("preview performs no HTTP write and returns no form token", async (t) => {
  setPage(t, page({ form: makeForm() }));
  const request = t.mock.method(globalThis, "fetch", () => assert.fail("unexpected fetch"));
  assert.deepEqual(await runPageJob("preflight", metadata, deadline()), { status: "ok", language });
  globalThis.document = page({ form: makeForm({ otherOption: true }) });
  assert.deepEqual(await runPageJob("preflight", metadata, deadline()), { status: "language" });
  assert.equal(request.mock.callCount(), 0);
});

for (const enctype of ["application/x-www-form-urlencoded", "multipart/form-data"]) {
  test(`one POST forwards the exact source using ${enctype}`, async (t) => {
    setPage(t, page({ form: makeForm({ enctype }) }));
    const received = [];
    const responses = [
      decodedResponse(page({ rows: [21] })),
      decodedResponse(null, { type: "opaqueredirect", status: 0 }),
      decodedResponse(page({ rows: [22, 21], success: true })),
    ];
    t.mock.method(globalThis, "fetch", async (url, options) => {
      received.push([url, options]);
      return responses.shift();
    });
    assert.deepEqual(await runPageJob("submit", submission, deadline()), { status: "ok", submissionId: 22 });
    assert.equal(received.length, 3);
    const [action, post] = received[1];
    assert.equal(action, base + path + "/dynamic?token=fake-csrf");
    assert.equal(post.method, "POST");
    assert.equal(post.redirect, "manual");
    if (enctype === "application/x-www-form-urlencoded") {
      assert.deepEqual(Buffer.from(post.body.get("source")), source);
      assert.equal(post.body.get("programTypeId"), "95");
      assert.equal(post.body.get("dynamicField"), "new value");
    } else {
      const bytes = Buffer.from(await post.body.arrayBuffer());
      assert.ok(bytes.includes(source));
      assert.ok(bytes.includes('name="programTypeId"\r\n\r\n95'));
      assert.ok(bytes.includes('name="dynamicField"\r\n\r\nnew value'));
    }
  });
}

test("Cloudflare response before or after POST never leads to a retry", async (t) => {
  setPage(t, page({ form: makeForm() }));
  let posts = 0;
  const blocked = decodedResponse(page({ challenge: true }), { status: 403, challenge: true });
  const response = t.mock.method(globalThis, "fetch", async () => blocked);
  assert.deepEqual(await runPageJob("submit", submission, deadline()), { status: "verification" });
  assert.equal(response.mock.callCount(), 1);
  response.mock.mockImplementation(async (url, options) => {
    if (options.method === "POST") {
      posts++;
      return blocked;
    }
    return decodedResponse(page({ rows: [21] }));
  });
  assert.deepEqual(await runPageJob("submit", submission, deadline()), { status: "unknownVerification" });
  assert.equal(posts, 1);
});

test("changed digest and two new submissions are handled conservatively", async (t) => {
  setPage(t, page({ form: makeForm() }));
  const responses = [
    decodedResponse(page({ rows: [21] })),
    decodedResponse(null, { type: "opaqueredirect", status: 0 }),
    decodedResponse(page({ rows: [23, 22, 21], success: true })),
  ];
  const request = t.mock.method(globalThis, "fetch", async () => responses.shift());
  assert.deepEqual(await runPageJob("submit", { ...submission, sha256: "0" }, deadline()), { status: "changed" });
  assert.equal(request.mock.callCount(), 0);
  assert.deepEqual(await runPageJob("submit", submission, deadline()), { status: "unknown" });
  assert.equal(request.mock.callCount(), 3);
});

async function checkBrowserBridge(t, customPort) {
  const previousChrome = globalThis.chrome;
  t.after(() => { globalThis.chrome = previousChrome; });
  const event = (listener = () => {}) => ({ addListener: listener });
  let onMessage;
  let opened;
  let savedPort = customPort;
  const expectedPort = customPort ?? 12450;
  const loginUrl = base + "/enter?locale=en";
  const injections = [];
  globalThis.chrome = {
    runtime: {
      id: "extension",
      onInstalled: event(),
      onStartup: event(),
      onMessage: event((listener) => { onMessage = listener; }),
    },
    storage: { local: { get: async () => ({ pairingCode: "fake-pairing-code", bridgePort: savedPort }) } },
    alarms: { get: async () => ({}), onAlarm: event() },
    tabs: {
      async create(data) {
        opened = data;
        return { id: 1 };
      },
      async get(id) {
        return { id, url: loginUrl, status: "complete", incognito: false };
      },
      async query() {
        if (customPort) return [{ id: 3, url: loginUrl, status: "complete", incognito: false }];
        return [
          { id: 1, url: opened.url, status: "complete", incognito: false },
          { id: 2, url: base, status: "complete", incognito: false },
        ];
      },
    },
    scripting: {
      async executeScript(options) {
        injections.push(options);
        savedPort = 15300;
        return [{ result: options.target.tabId !== 1 ? { status: "ok", handle } : { status: "authentication" } }];
      },
    },
  };

  let ack;
  const received = new Promise((resolve) => { ack = resolve; });
  const calls = [];
  const payload = customPort ? { handle, useOpenTab: true } : { handle };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push([url, options]);
    if (url.endsWith("/job")) {
      return new Response(JSON.stringify({ id: "job1", kind: "login", payload, remainingMs: 8_000 }));
    }
    ack(JSON.parse(options.body));
    return new Response(null, { status: 204 });
  });

  await import(`../browser_extension/worker.js?port=${expectedPort}`);
  const popupAck = new Promise((reply) => onMessage({ type: "poll" }, { id: "extension" }, reply));
  assert.deepEqual(await popupAck, { state: "waiting" });
  assert.deepEqual(await received, { id: "job1", result: { status: "ok", handle } });
  assert.deepEqual(opened, customPort ? undefined : { url: loginUrl, active: true });
  assert.deepEqual(injections.map((options) => options.target.tabId), customPort ? [3] : [1, 2]);
  assert.deepEqual(injections.at(-1).args.slice(0, 2), ["login", payload]);
  assert.deepEqual(calls.map(([url, options]) => [url, options.method, options.credentials]), [
    [`http://127.0.0.1:${expectedPort}/job`, "GET", "omit"],
    [`http://127.0.0.1:${expectedPort}/result`, "POST", "omit"],
  ]);
}

test("extension opens the real site and can recognize a login completed in another tab",
  (t) => checkBrowserBridge(t));
test("extension reuses an opened login tab and returns the result to the same custom port",
  (t) => checkBrowserBridge(t, 12580));
