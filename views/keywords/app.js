const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

const TYPES = ["keyword", "number", "link", "qrcode", "emoji"];
const blankTypes = () => ({
  keyword: { enabled: true, reply: "" },
  number: { enabled: false, reply: "" },
  link: { enabled: false, reply: "" },
  qrcode: { enabled: false, reply: "" },
  emoji: { enabled: false, reply: "" },
});

let state = {
  enabled: true,
  notify: false,
  notify_at: false,
  keywords: [],
  types: blankTypes(),
  count: 0,
  last_error: "",
};

function typeOf(key) {
  return state.types?.[key] || { enabled: false, reply: "" };
}

function render() {
  $("enabled").checked = !!state.enabled;

  for (const key of TYPES) {
    const type = typeOf(key);
    const box = $(`t-${key}`);
    const reply = $(`r-${key}`);
    box.checked = !!type.enabled;
    box.disabled = !state.enabled;
    reply.value = type.reply || "";
    reply.disabled = !state.enabled || !type.enabled || !state.notify;
    reply.placeholder = type.enabled
      ? state.notify
        ? "命中后的回复，留空则不发"
        : "先打开下面的提示总开关"
      : "该类未开启";
  }

  $("notify").checked = !!state.notify;
  $("notify").disabled = !state.enabled;
  $("notifyAt").checked = !!state.notify_at;
  $("notifyAt").disabled = !state.enabled || !state.notify;

  $("kwCount").textContent = state.keywords.length;
  $("count").textContent = `已撤回 ${state.count || 0} 条`;
  $("err").textContent = state.last_error ? `最近一次失败：${state.last_error}` : "";

  const keywordOn = !!state.enabled && !!typeOf("keyword").enabled;
  $("input").disabled = !keywordOn;
  $("add").disabled = !keywordOn;

  const box = $("tags");
  box.textContent = "";

  if (!state.keywords.length) {
    const empty = document.createElement("span");
    empty.className = "empty";
    empty.textContent = "还没有关键词，先添加一个";
    box.appendChild(empty);
    return;
  }

  for (const kw of state.keywords) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.appendChild(document.createTextNode(kw));

    const close = document.createElement("b");
    close.textContent = "×";
    close.title = "删除";
    close.addEventListener("click", () => {
      state.keywords = state.keywords.filter((item) => item !== kw);
      persist();
    });
    tag.appendChild(close);
    box.appendChild(tag);
  }
}

function snapshot() {
  return {
    enabled: state.enabled,
    notify: state.notify,
    notify_at: state.notify_at,
    keywords: state.keywords,
    types: Object.fromEntries(
      TYPES.map((key) => [
        key,
        {
          enabled: !!typeOf(key).enabled,
          reply: typeOf(key).reply || "",
        },
      ])
    ),
  };
}

async function persist() {
  try {
    state = await bridge.apiPost("state/save", snapshot());
    $("err").textContent = "";
    $("saved").textContent = "已保存";
    setTimeout(() => {
      $("saved").textContent = "";
    }, 1500);
  } catch (error) {
    $("err").textContent = error.message || String(error);
  }
  render();
}

function addKeyword() {
  const input = $("input");
  const kw = input.value.trim();
  if (!kw) return;
  if (state.keywords.includes(kw)) {
    input.value = "";
    return;
  }
  if (state.keywords.length >= 200) {
    $("err").textContent = "最多 200 个关键词";
    return;
  }
  state.keywords.push(kw);
  input.value = "";
  persist();
}

async function init() {
  try {
    await bridge.ready();
    state = await bridge.apiGet("state");
    state.types = { ...blankTypes(), ...(state.types || {}) };
  } catch (error) {
    $("err").textContent = error.message || String(error);
  }
  render();

  $("add").addEventListener("click", addKeyword);
  $("input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addKeyword();
  });

  $("enabled").addEventListener("change", () => {
    state.enabled = $("enabled").checked;
    persist();
  });

  for (const key of TYPES) {
    $(`t-${key}`).addEventListener("change", () => {
      state.types[key] = {
        ...typeOf(key),
        enabled: $(`t-${key}`).checked,
      };
      persist();
    });
    $(`r-${key}`).addEventListener("change", () => {
      state.types[key] = {
        ...typeOf(key),
        reply: $(`r-${key}`).value.trim(),
      };
      persist();
    });
  }

  $("notify").addEventListener("change", () => {
    state.notify = $("notify").checked;
    persist();
  });
  $("notifyAt").addEventListener("change", () => {
    state.notify_at = $("notifyAt").checked;
    persist();
  });
}

init();
