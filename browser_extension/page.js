export async function runPageJob(kind, payload, deadline) {
  // Chrome injects this function by value. All dependencies live in this scope.
  const origin = "https://codeforces.com";
  const positiveId = /^[1-9][0-9]*$/;
  const sameAccount = (handle) => Boolean(handle && (!payload.handle || handle.toLowerCase() === payload.handle.toLowerCase()));
  const normalize = (text) => text.trim().replace(/\s+/g, " ");
  const fail = (status) => ({ status });
  let sent = false;

  function handleOf(page) {
    for (const script of page.querySelectorAll("script:not([src])")) {
      const match = script.textContent.match(/\bCodeforces(?:\.handle|\[["']handle["']\])\s*=\s*["']([A-Za-z0-9_.-]+)["']/);
      if (match) return match[1];
    }

    const accountArea = page.querySelector(".lang-chooser");
    if (accountArea) {
      const links = profileLinks(accountArea);
      const handles = links
        .filter(({ link, handle }) => normalize(link.textContent).toLowerCase() === handle.toLowerCase())
        .map(({ handle }) => handle);
      if (new Set(handles.map((handle) => handle.toLowerCase())).size === 1) return handles[0];
      if (!accountArea.querySelector("a[href^='/enter']") &&
          new Set(links.map(({ handle }) => handle.toLowerCase())).size === 1) return links[0].handle;
    }

    const header = page.querySelector("#header");
    if (!header) return null;
    const signsOut = [...header.querySelectorAll("a[href], form[action]")].some((element) => {
      const path = pathOf(element.getAttribute("href") || element.getAttribute("action"));
      return /^\/(logout|exit)$/.test(path) || /^(logout|log out|sign out|выйти)$/i.test(normalize(element.textContent));
    });
    if (!signsOut) return null;
    const handles = profileLinks(header).map(({ handle }) => handle);
    return new Set(handles.map((handle) => handle.toLowerCase())).size === 1 ? handles[0] : null;
  }

  function profileLinks(area) {
    return [...area.querySelectorAll("a[href]")].flatMap((link) => {
      const match = /^\/profile\/([A-Za-z0-9_.-]{1,64})$/.exec(pathOf(link.getAttribute("href")));
      return match ? [{ link, handle: match[1] }] : [];
    });
  }

  function pathOf(href, base = origin) {
    try {
      const url = new URL(href, base);
      return url.origin === origin ? url.pathname.replace(/\/$/, "") : "";
    } catch {
      return "";
    }
  }

  function verification(page) {
    return /^just a moment/i.test(page.title) || Boolean(page.querySelector(
      "#challenge-form, #cf-challenge-running, #challenge-stage, "
      + ".g-recaptcha, .h-captcha, .cf-turnstile, [autocomplete='one-time-code'], "
      + "form[action*='twoFactor' i], iframe[src*='captcha']",
    ));
  }

  function loginState(page) {
    let handle = handleOf(page);
    if (!handle && pathOf(location.href) === "/settings/general") {
      const fields = [...page.querySelectorAll("form input[name=handle]")];
      if (fields.length === 1 && /^[A-Za-z0-9_.-]{1,64}$/.test(fields[0].defaultValue)) {
        handle = fields[0].defaultValue;
      }
    }
    if (handle) return sameAccount(handle) ? { status: "ok", handle } : fail("wrongAccount");
    const signInPage = page.querySelector("form input[name=handleOrEmail], .lang-chooser a[href^='/enter']");
    return fail(signInPage ? "authentication" : "accountMarkup");
  }

  function problemFromLink(href) {
    const path = pathOf(href);
    const prefix = payload.path + "/problem/";
    const index = path.slice(prefix.length);
    return path.startsWith(prefix) && /^[A-Za-z][A-Za-z0-9]*$/.test(index) ? index.toUpperCase() : "";
  }

  function readProblems() {
    if (pathOf(location.href) !== payload.path) return fail("access");
    const table = document.querySelector("table.problems");
    if (!table) return fail("structure");
    const problems = Object.create(null);
    for (const row of table.rows) {
      const cells = [...row.cells].filter((cell) => cell.tagName === "TD");
      if (!cells.length) continue;
      const index = problemFromLink(cells[0].querySelector("a[href]")?.getAttribute("href"));
      const link = [...(cells[1]?.querySelectorAll("a[href]") || [])].find(
        (item) => problemFromLink(item.getAttribute("href")) === index,
      );
      const name = normalize(link?.textContent || "");
      if (!index || !name || Object.hasOwn(problems, index)) return fail("structure");
      problems[index] = name;
    }
    const name = normalize(document.title.replace(/^Dashboard\s+-\s+|\s+-\s+Codeforces$/g, ""));
    if (!name || !Object.keys(problems).length) return fail("structure");
    return { status: "ok", name, problems };
  }

  function targetLanguage(label) {
    return /^GNU G\+\+23(?:\s+[0-9][0-9.]*(?:\s+\([^()]+\))?)?$/i.test(label)
      || /^C\+\+23\s*\(GCC(?:\s+[^()]+)?\)$/i.test(label);
  }

  function singleField(form, name, selector) {
    const fields = [...form.elements].filter((field) => field.name === name && !field.matches(":disabled"));
    return fields.length === 1 && fields[0].matches(selector) ? fields[0] : null;
  }

  function validOption(option) {
    return !option.disabled && !option.closest("optgroup[disabled]");
  }

  function inspectForm() {
    if (pathOf(location.href) !== payload.path + "/submit") return fail("access");
    const forms = [...document.forms].filter((form) => form.elements.namedItem("programTypeId")
      || form.elements.namedItem("source"));
    if (forms.length !== 1) return fail("structure");
    const form = forms[0];
    if (form.querySelector("[name*='captcha' i], [name*='otp' i]")) return fail("verification");
    // A form input named "action" shadows the HTMLFormElement.action property.
    const action = new URL(form.getAttribute("action") || location.href, document.baseURI || location.href);
    const method = form.getAttribute("method")?.toLowerCase();
    const encoding = form.getAttribute("enctype")?.toLowerCase() || "application/x-www-form-urlencoded";
    if (method !== "post" || action.origin !== origin
        || !["application/x-www-form-urlencoded", "multipart/form-data"].includes(encoding)) {
      return fail("structure");
    }
    const csrf = singleField(form, "csrf_token", "input[type=hidden]");
    if (!csrf?.value.trim()) return fail("csrf");
    const languageField = singleField(form, "programTypeId", "select");
    const problemField = singleField(form, "submittedProblemIndex", "select");
    const sourceField = singleField(form, "source", "textarea");
    if (!languageField || !problemField || !sourceField || sourceField.readOnly) return fail("structure");
    const contest = form.elements.namedItem("contestId");
    if (contest && contest.value !== payload.path.split("/").at(-1)) return fail("structure");

    const languageOptions = new Map([...languageField.options]
      .filter((option) => validOption(option) && positiveId.test(option.value) && targetLanguage(normalize(option.textContent)))
      .map((option) => [option.value, normalize(option.textContent)]));
    if (languageOptions.size !== 1) return fail("language");
    const [languageId, language] = languageOptions.entries().next().value;
    const problemOptions = new Set([...problemField.options]
      .filter((option) => validOption(option) && option.value.toUpperCase() === payload.index)
      .map((option) => option.value));
    if (problemOptions.size !== 1) return fail("problem");

    const submitters = [...form.elements].filter((control) => control.type === "submit" && !control.matches(":disabled"));
    if (submitters.some((control) => ["formaction", "formmethod", "formenctype"].some((attribute) => control.hasAttribute(attribute)))
        || (submitters.length > 1 && submitters.some((control) => control.name))) return fail("structure");
    const submitter = submitters.length === 1 ? submitters[0] : undefined;
    const fields = new FormData(form, submitter);
    for (const field of form.elements) {
      if (field.required && !field.matches(":disabled")
          && ![languageField, problemField, sourceField].includes(field)
          && (!fields.get(field.name) || (field.type === "file" && !fields.get(field.name).size))) {
        return fail("structure");
      }
    }
    if ([...fields.values()].some((value) => typeof value !== "string" && (value.size || value.name))) {
      return fail("structure");
    }
    return {
      status: "ok", action: action.href, encoding, fields, language, languageId,
      problem: problemOptions.values().next().value,
    };
  }

  async function recent() {
    let response;
    try {
      response = await fetch(origin + payload.path + "/my?locale=en", {
        mode: "same-origin", credentials: "same-origin", cache: "no-store",
        signal: AbortSignal.timeout(12_000),
      });
    } catch {
      return fail("network");
    }
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    if (response.headers.get("cf-mitigated") === "challenge" || verification(page)) {
      return fail("verification");
    }
    if (!sameAccount(handleOf(page))) return fail("authentication");
    if (!response.ok || pathOf(response.url) !== payload.path + "/my") return fail("access");
    const table = page.querySelector("table.status-frame-datatable");
    const rows = [...page.querySelectorAll("tr[data-submission-id]")];
    if (!rows.length && (
      (table && table.querySelector("td.id-cell, .view-source"))
      || (!table && !/\b(no submissions|nothing to show)\b/i.test(page.body.textContent))
    )) return fail("structure");

    const ids = new Map();
    for (const row of rows) {
      const id = row.getAttribute("data-submission-id");
      if (!positiveId.test(id) || !Number.isSafeInteger(Number(id))) return fail("structure");
      const links = [...row.querySelectorAll("a[href]")].map((link) => link.getAttribute("href"));
      const users = links.map((href) => pathOf(href).toLowerCase()).filter((path) => path.startsWith("/profile/"));
      ids.set(Number(id), links.some((href) => problemFromLink(href) === payload.index)
        && [...row.cells].some((cell) => targetLanguage(normalize(cell.textContent)))
        && (!users.length || users.includes(`/profile/${payload.handle.toLowerCase()}`)));
    }
    return { status: "ok", ids, success: successful(page) };
  }

  function successful(page) {
    const message = /\b(?:your\s+)?(?:solution|code)\s+(?:has\s+been\s+|was\s+)?submitted successfully\b/i;
    return [...page.querySelectorAll(".success, .alert-success, .notice, [role=status], [role=alert]")]
      .some((element) => message.test(element.textContent))
      || [...page.querySelectorAll("script:not([src])")].some((script) => {
        const messages = script.textContent.matchAll(/\bCodeforces\.showMessage\(\s*(["'])(.*?)\1\s*\)/gs);
        return [...messages].some((entry) => message.test(entry[2]));
      });
  }

  function showRejection(page) {
    const errors = [...page.querySelectorAll(".error, .error__for-form, .error-message, .alert-danger")]
      .filter((item) => !item.hidden && item.style.display !== "none")
      .map((item) => normalize(item.textContent)).filter(Boolean);
    if (!errors.length) return false;
    const notice = document.createElement("p");
    notice.setAttribute("role", "alert");
    notice.textContent = [...new Set(errors)].join(" / ").slice(0, 500);
    document.querySelector("form textarea[name=source]")?.before(notice);
    return true;
  }

  function encodeBody(encoding, fields) {
    if (encoding === "application/x-www-form-urlencoded") {
      return new URLSearchParams([...fields].map(([name, value]) => [name, typeof value === "string" ? value : ""]));
    }
    const boundary = "cf-submit-" + crypto.randomUUID();
    const parts = [];
    for (const [name, value] of fields) {
      const quoted = name.replace(/[\r\n"]/g, (char) => "%" + char.charCodeAt(0).toString(16));
      const disposition = `--${boundary}\r\nContent-Disposition: form-data; name="${quoted}"`;
      if (typeof value === "string") {
        parts.push(disposition + "\r\n\r\n", value, "\r\n");
      } else {
        parts.push(disposition + '; filename=""\r\nContent-Type: application/octet-stream\r\n\r\n', value, "\r\n");
      }
    }
    parts.push(`--${boundary}--\r\n`);
    // Preserve LF and CRLF in source exactly, rather than normalizing a FormData text part.
    return new Blob(parts, { type: `multipart/form-data; boundary=${boundary}` });
  }

  async function submit() {
    const initial = inspectForm();
    if (initial.status !== "ok") return initial;
    if (initial.language !== payload.language) return fail("changed");
    const bytes = Uint8Array.from(atob(payload.source), (char) => char.charCodeAt(0));
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const hex = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    if (hex !== payload.sha256) return fail("changed");
    const source = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    const before = await recent();
    if (before.status !== "ok") return before;
    const current = inspectForm();
    if (current.status !== "ok") return current;
    if (Date.now() >= deadline - 1_000 || current.language !== initial.language
        || current.languageId !== initial.languageId || !sameAccount(handleOf(document))) return fail("changed");
    current.fields.set("submittedProblemIndex", current.problem);
    current.fields.set("programTypeId", current.languageId);
    current.fields.set("source", source);
    const body = encodeBody(current.encoding, current.fields);
    sent = true;
    const response = await fetch(current.action, {
      method: "POST", body, mode: "same-origin", credentials: "same-origin",
      redirect: "manual", cache: "no-store", signal: AbortSignal.timeout(18_000),
    });
    let confirmed = false;
    if (response.type !== "opaqueredirect") {
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      if (response.headers.get("cf-mitigated") === "challenge" || verification(page)) {
        return fail("unknownVerification");
      }
      if (response.status < 500 && showRejection(page)) return fail("rejected");
      if (!response.ok) return fail("unknown");
      confirmed = successful(page);
      if (!confirmed) return fail("unknown");
    }
    const after = await recent();
    if (after.status === "verification") return fail("unknownVerification");
    if (after.status !== "ok" || !(confirmed || after.success)) return fail("unknown");
    const highest = Math.max(0, ...before.ids.keys());
    const additions = [...after.ids].filter(([id]) => id > highest);
    return additions.length === 1 && additions[0][1]
      ? { status: "ok", submissionId: additions[0][0] } : fail("unknown");
  }

  try {
    if (location.origin !== origin) return fail("access");
    if (verification(document)) return fail("verification");
    if (kind === "login") return loginState(document);
    if (!sameAccount(handleOf(document))) return fail("authentication");
    if (kind === "problems") return readProblems();
    if (kind === "submit") return await submit();
    if (kind === "preflight") {
      const fields = inspectForm();
      return fields.status === "ok" ? { status: "ok", language: fields.language } : fields;
    }
    return fail("structure");
  } catch {
    return fail(sent ? "unknown" : "structure");
  }
}
