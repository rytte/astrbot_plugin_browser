const bridge = window.AstrBotPluginView;
const status = document.getElementById("status");
const target = document.getElementById("target");
const refresh = document.getElementById("refresh");
const start = document.getElementById("start");
const url = document.getElementById("url");
const controls = document.getElementById("console");
const tabs = document.getElementById("tabs");
const screen = document.getElementById("screen");
const save = document.getElementById("save");
const cancel = document.getElementById("cancel");
const send = document.getElementById("send");
const text = document.getElementById("text");
const browserKeys = new Set([
  "Enter", "Tab", "Escape", "Backspace", "Delete", "Home", "End",
  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
]);
const connected = "已接管。请核对标签页的网站域名，再输入登录信息。";
let offer = null;
let token = "";
let frame = "";
let timer;
let starting = false;
let finishing = "";
let inputQueue = Promise.resolve();
let pendingInput = 0;

async function call(action, fields = {}) {
  const result = await bridge.apiPost("manual-login", { action, ...fields });
  if (result.error) {
    const error = new Error(result.message);
    error.code = result.error;
    throw error;
  }
  return result;
}

function clearControl(message) {
  clearTimeout(timer);
  token = "";
  frame = "";
  text.value = "";
  screen.removeAttribute("src");
  screen.hidden = true;
  save.disabled = true;
  tabs.replaceChildren();
  controls.hidden = true;
  refresh.hidden = false;
  status.textContent = message;
}

async function checkOffer() {
  if (starting || token) return;
  try {
    const result = await call("status");
    offer = result.offer;
    start.hidden = !offer || result.busy;
    status.textContent = result.busy
      ? "已有接管在进行。若原页面离线，等待接管超时后重新申请。"
      : offer
        ? "接管会关闭该聊天身份现有的浏览器标签页。"
        : "请先在目标聊天中以管理员身份发送 /browser_login。";
    target.textContent = offer ? `聊天来源 ${offer.origin} · 发送者 ${offer.sender}` : "";
  } catch (error) {
    status.textContent = error.message;
  }
}

async function poll(activeToken) {
  if (activeToken !== token || finishing) return;
  try {
    const result = await call("frame", { token: activeToken });
    if (activeToken !== token || finishing) return;
    if (result.jpeg) {
      frame = result.frame;
      screen.src = `data:image/jpeg;base64,${result.jpeg}`;
      screen.hidden = false;
      const items = result.tabs.map(({ id, site }) => new Option(site, id));
      if (items.length !== tabs.length ||
          items.some((item, index) => item.value !== tabs.options[index].value ||
                                     item.text !== tabs.options[index].text)) {
        tabs.replaceChildren(...items);
      }
      tabs.value = result.selected;
      save.disabled = !tabs.selectedOptions[0]?.text.startsWith("https://");
      status.textContent = connected;
    }
  } catch (error) {
    if (activeToken !== token || finishing) return;
    if (error.code === "LOGIN_SESSION" || error.code === "LOGIN_CLOSED") {
      clearControl(error.message);
      return;
    }
    frame = "";
    screen.removeAttribute("src");
    screen.hidden = true;
    save.disabled = true;
    status.textContent = error.message;
  }
  if (activeToken === token && !finishing) timer = setTimeout(() => poll(activeToken), 350);
}

function input(action, fields) {
  const activeToken = token;
  const activeFrame = frame;
  if (!activeToken || !activeFrame || finishing || pendingInput >= 12) return;
  pendingInput += 1;
  inputQueue = inputQueue.then(async () => {
    if (token !== activeToken || finishing === "cancel" || frame !== activeFrame) return;
    try {
      await call(action, { token: activeToken, frame: activeFrame, ...fields });
    } catch (error) {
      if (token === activeToken && !finishing) status.textContent = error.message;
    }
  }).finally(() => { pendingInput -= 1; });
}

async function finish(action) {
  if (!token || finishing) return;
  const activeToken = token;
  finishing = action;
  save.disabled = true;
  cancel.disabled = true;
  clearTimeout(timer);
  await inputQueue;
  try {
    await call(action, { token: activeToken });
    clearControl(action === "save"
      ? "登录态已保存。该聊天身份下次打开浏览器时会载入。"
      : "已取消，未保存本次登录。");
  } catch (error) {
    if (error.code === "LOGIN_SESSION") clearControl(error.message);
    else status.textContent = error.message;
  } finally {
    finishing = "";
    cancel.disabled = false;
    if (token === activeToken) timer = setTimeout(() => poll(activeToken), 350);
  }
}

start.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!offer || starting) return;
  starting = true;
  start.querySelector("button").disabled = true;
  status.textContent = "正在启动浏览器…";
  try {
    const result = await call("start", { offer: offer.id, url: url.value.trim() });
    token = result.token;
    url.value = "";
    start.hidden = true;
    refresh.hidden = true;
    controls.hidden = false;
    status.textContent = "正在加载网站…";
    await poll(token);
  } catch (error) {
    status.textContent = error.message;
  } finally {
    starting = false;
    start.querySelector("button").disabled = false;
  }
});

function position(event) {
  const rect = screen.getBoundingClientRect();
  return {
    x: Math.min(screen.naturalWidth - 1, Math.floor((event.clientX - rect.left) * screen.naturalWidth / rect.width)),
    y: Math.min(screen.naturalHeight - 1, Math.floor((event.clientY - rect.top) * screen.naturalHeight / rect.height)),
  };
}

screen.addEventListener("click", (event) => {
  screen.focus();
  input("click", position(event));
});
screen.addEventListener("wheel", (event) => {
  event.preventDefault();
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 360 : 1;
  const delta = (value) => Math.round(Math.max(-1200, Math.min(1200, scale * value)));
  input("scroll", { ...position(event), dx: delta(event.deltaX), dy: delta(event.deltaY) });
}, { passive: false });
screen.addEventListener("keydown", (event) => {
  if (event.isComposing || event.altKey) return;
  if (event.ctrlKey || event.metaKey) {
    if (event.key.toLowerCase() !== "a") return;
    event.preventDefault();
    input("key", { key: "ControlOrMeta+A" });
  } else if (browserKeys.has(event.key)) {
    event.preventDefault();
    input("key", { key: event.shiftKey && event.key === "Tab" ? "Shift+Tab" : event.key });
  } else if (event.key.length === 1) {
    event.preventDefault();
    input("text", { text: event.key });
  }
});
send.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!text.value || !frame || finishing) return;
  input("text", { text: text.value });
  text.value = "";
  screen.focus();
});
tabs.addEventListener("change", () => input("tab", { tab: tabs.value }));
refresh.addEventListener("click", checkOffer);
save.addEventListener("click", () => finish("save"));
cancel.addEventListener("click", () => finish("cancel"));

await bridge.ready();
refresh.hidden = false;
await checkOffer();
