/* 奴隶市场 · 管理账房 前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = { meta: null, groups: [], gid: "", rankKind: "currency", page: 1, size: 20, kw: "" };
/* 请求序号：切群/切面板时丢弃迟到的旧响应，避免旧数据覆盖新面板 */
const seq = { players: 0, rank: 0, market: 0, texts: 0 };

/* ---------- 基础 ---------- */
async function api(path, opts = {}) {
  const write = opts.method && opts.method !== "GET";
  const res = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      // 自定义头：跨站脚本无法附加（会触发预检并被拒），是无密码模式下的 CSRF 防线
      ...(write ? { "X-Slvm-Req": "1" } : {}),
    },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 423) {
    // 服务器强制要求改密：弹重置层
    showReset(true);
    throw new Error(data.error || "首次登录必须重置密码");
  }
  if (res.status === 401 && !opts.noAuthRedirect) {
    showLogin(true);
    throw new Error(data.error || "未登录");
  }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("#toastBox").appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

const fmtNum = (n) => Number(n || 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const emptyHint = (msg, sub = "") => `<div class="empty-state">
  <svg viewBox="0 0 24 24" fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M3 21h18"/><path d="M5 21V8l7-5 7 5v13"/><path d="M9 21v-6h6v6"/>
  </svg>
  <div class="em-main">${esc(msg)}</div>
  ${sub ? `<div class="em-sub">${esc(sub)}</div>` : ""}
</div>`;
const NO_GROUP_HINT = "还没有群参与游戏";
const NO_GROUP_SUB = "去群里发送「！奴隶帮助」开始吧";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- 登录 / 强制改密 ---------- */
function showLogin(show) { $("#loginOverlay").classList.toggle("show", show); }
function showReset(show) { $("#resetOverlay").classList.toggle("show", show); }

async function doLogin() {
  const pwd = $("#loginPwd").value;
  $("#loginBtn").disabled = true;
  try {
    // noAuthRedirect：登录接口自己的 401 是"密码错误"，不能被当成会话过期
    const r = await api("/api/auth/login", {
      method: "POST",
      body: { password: pwd },
      noAuthRedirect: true,
    });
    $("#loginErr").textContent = "";
    $("#loginPwd").value = "";
    if (r.must_reset) {
      // 临时密码首次登录：跳出强制改密弹层，原 password 字段清空
      showReset(true);
      $("#resetNew").focus();
      $("#resetErr").textContent = "";
    } else {
      showLogin(false);
      await boot();
    }
  } catch (e) { $("#loginErr").textContent = e.message; }
  $("#loginBtn").disabled = false;
}
$("#loginBtn").onclick = doLogin;
$("#loginPwd").addEventListener("keydown", (e) => e.key === "Enter" && doLogin());

$("#resetBtn").onclick = async () => {
  const newPwd = $("#resetNew").value;
  const confirm = $("#resetConfirm").value;
  $("#resetErr").textContent = "";
  if (!newPwd || newPwd.length < 6) {
    $("#resetErr").textContent = "新密码至少 6 个字符";
    return;
  }
  if (newPwd !== confirm) {
    $("#resetErr").textContent = "两次输入不一致";
    return;
  }
  $("#resetBtn").disabled = true;
  try {
    await api("/api/auth/change-password", {
      method: "POST",
      body: { new_password: newPwd },
    });
    showReset(false);
    showLogin(false);
    await boot();
  } catch (e) { $("#resetErr").textContent = e.message; }
  $("#resetBtn").disabled = false;
};

$("#logoutChip").onclick = async () => {
  await api("/api/auth/logout", { method: "POST" });
  location.reload();
};

/* ---------- 导航 ---------- */
const PAGE_TITLES = { overview: "总览", rank: "排行榜", market: "市场一览", players: "玩家管理", backup: "数据备份", texts: "文案编辑", config: "插件配置" };
const PAGE_NEEDS_GID = new Set(["rank", "market", "players"]);

function moveInk(btn) {
  const ink = $("#nav .nav-ink");
  if (!ink || !btn) return;
  if (window.innerWidth <= 860) {
    ink.style.top = "";
    ink.style.height = "4px";
    ink.style.left = btn.offsetLeft + "px";
    ink.style.width = btn.offsetWidth + "px";
  } else {
    ink.style.left = "0";
    ink.style.width = "100%";
    ink.style.top = btn.offsetTop + "px";
    ink.style.height = btn.offsetHeight + "px";
  }
}
window.addEventListener("resize", () => moveInk($("#nav .nav-btn.on")));

$("#nav").addEventListener("click", (e) => {
  const btn = e.target.closest(".nav-btn");
  if (!btn) return;
  $$(".nav-btn").forEach((b) => b.classList.toggle("on", b === btn));
  moveInk(btn);
  $$(".panel").forEach((p) => p.classList.remove("on"));
  const p = btn.dataset.p;
  $("#p-" + p)?.classList.add("on");
  $("#pageTitle").textContent = PAGE_TITLES[p] || p;
  $("#gidPickerWrap").hidden = !PAGE_NEEDS_GID.has(p);
  state.page = 1;          // 换面板重置分页，否则会带着旧页码请求越界页
  state.kw = "";
  if ($("#searchKw")) $("#searchKw").value = "";
  if (p === "overview") loadOverview();
  if (p === "rank") loadRanking();
  if (p === "market") loadMarket();
  if (p === "players") loadPlayers();
  if (p === "backup") loadBackups();
  if (p === "texts") loadTexts();
  if (p === "config") loadConfig();
});

/* ---------- 群选择 ---------- */
function renderGroupPicker() {
  const sel = $("#gidPicker");
  sel.innerHTML = state.groups
    .map((g) => `<option value="${esc(g.gid)}">${esc(g.gid)}（${g.count}人）</option>`)
    .join("");
  if (!state.groups.some((g) => g.gid === state.gid)) state.gid = state.groups[0]?.gid || "";
  if (state.gid) sel.value = state.gid;
}
$("#gidPicker").addEventListener("change", () => {
  state.gid = $("#gidPicker").value;
  state.page = 1;          // 换群同样要回到第 1 页
  state.kw = "";
  if ($("#searchKw")) $("#searchKw").value = "";
  const active = $("#nav .nav-btn.on")?.dataset.p;
  if (active === "rank") loadRanking();
  if (active === "market") loadMarket();
  if (active === "players") loadPlayers();
});

/* ---------- 总览 ---------- */
async function loadOverview() {
  try {
    const { stats } = await api("/api/overview");
    const cards = [
      ["玩家总数", fmtNum(stats.players)],
      ["参与群数", fmtNum(stats.groups)],
      ["流通金币", fmtNum(stats.currency)],
      ["银行存款", fmtNum(stats.bank)],
      ["在册奴隶", fmtNum(stats.slaves)],
    ];
    $("#statCards").innerHTML = cards
      .map(([k, v]) => `<div class="stat-card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`)
      .join("");
  } catch (e) { toast(e.message, true); }
}

/* ---------- 排行榜 ---------- */
$("#rankKind").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#rankKind button").forEach((x) => x.classList.toggle("on", x === b));
  state.rankKind = b.dataset.k;
  loadRanking();
});

