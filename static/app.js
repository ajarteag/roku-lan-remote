const $ = (sel) => document.querySelector(sel);

let currentAppId = null;
let toastTimer = null;

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2500);
}

async function api(path, options) {
  const resp = await fetch(path, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  return data;
}

const post = (path) => api(path, { method: "POST" }).catch((e) => toast(e.message));

function fmtTime(ms) {
  if (ms == null) return null;
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

async function refreshStatus() {
  try {
    const st = await api("/api/status");
    const dot = $("#power-dot");
    if (!st.reachable) {
      dot.className = "dot off";
      $("#tv-name").textContent = `TV unreachable (${st.tv_ip})`;
      $("#now-app").textContent = "—";
      $("#now-state").textContent = "check that the TV has power";
      return;
    }
    const power = st.device.power || "";
    dot.className = "dot " + (power === "PowerOn" ? "on" : "standby");
    $("#tv-name").textContent = st.device.name || st.device.model || "Roku TV";

    const appName = st.app.screensaver ? `${st.app.name} (screensaver)` : st.app.name;
    $("#now-app").textContent = power === "PowerOn" ? (appName || "Home") : "Standby";

    const icon = $("#now-icon");
    if (st.app.id && power === "PowerOn") {
      icon.src = `/api/icon/${st.app.id}`;
      icon.hidden = false;
    } else {
      icon.hidden = true;
    }

    let stateLine = power === "PowerOn" ? (st.player.state || "idle") : "display off";
    const pos = fmtTime(st.player.position_ms);
    const dur = fmtTime(st.player.duration_ms);
    if (pos && st.player.state && st.player.state !== "none") {
      stateLine += dur ? ` · ${pos} / ${dur}` : ` · ${pos}`;
    }
    $("#now-state").textContent = stateLine;

    if (st.app.id !== currentAppId) {
      currentAppId = st.app.id;
      document.querySelectorAll(".app").forEach((el) =>
        el.classList.toggle("active-app", el.dataset.id === currentAppId));
    }
  } catch {
    $("#power-dot").className = "dot off";
    $("#now-state").textContent = "server unreachable";
  }
}

async function loadApps() {
  try {
    const apps = await api("/api/apps");
    const inputs = apps.filter((a) => a.type === "tvin");
    const channels = apps.filter((a) => a.type !== "tvin");

    $("#inputs").innerHTML = "";
    for (const input of inputs) {
      const btn = document.createElement("button");
      btn.className = "chip";
      btn.textContent = input.name;
      btn.onclick = () => post(`/api/launch/${input.id}`);
      $("#inputs").append(btn);
    }

    $("#apps").innerHTML = "";
    for (const app of channels) {
      const btn = document.createElement("button");
      btn.className = "app";
      btn.dataset.id = app.id;
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = `/api/icon/${app.id}`;
      img.alt = "";
      const label = document.createElement("span");
      label.textContent = app.name;
      btn.append(img, label);
      btn.onclick = () => post(`/api/launch/${app.id}`);
      $("#apps").append(btn);
    }
  } catch (e) {
    toast("Couldn't load apps: " + e.message);
  }
}

async function loadMacros() {
  try {
    const macros = await api("/api/macros");
    $("#macro-json").value = JSON.stringify(macros, null, 2);
    $("#macros").innerHTML = "";
    for (const macro of macros) {
      const btn = document.createElement("button");
      btn.className = "chip";
      btn.textContent = `${macro.icon || "⚡"} ${macro.name}`;
      btn.onclick = async () => {
        btn.classList.add("running");
        await post(`/api/macro/${encodeURIComponent(macro.name)}`);
        btn.classList.remove("running");
      };
      $("#macros").append(btn);
    }
  } catch (e) {
    toast("Couldn't load automations: " + e.message);
  }
}

$("#macro-save").onclick = async (ev) => {
  ev.preventDefault();
  try {
    const macros = JSON.parse($("#macro-json").value);
    await api("/api/macros", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(macros),
    });
    toast("Automations saved");
    loadMacros();
  } catch (e) {
    toast("Invalid JSON: " + e.message);
  }
};

document.querySelectorAll("[data-key]").forEach((btn) => {
  btn.addEventListener("click", () => post(`/api/keypress/${btn.dataset.key}`));
});

$("#text-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("#text-input");
  if (!input.value) return;
  await api("/api/text", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: input.value }),
  }).catch((e) => toast(e.message));
  input.value = "";
});

const KEYMAP = {
  ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
  Enter: "Select", Backspace: "Back", Escape: "Home", " ": "Play",
};

document.addEventListener("keydown", (ev) => {
  const tag = document.activeElement.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  const key = KEYMAP[ev.key];
  if (key) {
    ev.preventDefault();
    post(`/api/keypress/${key}`);
  }
});

loadApps();
loadMacros();
refreshStatus();
setInterval(refreshStatus, 2500);
