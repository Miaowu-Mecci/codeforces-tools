const form = document.querySelector("#pairing");
const controls = document.querySelector("#controls");
const codeField = document.querySelector("#pairing-code");
const input = document.querySelector("#code");
const port = document.querySelector("#port");
const save = document.querySelector("#save");
const status = document.querySelector("#status");
const messages = {
  idle: "已配对。运行终端命令时请保持浏览器开启。",
  waiting: "正在处理服务器命令。若打开了 Codeforces 标签页，请按网页提示操作。",
  login_verification: "请完成 Codeforces 标签页上的验证。",
  login_needed: "网页仍显示登录入口。请在同一浏览器中登录 Codeforces。",
  login_markup: "已读取 Codeforces 页面，但网页顶栏的账号格式未识别。",
  login_loading: "正在等 Codeforces 标签页加载。",
  login_permission: "扩展无法读取网页。请在扩展详情的「网站访问权限」允许访问 codeforces.com。",
  login_other_site: "当前标签页不在 codeforces.com。请打开 https://codeforces.com/。",
  login_inspection_failed: "浏览器没有返回网页检查结果。请刷新 Codeforces 页面并重新检查。",
  login_wrong_account: "当前浏览器账号与终端指定的账号不一致。",
  confirming_login: "已识别账号，正在将确认返回服务器。",
  login_confirmed: "已确认登录。请查看服务器终端。",
  login_expired: "本次登录没有确认成功。请在服务器重新运行 cflogin --browser。",
  confirmation_lost: "未能把登录确认交给服务器。请检查本机连接端口并重试 cflogin --browser。",
  no_tunnel: "尚未接到命令。请核对本机端口，并确认终端命令已运行。",
  unauthorized: "配对码无效。请重新配对。",
};

let { pairingCode, bridgePort = 12450 } = await chrome.storage.local.get(["pairingCode", "bridgePort"]);
port.value = bridgePort;
render();
if (pairingCode) void poll();

function render(state = "idle") {
  const paired = Boolean(pairingCode);
  codeField.hidden = paired;
  input.disabled = paired;
  save.textContent = paired ? "保存端口" : "连接";
  controls.hidden = !paired;
  status.textContent = paired
    ? messages[state] || messages.idle
    : "运行 cflogin --browser，用终端显示的配对码连接。";
}

async function poll() {
  if (!pairingCode) return;
  try {
    const state = await chrome.runtime.sendMessage({ type: "poll" });
    render(state?.state);
  } catch {
    render("no_tunnel");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = pairingCode || input.value;
  const value = port.valueAsNumber;
  if (!Number.isInteger(value) || value < 1 || value > 65535) return;
  await chrome.storage.local.set({ pairingCode: code, bridgePort: value });
  pairingCode = code;
  input.value = "";
  render();
  await poll();
});

document.querySelector("#check").addEventListener("click", poll);
document.querySelector("#disconnect").addEventListener("click", async () => {
  await chrome.storage.local.remove("pairingCode");
  pairingCode = undefined;
  render();
});