async function loadRanking() {
  if (!state.gid) { $("#rankList").innerHTML = emptyHint(NO_GROUP_HINT, NO_GROUP_SUB); return; }
  const my = ++seq.rank;
  try {
    const { rows } = await api(`/api/ranking?gid=${encodeURIComponent(state.gid)}&kind=${state.rankKind}`);
    if (my !== seq.rank) return;   // 已经切走了，丢弃这次迟到的响应
    $("#rankList").innerHTML = rows.length
      ? rows.map((r) => `
        <div class="rank-row">
          <div class="rank-no">${r.rank <= 3 ? ["🥇", "🥈", "🥉"][r.rank - 1] : esc(r.rank)}</div>
          <div><div class="name">${esc(r.name)}</div><div class="uid">${esc(r.id)}</div></div>
          <div class="score">${esc(r.score)}</div>
        </div>`).join("")
      : `<p class="empty">暂无数据</p>`;
  } catch (e) { toast(e.message, true); }
}

/* ---------- 市场 ---------- */
async function loadMarket() {
  if (!state.gid) { $("#marketGrid").innerHTML = emptyHint(NO_GROUP_HINT, NO_GROUP_SUB); return; }
  const my = ++seq.market;
  try {
    const { items } = await api(`/api/market?gid=${encodeURIComponent(state.gid)}`);
    if (my !== seq.market) return;
    $("#marketGrid").innerHTML = items.length
      ? items.map((it) => `
        <div class="mk-card">
          <div class="nm">${esc(it.name)}</div><div class="id">${esc(it.id)}</div>
          <div class="vl">身价 ${esc(it.value)}</div>
          <div class="ms">主人：${esc(it.master)}</div>
        </div>`).join("")
      : `<p class="empty">市场上还没有人</p>`;
  } catch (e) { toast(e.message, true); }
}

/* ---------- 玩家 ---------- */
/* 玩家表格行：分页与搜索共用同一模板，避免两份复制粘贴各自漂移 */
const playerRow = (p) => `
  <tr>
    <td><b>${esc(p.nickname)}</b><br><span class="uid">${esc(p.uid)}</span></td>
    <td>${fmtNum(p.currency)}</td><td>${fmtNum(p.value)}</td><td>${esc(p.slave_count)}</td>
    <td>${esc(p.master || "无")}</td>
    <td>Lv.${esc(p.bank_level)} · ${fmtNum(p.bank_balance)}</td>
    <td><span class="badge ok">${esc(p.tier)} ${esc(p.rank_score)}</span></td>
    <td><div class="op"><button class="btn sm" data-edit="${esc(p.uid)}">编辑</button></div></td>
  </tr>`;

async function loadPlayers() {
  if (!state.gid) {
    $("#playerTbl tbody").innerHTML = `<tr><td colspan="8">${emptyHint(NO_GROUP_HINT)}</td></tr>`;
    $("#playerPager").innerHTML = "";
    return;
  }
  const my = ++seq.players;
  try {
    const r = await api(`/api/players?gid=${encodeURIComponent(state.gid)}&page=${state.page}&size=${state.size}`);
    if (my !== seq.players) return;
    const size = r.size || state.size;   // 分页大小由后端给出，不再前端硬编码 20
    const pages = Math.max(1, Math.ceil(r.total / size));
    if (state.page > pages) {             // 换群后页码越界：钳回最后一页重取
      state.page = pages;
      return loadPlayers();
    }
    $("#playerTbl tbody").innerHTML = r.players.length
      ? r.players.map(playerRow).join("")
      : `<tr><td colspan="8"><p class="empty">该群暂无玩家数据</p></td></tr>`;
    $("#playerPager").innerHTML = pages > 1
      ? Array.from({ length: pages }, (_, i) =>
          `<button class="btn sm ${i + 1 === state.page ? "primary" : ""}" data-page="${i + 1}">${i + 1}</button>`).join("")
      : "";
  } catch (e) { toast(e.message, true); }
}
$("#playerPager").addEventListener("click", (e) => {
  const b = e.target.closest("[data-page]");
  if (b) { state.page = Number(b.dataset.page); loadPlayers(); }
});
$("#playerTbl").addEventListener("click", (e) => {
  const b = e.target.closest("[data-edit]");
  if (b) openEdit(b.dataset.edit);
});

async function openEdit(uid) {
  try {
    const { profile } = await api(`/api/admin/player?gid=${encodeURIComponent(state.gid)}&uid=${encodeURIComponent(uid)}`);
    $("#editTitle").textContent = `编辑玩家 · ${profile.nickname}`;
    const fields = [
      ["currency", "金币", profile.currency],
      ["value", "身价", profile.value],
      ["master", "主人 ID（留空为无）", profile.master],
      ["bank_level", "银行等级", profile.bank_level],
      ["bank_balance", "银行存款", profile.bank_balance],
    ];
    $("#editForm").innerHTML = fields
      .map(([k, label, v]) => `<div class="f"><label>${label}</label><input data-k="${k}" value="${esc(v)}"></div>`)
      .join("");
    $("#editIco").className = "modal-ico edit";
    $("#editIco").innerHTML = ICO_EDIT;
    $("#editDelete").style.display = "";
    $("#editSave").textContent = "保存";
    openModal("#editMask");
    $("#editSave").onclick = async () => {
      const values = {};
      $$("#editForm input").forEach((i) => (values[i.dataset.k] = i.value));
      try {
        const r = await api("/api/admin/player/save", { method: "POST", body: { gid: state.gid, uid, ...values } });
        toast("已保存");
        (r.rejected || []).forEach((k) => toast(`字段 ${k} 未生效`, true));
        closeModal("#editMask");
        loadPlayers();
      } catch (e2) { toast(e2.message, true); }
    };
    $("#editDelete").onclick = () => askConfirm("删除存档？", `将删除 ${profile.nickname}（${uid}）的存档（删除前自动留档到回收站）。`, async () => {
      try {
        await api("/api/admin/player/delete", { method: "POST", body: { gid: state.gid, uid } });
        toast("已删除");
        closeModal("#editMask");
        loadPlayers();
      } catch (e2) { toast(e2.message, true); }
    });
  } catch (e) { toast(e.message, true); }
}
$("#editCancel").onclick = () => closeModal("#editMask");

/* ---------- 搜索 ---------- */
$("#btnSearch").onclick = async () => {
  const kw = $("#searchKw").value.trim();
  state.kw = kw;
  if (!state.gid || !kw) { state.page = 1; return loadPlayers(); }
  const my = ++seq.players;
  try {
    const { results } = await api(`/api/search?gid=${encodeURIComponent(state.gid)}&kw=${encodeURIComponent(kw)}`);
    if (my !== seq.players) return;
    $("#playerTbl tbody").innerHTML = results.length
      ? results.map(playerRow).join("")
      : `<tr><td colspan="8"><p class="empty">未找到匹配玩家</p></td></tr>`;
    $("#playerPager").innerHTML = "";
  } catch (e) { toast(e.message, true); }
};
$("#searchKw").addEventListener("keydown", (e) => e.key === "Enter" && $("#btnSearch").click());

/* ---------- 备份 ---------- */
async function loadBackups() {
  try {
    const { backups } = await api("/api/backups");
    $("#bkList").innerHTML = backups.length
      ? backups.map((b) => `
        <div class="bk-row">
          <span class="badge">#${b.index}</span><span class="nm">${esc(b.name)}</span>
          <span class="op">
            <button class="btn sm" data-restore="${b.index}">恢复</button>
            <button class="btn sm danger" data-delete="${b.index}">删除</button>
          </span>
        </div>`).join("")
      : `<p class="empty">当前没有任何备份</p>`;
  } catch (e) { toast(e.message, true); }
}
$("#bkList").addEventListener("click", (e) => {
  const r = e.target.closest("[data-restore]");
  const d = e.target.closest("[data-delete]");
  if (r) askConfirm("恢复备份？", `将把数据回滚到备份 #${r.dataset.restore}。`, async () => {
    try { await api("/api/backups/restore", { method: "POST", body: { index: Number(r.dataset.restore) } }); toast("恢复成功"); loadBackups(); }
    catch (e2) { toast(e2.message, true); }
  });
  if (d) askConfirm("删除备份？", `将删除备份 #${d.dataset.delete}。`, async () => {
    try { await api("/api/backups/delete", { method: "POST", body: { index: Number(d.dataset.delete) } }); toast("已删除"); loadBackups(); }
    catch (e2) { toast(e2.message, true); }
  });
});
$("#btnCreateBackup").onclick = async () => {
  try { await api("/api/backups/create", { method: "POST" }); toast("备份创建成功"); loadBackups(); }
  catch (e) { toast(e.message, true); }
};

/* ---------- 文案编辑 ---------- */
let textsFull = {}; // 键名 -> 从服务端读到的完整数组（折叠部分保存时据此还原）
const FOLD_AT = 30;
const txtItems = (arr) =>
  arr.map((s2) => `<div class="txt-item"><input value="${esc(s2)}"><button class="btn sm del">✕</button></div>`).join("");

async function loadTexts() {
  const my = ++seq.texts;
  try {
    const name = $("#textsName").value;
    const { data } = await api(`/api/admin/texts?name=${encodeURIComponent(name)}`);
    if (my !== seq.texts) return;
    textsFull = data;
    if (name === "help") renderHelpEditor(data);
    else renderListEditor(data);
  } catch (e) { toast(e.message, true); }
}

function renderListEditor(data) {
  $("#textsBody").innerHTML = Object.entries(data).map(([k, v]) => {
    const isList = Array.isArray(v) && v.every((x) => typeof x === "string");
    if (isList) {
      // 只渲染前 FOLD_AT 条，但把折叠条数记在 data-shown 上：
      // 保存时从 textsFull 还原尾部，否则改一条就会把后面上百条一起截掉
      const fold = v.length > FOLD_AT;
      const shown = fold ? v.slice(0, FOLD_AT) : v;
      return `
      <div class="txt-key" data-key="${esc(k)}" data-kind="list"${fold ? ` data-shown="${FOLD_AT}"` : ""}>
        <label>${esc(k)}（${v.length} 条）</label>
        <div class="txt-list">${txtItems(shown)}</div>
        ${fold ? `<p class="he-tip">另有 ${v.length - FOLD_AT} 条已折叠，保存时会原样保留</p>
        <button class="btn sm" data-expand="${esc(k)}">展开全部 ${v.length} 条</button>` : ""}
        <button class="btn sm" data-add>＋ 添加一条</button>
      </div>`;
    }
    return `
      <div class="txt-key" data-key="${esc(k)}" data-kind="json">
        <label>${esc(k)}</label>
        <textarea class="txt-json" rows="10">${esc(JSON.stringify(v, null, 2))}</textarea>
      </div>`;
  }).join("");
}

/* ===== 帮助文案：可视化编辑（不再手写 JSON） ===== */
const helpSecHTML = (sec = {}) => `
  <div class="he-sec">
    <div class="he-sec-hd">
      <input class="he-ico" maxlength="4" placeholder="🛒" value="${esc(sec.icon || "")}">
      <input class="he-title" placeholder="分栏标题" value="${esc(sec.title || "")}">
      <label class="he-wide"><input type="checkbox"${sec.wide ? " checked" : ""}>通栏</label>
      <span class="he-ops">
        <button type="button" class="btn sm" data-mv="-1" title="上移">↑</button>
        <button type="button" class="btn sm" data-mv="1" title="下移">↓</button>
        <button type="button" class="btn sm danger" data-del-sec title="删除分栏">✕</button>
      </span>
    </div>
    <div class="he-items">${txtItems((sec.items || []).map(String))}</div>
    <button type="button" class="btn sm" data-add-item>＋ 添加条目</button>
  </div>`;

function renderHelpEditor(data) {
  const secs = Array.isArray(data.sections) ? data.sections : [];
  $("#textsBody").innerHTML = `
    <div class="help-ed">
      <div class="he-row"><label>标题（图片大标题，必填）</label>
        <input id="heTitle" value="${esc(data.title || "")}"></div>
      <div class="he-row"><label>副标题（标题下方的小字）</label>
        <input id="heSub" value="${esc(data.sub || "")}"></div>
      <div class="he-row"><label>分栏（每张卡片一个分栏，勾「通栏」占满整行）</label>
        <div class="he-secs" id="heSecs">${secs.map(helpSecHTML).join("")}</div></div>
      <button type="button" class="btn sm" id="heAddSec">＋ 添加分栏</button>
      <details class="he-plain">
        <summary>纯文本版帮助（渲染图片失败时发送的文字，一般不用改）</summary>
        <p class="he-tip">留空保存时会按上面的分栏自动生成。</p>
        <textarea id="heText">${esc(data.text || "")}</textarea>
        <button type="button" class="btn sm" id="heGenText">按分栏重新生成</button>
      </details>
    </div>`;
}

function collectHelpSections() {
  return $$("#heSecs .he-sec").map((el) => {
    const sec = {
      icon: $(".he-ico", el).value.trim(),
      title: $(".he-title", el).value.trim(),
      items: $$(".he-items input", el).map((i) => i.value.trim()).filter(Boolean),
    };
    if ($(".he-wide input", el).checked) sec.wide = true;
    return sec;
  }).filter((s) => s.title && s.items.length);
}

const helpPlainText = (title, secs) =>
  [title || "奴隶市场帮助", ...secs.map((s) => `${s.title}：${s.items.join("｜")}`)].join("\n");

function collectHelp() {
  const title = $("#heTitle").value.trim();
  if (!title) { toast("标题不能为空", true); return null; }
  const sections = collectHelpSections();
  if (!sections.length) { toast("至少保留一个带条目的分栏", true); return null; }
  return {
    title,
    sub: $("#heSub").value.trim(),
    text: $("#heText").value.trim() || helpPlainText(title, sections),
    sections,
  };
}

$("#btnLoadTexts").onclick = loadTexts;
$("#textsName").addEventListener("change", loadTexts);
$("#textsBody").addEventListener("click", (e) => {
  const t = e.target;
  if (t.matches("[data-expand]")) {
    // 从完整数据源还原全部条目，并清掉 data-shown（此后不再需要拼接尾部）
    const key = t.closest(".txt-key");
    $(".txt-list", key).innerHTML = txtItems(textsFull[t.dataset.expand] || []);
    delete key.dataset.shown;
    $(".he-tip", key)?.remove();
    t.remove();
    return;
  }
  if (t.matches("[data-add]")) {
    const key = t.closest(".txt-key");
    $(".txt-list", key).insertAdjacentHTML("beforeend", txtItems([""]));
    $(".txt-list .txt-item:last-child input", key).focus();
    return;
  }
  if (t.matches("[data-add-item]")) {
    const box = $(".he-items", t.closest(".he-sec"));
    box.insertAdjacentHTML("beforeend", txtItems([""]));
    $(".txt-item:last-child input", box).focus();
    return;
  }
  if (t.matches("[data-del-sec]")) {
    const sec = t.closest(".he-sec");
    askConfirm("删除分栏？", `将移除「${$(".he-title", sec).value || "未命名"}」分栏及其条目（保存后生效）。`,
      () => sec.remove());
    return;
  }
  if (t.matches("[data-mv]")) {
    const sec = t.closest(".he-sec"), dir = Number(t.dataset.mv);
    const sib = dir < 0 ? sec.previousElementSibling : sec.nextElementSibling;
    if (sib) dir < 0 ? sec.parentNode.insertBefore(sec, sib) : sec.parentNode.insertBefore(sib, sec);
    return;
  }
  if (t.matches("#heAddSec")) {
    $("#heSecs").insertAdjacentHTML("beforeend", helpSecHTML());
    $("#heSecs .he-sec:last-child .he-title").focus();
    return;
  }
  if (t.matches("#heGenText")) {
    $("#heText").value = helpPlainText($("#heTitle").value.trim(), collectHelpSections());
    toast("已按分栏重新生成");
    return;
  }
  if (t.matches(".del")) t.closest(".txt-item").remove();
});
$("#btnSaveTexts").onclick = async () => {
  const name = $("#textsName").value;
  let data;
  if (name === "help") {
    data = collectHelp();
    if (!data) return;
  } else {
    data = {};
    for (const key of $$("#textsBody .txt-key")) {
      const name2 = key.dataset.key;
      if (key.dataset.kind === "json") {
        try {
          data[name2] = JSON.parse($(".txt-json", key).value);
        } catch {
          toast(`键 ${name2} 的 JSON 格式错误`, true);
          return;
        }
      } else {
        const shown = $$(".txt-list input", key).map((i) => i.value.trim()).filter(Boolean);
        // 折叠时把没渲染出来的尾部原样接回去
        const cut = Number(key.dataset.shown || 0);
        const tail = cut ? (textsFull[name2] || []).slice(cut) : [];
        data[name2] = [...shown, ...tail];
      }
    }
  }
  try {
    await api("/api/admin/texts/save", { method: "POST", body: { name, data } });
    toast("文案已保存并热更新");
    loadTexts(); // 重渲染以刷新计数与折叠状态
  } catch (e) { toast(e.message, true); }
};

/* ---------- 配置（行式布局 + 步进器 + 开关 + 免冷却选人，参考上班族物语） ---------- */
let ignoreCD = []; // 免冷却用户 [{gid,uid,nickname}]

function rowHTML(key, desc, hint, ctrl, keyLabel) {
  return `<div class="cfg-row"><div class="cfg-info">
    <span class="cfg-name">${esc(desc)}</span><span class="cfg-key">${esc(keyLabel || key)}</span>
    ${hint ? `<div class="cfg-hint">${esc(hint)}</div>` : ""}
    </div><div class="cfg-ctrl">${ctrl}</div></div>`;
}

function ctrlFor(key, meta, val, group) {
  const tp = meta.type || "string";
  const gAttr = group ? ` data-g="${esc(group)}"` : "";
  const kAttr = ` data-k="${esc(key)}"`;
  if (tp === "bool") {
    return `<input type="checkbox" class="sw cfg-in"${gAttr}${kAttr}${val ? " checked" : ""}>`;
  }
  if (tp === "int" || tp === "float") {
    const step = tp === "float" ? 'step="any"' : 'step="1"';
    // min/max 来自 schema，输入框与步进器都据此夹值（手敲负数/超大值也会被拦）
    const lo = meta.min == null ? 0 : meta.min;
    const hi = meta.max == null ? "" : ` max="${esc(meta.max)}"`;
    return `<div class="stepper">
      <button type="button" class="st-btn" data-step="-1" tabindex="-1" aria-label="减少">−</button>
      <input type="number" ${step} min="${esc(lo)}"${hi} class="cfg-in"${gAttr}${kAttr} data-tp="${tp}" value="${esc(val == null ? lo : val)}">
      <button type="button" class="st-btn" data-step="1" tabindex="-1" aria-label="增加">+</button></div>`;
  }
  if (tp === "list") {
    return `<textarea class="cfg-in"${gAttr}${kAttr} data-tp="list" rows="2"
      placeholder="每行一个">${esc((val || []).join("\n"))}</textarea>`;
  }
  const hidden = key === "webui_password";
  return `<input type="${hidden ? "password" : "text"}" class="cfg-in"${gAttr}${kAttr} data-tp="string" value="${esc(val == null ? "" : val)}">`;
}

/* 数字输入的统一夹值：步进器与手动输入（blur）都走这里 */
function clampNum(inp) {
  const lo = inp.min === "" ? -Infinity : parseFloat(inp.min);
  const hi = inp.max === "" ? Infinity : parseFloat(inp.max);
  let v = parseFloat(inp.value);
  if (!isFinite(v)) v = isFinite(lo) ? lo : 0;
  v = Math.min(hi, Math.max(lo, v));
  inp.value = Math.round(v * 10000) / 10000;
  return v;
}

function bindStepper(btn) {
  const inp = btn.parentNode.querySelector("input");
  if (!inp) return;
  let t = null, r = null;
  const stop = () => { clearTimeout(t); clearInterval(r); t = r = null;
    document.removeEventListener("mouseup", stop); document.removeEventListener("mouseleave", stop); };
  const bump = () => {
    if (!inp.isConnected) return stop();   // 配置面板被重渲染后不再空转
    let v = parseFloat(inp.value); if (isNaN(v)) v = 0;
    const fine = (inp.step && inp.step !== "any") ? (parseFloat(inp.step) || 1)
      : (inp.dataset.tp === "float" ? 0.1 : 1);
    inp.value = v + parseFloat(btn.dataset.step) * fine;
    clampNum(inp);
    inp.classList.remove("st-flash"); void inp.offsetWidth; inp.classList.add("st-flash");
  };
  btn.addEventListener("mousedown", (e) => {
    e.preventDefault(); bump();
    document.addEventListener("mouseup", stop); document.addEventListener("mouseleave", stop);
    // 鼠标拖出浏览器后再松开：document.mouseup/mouseleave 都不会触发
    window.addEventListener("blur", stop);
    t = setTimeout(() => { r = setInterval(bump, 55); }, 400);
  });
  btn.addEventListener("click", (e) => e.preventDefault());
  btn.addEventListener("blur", stop);
}

async function loadConfig() {
  try {
    const { schema, config, hidden_keys } = await api("/api/admin/config");
    ignoreCD = (Array.isArray(config.ignoreCDUsers) ? config.ignoreCDUsers : [])
      .map(String).filter(Boolean).map((uid) => ({ uid }));
    let html = "";
    for (const [key, meta] of Object.entries(schema)) {
      const val = config[key];
      if (meta.type === "object") {
        html += `<div class="cfg-group-title">${esc(meta.description || key)}</div>`;
        for (const [sk, sm] of Object.entries(meta.items || {})) {
          const sv = (val || {})[sk] ?? sm.default;
          html += rowHTML(sk, sm.description || sk, sm.hint, ctrlFor(sk, sm, sv, key), `${key}.${sk}`);
        }
        continue;
      }
      if (key === "ignoreCDUsers") {
        html += rowHTML(key, meta.description, meta.hint,
          `<div class="chip-list" id="ncdChips"></div>
           <div class="ncd-add">
             <select id="ncdSelect"><option value="">加载玩家中…</option></select>
             <button type="button" class="btn sm primary" id="ncdAdd">添加</button>
           </div>`, key);
        continue;
      }
      const label = hidden_keys.includes(key)
        ? `${meta.hint || ""}（留空表示不修改）`.trim()
        : meta.hint;
      html += rowHTML(key, meta.description || key, label, ctrlFor(key, meta, val));
    }
    $("#cfgForm").innerHTML = html;
    $$("#cfgForm .st-btn").forEach(bindStepper);
    // 手敲的值在失焦时也按 schema 的 min/max 夹一次
    $$('#cfgForm input[type=number]').forEach((i) =>
      i.addEventListener("blur", () => clampNum(i)));
    renderIgnoreCD();
    await loadPlayerOptions();
  } catch (e) { toast(e.message, true); }
}

function renderIgnoreCD() {
  const box = $("#ncdChips");
  if (!box) return;
  box.innerHTML = ignoreCD
    .map((u) => `<span class="chip-item">${esc(u.label || u.uid)}
      <button type="button" class="cx" data-uid="${esc(u.uid)}" aria-label="移除">✕</button></span>`)
    .join("");
  $$(".chip-item .cx", box).forEach((b) =>
    b.addEventListener("click", () => {
      ignoreCD = ignoreCD.filter((u) => u.uid !== b.dataset.uid);
      renderIgnoreCD();
    }));
}

async function loadPlayerOptions() {
  const sel = $("#ncdSelect");
  if (!sel) return;
  try {
    // 一次请求拿全部群的全部玩家。旧写法按 /api/players 分页接口取，
    // 每群只能拿到前 20 人，第 21 个人永远选不到。
    const { players } = await api("/api/players_all");
    if (!players.length) {
      sel.innerHTML = `<option value="">暂无玩家数据</option>`;
      sel.disabled = true;
      return;
    }
    // 给已在名单里的 uid 补上昵称，否则刷新后只剩一串数字认不出是谁
    const names = new Map(players.map((p) => [String(p.uid), `${p.nickname}（${p.uid}）`]));
    let changed = false;
    ignoreCD.forEach((u) => {
      if (!u.label && names.has(u.uid)) { u.label = names.get(u.uid); changed = true; }
    });
    if (changed) renderIgnoreCD();

    const byGroup = {};
    players.forEach((p) => (byGroup[p.gid] = byGroup[p.gid] || []).push(p));
    sel.disabled = false;
    sel.innerHTML =
      `<option value="">选择玩家…</option>` +
      Object.entries(byGroup)
        .map(([gid, list]) =>
          `<optgroup label="群 ${esc(gid)}">` +
          list.map((p) => `<option value="${esc(p.uid)}" data-gid="${esc(p.gid)}">${esc(p.nickname)}（${esc(p.uid)}）</option>`).join("") +
          `</optgroup>`)
        .join("");
  } catch { sel.innerHTML = `<option value="">玩家列表加载失败</option>`; }
}

document.addEventListener("click", (e) => {
  if (!e.target.matches("#ncdAdd")) return;
  const sel = $("#ncdSelect");
  const opt = sel?.selectedOptions[0];
  if (!opt || !opt.value) { toast("请先选择玩家", true); return; }
  if (ignoreCD.some((u) => u.uid === opt.value)) { toast("该玩家已在列表中", true); return; }
  ignoreCD.push({ uid: opt.value, gid: opt.dataset.gid || "", label: opt.textContent });
  renderIgnoreCD();
});

$("#btnSaveCfg").onclick = async () => {
  const values = {};
  $$("#cfgForm .cfg-in").forEach((el) => {
    const key = el.dataset.k;
    const group = el.dataset.g;
    // 先算出值，再按 data-g 归位。旧写法在 checkbox 分支提前 return，
    // 嵌套配置组里的开关会被平铺到顶层、后端找不到该键而静默丢弃。
    const v = el.type === "checkbox" ? el.checked : el.value;
    if (group) (values[group] = values[group] || {})[key] = v;
    else values[key] = v;
  });
  values.ignoreCDUsers = ignoreCD.map((u) => u.uid);
  try {
    const r = await api("/api/admin/config/save", { method: "POST", body: { values } });
    toast(`已保存 ${r.applied} 项${r.persisted ? "（已持久化）" : ""}`);
    (r.notes || []).forEach((n) => toast(n, true));   // 被拒绝/被夹值的项要让人看见
    loadConfig();
  } catch (e) { toast(e.message, true); }
};

/* ---------- 确认弹窗（独立第二层，不再复用编辑弹窗的 DOM） ---------- */
const ICO_WARN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 20h16a2 2 0 0 0 1.73-2Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
const ICO_EDIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>';

function askConfirm(title, msg, cb) {
  $("#askIco").innerHTML = ICO_WARN;
  $("#askTitle").textContent = title;
  $("#askMsg").textContent = msg;
  openModal("#askMask");
  $("#askOk").onclick = () => { closeModal("#askMask"); cb?.(); };
}
$("#askCancel").onclick = () => closeModal("#askMask");

/* ---------- 弹窗通用：焦点管理 + Escape 关闭 ---------- */
let lastFocus = null;
function openModal(sel) {
  lastFocus = document.activeElement;
  const mask = $(sel);
  mask.classList.add("show");
  ($("input, button", mask) || $(".modal", mask))?.focus();
}
function closeModal(sel) {
  $(sel).classList.remove("show");
  if (lastFocus && lastFocus.isConnected) lastFocus.focus();
}
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const top = $(".modal-mask.show:last-of-type") || $(".modal-mask.show");
  if (top) closeModal("#" + top.id);
});

/* ---------- 自定义下拉（移植上班族物语，支持 optgroup 与键盘操作） ----------
   原生 select 保留在 DOM 里当数据载体（隐藏），值仍从 sel.value / selectedOptions 读，
   只把弹层换成自绘的 .sel-list —— 祖先元素的 transform 动画会让 Chromium 原生
   下拉列表定位错乱、闪烁甚至选不中，自绘弹层不受影响。 */
const selOptModel = (o) => ({ v: o.value, t: o.textContent, on: o.selected, dis: o.disabled });

function selModel(sel) {
  const rows = [];
  for (const node of sel.children) {
    if (node.tagName === "OPTGROUP") {
      rows.push({ group: node.label });
      for (const o of node.children) if (o.tagName === "OPTION") rows.push(selOptModel(o));
    } else if (node.tagName === "OPTION") rows.push(selOptModel(node));
  }
  return rows;
}

function selListHTML(sel, listId) {
  const rows = selModel(sel);
  if (!rows.length) return `<div class="sel-opt dis">无可选项</div>`;
  let i = 0;
  return rows.map((r) => r.group !== undefined
    ? `<div class="sel-group" role="group" aria-label="${esc(r.group)}">${esc(r.group)}</div>`
    : `<div class="sel-opt${r.on ? " on" : ""}${r.dis ? " dis" : ""}" role="option"
         id="${listId}-o${i++}" aria-selected="${r.on ? "true" : "false"}"
         ${r.dis ? 'aria-disabled="true"' : ""} data-v="${esc(r.v)}">${esc(r.t)}</div>`
  ).join("");
}

function syncSelect(wrap) {
  const sel = $("select", wrap);
  if (!sel) return;
  const cur = sel.selectedOptions[0] || sel.options[0];
  $(".sel-label", wrap).textContent = cur ? cur.textContent : "请选择";
  const list = $(".sel-list", wrap);
  list.innerHTML = selListHTML(sel, list.id);
  wrap.classList.toggle("locked", sel.disabled);
  $(".sel-trigger", wrap).setAttribute("aria-disabled", String(!!sel.disabled));
}

function closeSelects(except) {
  $$(".sel-wrap.open").forEach((w) => {
    if (w === except) return;
    w.classList.remove("open");
    const t = $(".sel-trigger", w);
    t?.setAttribute("aria-expanded", "false");
    t?.removeAttribute("aria-activedescendant");
    $$(".sel-opt.hl", w).forEach((o) => o.classList.remove("hl"));
  });
}

let _selId = 0;
function enhanceSelect(sel) {
  if (sel.closest(".sel-wrap")) return;
  const id = `sel${++_selId}`;
  const wrap = document.createElement("div");
  wrap.className = "sel-wrap";
  // aria-labelledby 沿用原生 select 上的标注，否则自绘 trigger 是个无名 combobox
  const label = sel.getAttribute("aria-labelledby");
  wrap.innerHTML = `<div class="sel-trigger" tabindex="0" role="combobox"
      aria-expanded="false" aria-haspopup="listbox" aria-controls="${id}-list"
      ${label ? `aria-labelledby="${esc(label)}"` : ""}>
      <span class="sel-label"></span><i class="sel-arrow" aria-hidden="true"></i></div>
    <div class="sel-list" id="${id}-list" role="listbox"></div>`;
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  const trigger = $(".sel-trigger", wrap), list = $(".sel-list", wrap);

  const place = () => {
    // 弹层用 fixed 定位并按 trigger 的视口坐标摆放：
    // 这样不受任何祖先 overflow:hidden 裁剪，也不受祖先 transform 影响
    const r = trigger.getBoundingClientRect();
    const below = window.innerHeight - r.bottom;
    const up = below < 300 && r.top > below;
    wrap.classList.toggle("up", up);
    list.style.left = `${r.left}px`;
    list.style.width = `${r.width}px`;
    list.style.maxHeight = `${Math.max(140, (up ? r.top : below) - 16)}px`;
    if (up) { list.style.top = "auto"; list.style.bottom = `${window.innerHeight - r.top + 5}px`; }
    else { list.style.bottom = "auto"; list.style.top = `${r.bottom + 5}px`; }
  };

  const setOpen = (on) => {
    if (on) { closeSelects(wrap); place(); }
    wrap.classList.toggle("open", on);
    trigger.setAttribute("aria-expanded", String(on));
    if (!on) {
      $$(".sel-opt.hl", list).forEach((o) => o.classList.remove("hl"));
      trigger.removeAttribute("aria-activedescendant");
    } else $(".sel-opt.on", list)?.scrollIntoView({ block: "nearest" });
  };

  const pick = (opt) => {
    if (!opt || opt.classList.contains("dis") || !opt.hasAttribute("data-v")) return;
    sel.value = opt.dataset.v;
    setOpen(false);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    syncSelect(wrap);
  };

  const highlight = (opt) => {
    $$(".sel-opt.hl", list).forEach((o) => o.classList.remove("hl"));
    opt.classList.add("hl");
    trigger.setAttribute("aria-activedescendant", opt.id);
    opt.scrollIntoView({ block: "nearest" });
  };

  // 祖先 hover 引发 transform（.cfg-row:hover translateX(3px 等）会让 fixed
  // 包含块变成祖先，弹层会粘在旧位置——鼠标进入 trigger 时重新摆位
  trigger.addEventListener("mouseenter", () => { if (wrap.classList.contains("open")) place(); });
  trigger.addEventListener("click", (e) => { e.stopPropagation(); setOpen(!wrap.classList.contains("open")); });
  list.addEventListener("click", (e) => { e.stopPropagation(); pick(e.target.closest(".sel-opt")); });
  trigger.addEventListener("keydown", (e) => {
    const opts = $$(".sel-opt:not(.dis)", list);
    if (!opts.length) return;
    const idx = opts.findIndex((o) => o.classList.contains("hl"));
    const open = wrap.classList.contains("open");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) return setOpen(true);
      const base = idx < 0 ? opts.findIndex((o) => o.classList.contains("on")) : idx;
      highlight(opts[Math.min(opts.length - 1, Math.max(0, base + (e.key === "ArrowDown" ? 1 : -1)))]);
    } else if (e.key === "Home" || e.key === "End") {
      if (!open) return;
      e.preventDefault();
      highlight(e.key === "Home" ? opts[0] : opts[opts.length - 1]);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!open) setOpen(true);
      else pick(opts[idx] || $(".sel-opt.on", list));
    } else if (e.key === "Escape") {
      setOpen(false);
    } else if (e.key === "Tab") {
      setOpen(false);   // 键盘离开时不把弹层留在页面上
    } else if (e.key.length === 1) {
      // 首字母跳转（原生 select 的习惯）
      const ch = e.key.toLowerCase();
      const hit = opts.find((o) => o.textContent.trim().toLowerCase().startsWith(ch));
      if (hit) { if (!open) setOpen(true); highlight(hit); }
    }
  });
  trigger.addEventListener("blur", () => setTimeout(() => {
    if (!wrap.contains(document.activeElement)) setOpen(false);
  }, 0));
  // 打开期间跟随滚动/缩放重新定位
  wrap._place = place;

  // 选项被业务代码重建 / 禁用状态变化时，重画弹层与标签
  new MutationObserver(() => syncSelect(wrap))
    .observe(sel, { childList: true, attributes: true, attributeFilter: ["disabled"] });
  sel.addEventListener("change", () => syncSelect(wrap));
  syncSelect(wrap);
}

const enhanceAllSelects = () => $$("select").forEach(enhanceSelect);
let _selTimer = null;
new MutationObserver(() => {
  if (_selTimer) clearTimeout(_selTimer);
  _selTimer = setTimeout(enhanceAllSelects, 0);
}).observe(document.body, { childList: true, subtree: true });
document.addEventListener("click", () => closeSelects());
window.addEventListener("resize", () => closeSelects());
window.addEventListener("scroll", () => {
  $$(".sel-wrap.open").forEach((w) => w._place && w._place());
}, { passive: true });
enhanceAllSelects();

/* ---------- 移动端表格标签化（移植自上班族物语） ---------- */
function stampTableLabels() {
  if (window.innerWidth > 768) return;
  $$(".tbl").forEach((tb) => {
    const heads = $$("thead th", tb).map((th) => th.textContent.replace(/\s+/g, " ").trim());
    $$("tbody tr", tb).forEach((tr) => {
      $$("td", tr).forEach((td, i) => {
        if (td.hasAttribute("data-th")) return;
        if (td.getAttribute("colspan")) return;
        if (heads[i]) td.setAttribute("data-th", heads[i]);
      });
    });
  });
}
let _stampTimer = null;
new MutationObserver(() => {
  if (window.innerWidth > 768) return;
  if (_stampTimer) clearTimeout(_stampTimer);
  _stampTimer = setTimeout(stampTableLabels, 60);
}).observe(document.body, { childList: true, subtree: true });
window.addEventListener("resize", stampTableLabels);

/* ---------- 浮尘粒子（移植自上班族物语） ---------- */
(function () {
  const box = document.getElementById("moteField");
  if (!box) return;
  for (let i = 0; i < 42; i++) {
    const m = document.createElement("i");
    m.className = "mote";
    m.style.left = Math.random() * 100 + "vw";
    m.style.top = Math.random() * 100 + "vh";
    m.style.animationDelay = (Math.random() * 4).toFixed(1) + "s";
    m.style.animationDuration = (2 + Math.random() * 3).toFixed(1) + "s";
    m.style.width = m.style.height = (Math.random() * 2 + 1).toFixed(1) + "px";
    box.appendChild(m);
  }
})();

/* ---------- 滚动进度条 ---------- */
(function () {
  const bar = document.getElementById("scrollProgress");
  if (!bar) return;
  let ticking = false;
  function upd() {
    const h = document.documentElement;
    const max = h.scrollHeight - h.clientHeight;
    const p = max > 0 ? Math.min(1, (h.scrollTop || document.body.scrollTop) / max) : 0;
    bar.style.transform = "scaleX(" + p + ")";
    bar.classList.toggle("on", p > 0.002);
    ticking = false;
  }
  window.addEventListener("scroll", () => {
    if (!ticking) { requestAnimationFrame(upd); ticking = true; }
  }, { passive: true });
  window.addEventListener("resize", upd);
  upd();
})();

/* ---------- 时钟与启动 ---------- */
const clockEl = $("#clock");
function tick() {
  clockEl.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
}
setInterval(tick, 1000);

async function boot() {
  state.meta = await api("/api/meta");
  state.size = state.meta.page_size || 20;
  $("#verChip").textContent = "v" + state.meta.version;
  $("#logoutChip").hidden = !state.meta.auth_required;
  state.groups = (await api("/api/groups")).groups;
  renderGroupPicker();
  tick();
  moveInk($("#nav .nav-btn.on"));
  stampTableLabels();
  await loadOverview();
}

(async () => {
  const meta = await api("/api/meta").catch(() => null);
  if (!meta) return;
  const check = await api("/api/auth/check").catch(() => ({ ok: false, required: false }));
  if (check.required && !check.ok) { showLogin(true); return; }
  if (check.required && check.ok && check.must_reset) {
    // 当前 cookie 还活着但密码文件标了 must_reset：进重置层
    showReset(true);
    $("#resetNew").focus();
    return;
  }
  // boot 里的 401 也要兜住，否则初始化半途中断、页面停在"加载中…"
  await boot().catch((e) => toast(e.message, true));
})();
