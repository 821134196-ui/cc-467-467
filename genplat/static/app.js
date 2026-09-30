/* 备用发电机运维平台前端：原生 JS，零依赖。
 * 视图：机组总览 / 试运行日历 / 步骤执行 / 参数趋势 / 故障停机 / 燃油台账 / 审批记录。
 */
"use strict";

const STEP_DEFS = [
  { code: "visual", order: 1, name: "外观检查" },
  { code: "fuel", order: 2, name: "油位检查" },
  { code: "battery", order: 3, name: "启动电池" },
  { code: "no_load", order: 4, name: "空载启动" },
  { code: "loaded", order: 5, name: "带载测试" },
];
const STEP_NAME = Object.fromEntries(STEP_DEFS.map(s => [s.code, s.name]));
const STATUS_LABEL = {
  standby: "待命", running: "运行中", fault: "故障", locked: "维修封锁",
  draft: "草稿", pending: "待批准", approved: "已批准", cancelled: "已取消",
  done: "合格完成", failed: "故障停机", aborted: "中止",
  pass: "合格", fail: "不合格", skipped: "已越过",
};
const EVENT_LABEL = {
  plan_submitted: "计划提交", plan_approved: "计划批准", plan_rejected: "计划驳回",
  plan_cancelled: "计划取消", session_started: "会话开始", session_blocked: "启动被阻断",
  session_done: "试运行合格完成", session_aborted: "会话中止",
  step_completed: "步骤合格", step_failed: "步骤失败", step_skipped: "步骤越过",
  skip_requested: "越步申请", skip_approved: "越步批准", skip_denied: "越步驳回",
  start_failure: "启动失败", voltage_out_of_range: "电压越限",
  abnormal_noise: "异常噪声", oil_leak: "漏油", load_interrupted: "带载中断",
  fuel_discrepancy: "燃油差异", fault_shutdown: "故障停机",
  fueling_created: "加油登记", fueling_revised: "加油数量修订",
  fueling_reviewed: "加油修订复核", lockout_on: "封锁挂牌", lockout_off: "封锁解除",
  alert_due_plan: "到期提醒", alert_low_fuel: "低油位提醒", sim_mode_changed: "工况切换",
};
const ROLE_LABEL = { operator: "值班员", supervisor: "工程主管", fuel_clerk: "加油员" };

// ---------- 工具 ----------
const $ = sel => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtNum = (v, d = 1) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleTimeString("zh-CN", { hour12: false });
}
function fmtDT(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso :
    `${d.toLocaleDateString("zh-CN")} ${d.toLocaleTimeString("zh-CN", { hour12: false })}`;
}
function toLocalInput(iso) {
  const d = iso ? new Date(iso) : new Date();
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T` +
    `${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fromLocalInput(v) { return new Date(v).toISOString(); }
function dur(sec) {
  if (sec === null || sec === undefined) return "—";
  const m = Math.floor(sec / 60), s = sec % 60;
  return m >= 60 ? `${Math.floor(m / 60)}h${m % 60}m` : `${m}m${s}s`;
}

async function api(path, opts = {}) {
  const headers = { "X-User-Id": state.uid };
  if (opts.body !== undefined && !(opts.body instanceof ArrayBuffer) &&
      typeof opts.body !== "string") {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `请求失败 ${res.status}`);
  return data;
}

// ---------- 全局状态 ----------
const state = {
  uid: localStorage.getItem("gp_uid") || "u-op1",
  users: [],
  me: null,
  timer: null,
  calCursor: null,   // 日历当前月份
  trendUnit: null,
  trendSession: null,
  fuelUnit: null,
};

// ---------- 图标 ----------
const ICON = {
  lock: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="11" width="14" height="9" rx="1.5"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
  warn: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3 2.5 20h19L12 3z"/><path d="M12 10v5M12 18v.5"/></svg>',
  fuel: '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="3" width="9" height="18" rx="1"/><path d="M15 9h2.5a1.5 1.5 0 0 1 1.5 1.5V16a1.5 1.5 0 0 0 3 0V8l-3-3M9 3h3"/></svg>',
  bolt: '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/></svg>',
  check: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3"><path d="M4 12.5 10 18 20 6"/></svg>',
};

function badge(status, extra = "") {
  const icon = status === "locked" ? `<span style="display:inline-flex;vertical-align:-2px">${ICON.lock}</span>` : "";
  return `<span class="badge ${esc(status)} ${extra}">${icon}${esc(STATUS_LABEL[status] || status)}</span>`;
}

function toast(msg, kind = "ok") {
  const t = el("div", `toast ${kind}`, esc(msg));
  $("#toastLayer").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, 2600);
  setTimeout(() => t.remove(), 3000);
}

// ---------- 模态 ----------
function modal(title, bodyHtml, footerHtml = "") {
  const layer = $("#modalLayer");
  $("#modalBox").innerHTML =
    `<div class="modal-hd"><h3>${esc(title)}</h3>` +
    `<button class="xclose" data-close>×</button></div>` +
    `<div class="modal-bd">${bodyHtml}</div>` +
    (footerHtml ? `<div class="modal-ft">${footerHtml}</div>` : "");
  layer.hidden = false;
  $("#modalBox").querySelector("[data-close]").onclick = closeModal;
  return $("#modalBox");
}
function closeModal() { $("#modalLayer").hidden = true; }
$("#modalLayer").addEventListener("click", e => {
  if (e.target.id === "modalLayer") closeModal();
});

// ---------- 轮询 ----------
function setTimer(fn, ms) {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(fn, ms);
}

// ============================================================
// 视图：机组总览
// ============================================================
async function viewUnits() {
  setTimer(viewUnits, 4000);
  const data = await api("/api/overview");
  const v = $("#view");
  v.innerHTML = `
    <div class="page-head"><h1>机组总览</h1>
      <span class="hint">单行参数带 · 模拟适配器实时刷新 · 维修封锁机组不计入应急可用容量</span></div>
    <div class="cards-row">
      <div class="stat accent-green"><div class="k">应急可用容量</div>
        <div class="v">${fmtNum(data.available_kw, 0)} <small>kW</small></div></div>
      <div class="stat"><div class="k">装机总容量</div>
        <div class="v">${fmtNum(data.total_kw, 0)} <small>kW</small></div></div>
      <div class="stat accent-gray"><div class="k">封锁容量（排除）</div>
        <div class="v">${fmtNum(data.locked_out_kw, 0)} <small>kW</small></div></div>
      <div class="stat accent-yellow"><div class="k">机组数量</div>
        <div class="v">${data.units.length} <small>台</small></div></div>
    </div>
    <div id="unitBands"></div>`;

  const wrap = $("#unitBands");
  for (const u of data.units) {
    const readings = await api(`/api/units/${u.id}/readings?limit=1`);
    const r = readings[0];
    const running = u.status === "running";
    const vOk = r && r.voltage > 100 && r.voltage >= 385 && r.voltage <= 415;
    const vBad = r && r.voltage > 100 && !(r.voltage >= 385 && r.voltage <= 415);
    const fuelCls = u.fuel_level < 25 ? "bad" : u.fuel_level < 40 ? "warn" : "ok";
    const batCls = u.battery_voltage < 12.4 ? "bad" : u.battery_voltage < 12.6 ? "warn" : "ok";
    const band = el("div", `unit-band ${u.status}`);
    band.innerHTML = `
      <div class="ub-id">
        <div class="ub-code">${esc(u.code)}</div>
        <div class="ub-name">${esc(u.name)}</div>
        <div class="ub-bld">${esc(u.building)} · <span class="num">${fmtNum(u.rated_power_kw, 0)} kW</span></div>
        ${u.status === "locked" ? `<div class="ub-lockline">${ICON.lock}
            <span>维修封锁：${esc(u.lockout_reason || "已挂牌，禁止启动")}<br>
            挂牌：${esc(u.lockout_by || "")} · ${u.lockout_at ? esc(fmtDT(u.lockout_at)) : ""}</span></div>` : ""}
        <div style="margin-top:6px">${badge(u.status)}
          <span class="badge draft flag-chip">工况·${({ normal: "正常", start_fail: "启动失败", leak: "漏油" })[u.sim_mode]}</span></div>
      </div>
      <div class="ub-cell">
        <div class="lab"><span class="live-dot ${running ? "" : "idle"}"></span>电压</div>
        <div class="val ${vBad ? "bad" : vOk ? "ok" : ""}">${r && r.voltage > 1 ? fmtNum(r.voltage, 1) : "—"} <small>V</small></div>
      </div>
      <div class="ub-cell">
        <div class="lab">频率 / 负载</div>
        <div class="val" style="font-size:13.5px">
          ${r && r.frequency > 1 ? fmtNum(r.frequency, 2) : "—"}<small> Hz</small>
          &nbsp;${r && r.load_kw > 1 ? fmtNum(r.load_kw, 1) : ""}<small>${r && r.load_kw > 1 ? " kW" : ""}</small></div>
      </div>
      <div class="ub-cell">
        <div class="lab">${ICON.fuel} 燃油液位 ${u.low_fuel ? '<span class="badge failed flag-chip">低油位</span>' : ""}</div>
        <div class="val ${fuelCls}">${fmtNum(u.fuel_level, 1)} <small>%</small></div>
        <div class="minibar"><i class="${fuelCls}" style="width:${u.fuel_level}%"></i></div>
      </div>
      <div class="ub-cell">
        <div class="lab">启动电池</div>
        <div class="val ${batCls}">${fmtNum(u.battery_voltage, 2)} <small>V</small></div>
        <div class="minibar"><i class="${batCls === "ok" ? "" : batCls}" style="width:${Math.min(100, (u.battery_voltage / 13.8) * 100)}%"></i></div>
      </div>
      <div class="ub-actions">
        <button class="btn sm yellow" data-act="run">试运行</button>
        <button class="btn sm ghost" data-act="trend">参数趋势</button>
        ${u.status === "locked"
          ? '<button class="btn sm green" data-act="unlock">解除封锁</button>'
          : '<button class="btn sm danger" data-act="lock">维修封锁</button>'}
      </div>`;
    band.querySelector('[data-act="run"]').onclick = () => { location.hash = "#/run"; };
    band.querySelector('[data-act="trend"]').onclick = () => {
      state.trendUnit = u.id; location.hash = "#/trends";
    };
    band.querySelector('[data-act="lock"]').onclick = () => lockoutDialog(u, true);
    band.querySelector('[data-act="unlock"]').onclick = () => lockoutDialog(u, false);
    wrap.appendChild(band);
  }
}

function lockoutDialog(u, lock) {
  if (!lock) {
    if (!confirm(`确认解除 ${u.code} 的维修封锁？`)) return;
    api(`/api/units/${u.id}/lockout`, { method: "DELETE" })
      .then(() => { toast("封锁已解除"); viewUnits(); })
      .catch(e => toast(e.message, "err"));
    return;
  }
  const box = modal(`维修封锁 · ${u.code}`,
    `<label class="fld">封锁原因（将显示在机组参数带，并禁止启动、排除容量）
      <textarea id="lockReason" rows="3" placeholder="例如：等待更换节温器，禁止合闸"></textarea></label>
      <p class="muted" style="font-size:12px">仅工程主管可操作；运行中的机组须先停机。</p>`,
    `<button class="btn ghost" data-close>取消</button>
     <button class="btn danger" id="lockOk">${ICON.lock} 确认封锁挂牌</button>`);
  box.querySelector("#lockOk").onclick = () => {
    const reason = box.querySelector("#lockReason").value.trim();
    if (!reason) return toast("请填写封锁原因", "err");
    api(`/api/units/${u.id}/lockout`, { method: "POST", body: { reason } })
      .then(() => { closeModal(); toast("已封锁挂牌"); viewUnits(); })
      .catch(e => toast(e.message, "err"));
  };
}

// ============================================================
// 视图：试运行日历
// ============================================================
async function viewCalendar() {
  setTimer(viewCalendar, 15000);
  const plans = await api("/api/plans");
  if (!state.calCursor) {
    const n = new Date();
    state.calCursor = new Date(n.getFullYear(), n.getMonth(), 1);
  }
  const cur = state.calCursor;
  const y = cur.getFullYear(), m = cur.getMonth();
  const first = new Date(y, m, 1);
  const start = new Date(first);
  start.setDate(1 - first.getDay());
  const v = $("#view");
  v.innerHTML = `
    <div class="page-head"><h1>试运行日历</h1>
      <span class="hint">计划须工程主管批准后方可执行</span>
      <div style="flex:1"></div>
      <div class="btn-row">
        <button class="btn ghost sm" id="calPrev">‹ 上月</button>
        <button class="btn ghost sm" id="calToday">今天</button>
        <button class="btn ghost sm" id="calNext">下月 ›</button>
        <button class="btn yellow sm" id="calNew">+ 新建计划</button>
      </div>
    </div>
    <div class="card"><div class="card-bd">
      <div class="legend" style="margin-bottom:10px">
        <span><i style="background:#2F9E44"></i>已批准</span>
        <span><i style="background:#E0A329"></i>待批准</span>
        <span><i style="background:#C3C9CE"></i>草稿</span>
        <span><i style="background:#B7BFC5"></i>已取消</span>
      </div>
      <div class="cal-wrap"><div>
        <div class="calendar">${["日", "一", "二", "三", "四", "五", "六"]
          .map(d => `<div class="cal-dow">周${d}</div>`).join("")}</div>
        <div class="calendar" id="calGrid" style="margin-top:6px"></div>
      </div></div>
    </div></div>
    <div class="card"><div class="card-hd"><h2>全部计划</h2></div>
      <div id="planList"></div></div>`;

  $("#calPrev").onclick = () => { state.calCursor = new Date(y, m - 1, 1); viewCalendar(); };
  $("#calNext").onclick = () => { state.calCursor = new Date(y, m + 1, 1); viewCalendar(); };
  $("#calToday").onclick = () => { state.calCursor = new Date(new Date().getFullYear(), new Date().getMonth(), 1); viewCalendar(); };
  $("#calNew").onclick = newPlanDialog;

  const grid = $("#calGrid");
  const todayStr = new Date().toDateString();
  for (let i = 0; i < 42; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    const cell = el("div", `cal-cell ${d.getMonth() === m ? "" : "other"} ${d.toDateString() === todayStr ? "today" : ""}`);
    cell.innerHTML = `<div class="cal-date">${d.getMonth() + 1}/${d.getDate()}${d.toDateString() === todayStr ? " · 今天" : ""}</div>`;
    for (const p of plans) {
      const pd = new Date(p.scheduled_at);
      if (pd.toDateString() === d.toDateString()) {
        const ev = el("div", `cal-ev ${p.status}`,
          `<b>${esc(p.unit_code)}</b>${fmtTime(p.scheduled_at)} ${esc(STATUS_LABEL[p.status])}`);
        ev.onclick = () => planDialog(p);
        cell.appendChild(ev);
      }
    }
    grid.appendChild(cell);
  }

  const list = $("#planList");
  if (!plans.length) { list.innerHTML = '<div class="empty">暂无计划</div>'; return; }
  list.innerHTML = `<table class="grid"><thead><tr>
    <th>计划时间</th><th>机组</th><th>楼宇</th><th>说明</th><th>状态</th><th></th></tr></thead>
    <tbody>${plans.map(p => `<tr class="clickable" data-pid="${p.id}">
      <td class="num">${esc(fmtDT(p.scheduled_at))}</td>
      <td><b>${esc(p.unit_code)}</b> ${esc(p.unit_name)}</td>
      <td>${esc(p.building)}</td><td>${esc(p.note || "—")}</td>
      <td>${badge(p.status)}</td><td class="right"><button class="btn sm ghost">详情</button></td>
    </tr>`).join("")}</tbody></table>`;
  list.querySelectorAll("tr[data-pid]").forEach(tr =>
    tr.onclick = () => planDialog(plans.find(p => p.id == tr.dataset.pid)));
}

function newPlanDialog() {
  api("/api/overview").then(data => {
    const opts = data.units.map(u =>
      `<option value="${u.id}">${esc(u.code)} · ${esc(u.name)}（${esc(u.building)}）</option>`).join("");
    const box = modal("新建试运行计划",
      `<div class="form-grid">
        <label class="fld">机组<select id="npUnit">${opts}</select></label>
        <label class="fld">计划时间<input type="datetime-local" id="npTime" value="${toLocalInput()}"></label>
       </div>
       <p style="margin-top:12px"><label class="fld">说明<textarea id="npNote" rows="2"></textarea></label></p>
       <p class="muted" style="font-size:12px">创建后为草稿，提交后由工程主管审批。</p>`,
      `<button class="btn ghost" data-close>取消</button>
       <button class="btn yellow" id="npOk">创建草稿</button>`);
    box.querySelector("#npOk").onclick = () => {
      const body = {
        unit_id: Number(box.querySelector("#npUnit").value),
        scheduled_at: fromLocalInput(box.querySelector("#npTime").value),
        note: box.querySelector("#npNote").value,
      };
      api("/api/plans", { method: "POST", body })
        .then(p => { closeModal(); toast("计划已创建（草稿）"); viewCalendar(); planDialog(p); })
        .catch(e => toast(e.message, "err"));
    };
  });
}

function planDialog(p) {
  const box = modal(`计划 #${p.id} · ${p.unit_code}`,
    `<dl class="kv">
      <dt>计划时间</dt><dd class="num">${esc(fmtDT(p.scheduled_at))}</dd>
      <dt>机组</dt><dd>${esc(p.unit_name)} · ${esc(p.building)}</dd>
      <dt>状态</dt><dd>${badge(p.status)}</dd>
      <dt>说明</dt><dd>${esc(p.note || "—")}</dd>
      <dt>取消原因</dt><dd>${esc(p.cancel_reason || "—")}</dd>
     </dl>
     <div class="btn-row" id="planActions" style="margin-top:14px"></div>`);
  const acts = box.querySelector("#planActions");
  const add = (txt, cls, fn) => {
    const b = el("button", `btn sm ${cls}`, txt); b.onclick = fn; acts.appendChild(b);
  };
  if (p.status === "draft")
    add("提交审批", "yellow", () => api(`/api/plans/${p.id}/submit`, { method: "POST" })
      .then(p2 => { closeModal(); toast("已提交审批"); viewCalendar(); planDialog(p2); })
      .catch(e => toast(e.message, "err")));
  if (p.status === "pending") {
    add("批准（主管）", "green", () => api(`/api/plans/${p.id}/review`, { method: "POST", body: { approve: true } })
      .then(p2 => { closeModal(); toast("已批准"); viewCalendar(); planDialog(p2); })
      .catch(e => toast(e.message, "err")));
    add("驳回（主管）", "danger", () => api(`/api/plans/${p.id}/review`, { method: "POST", body: { approve: false } })
      .then(() => { closeModal(); toast("已驳回为草稿"); viewCalendar(); })
      .catch(e => toast(e.message, "err")));
  }
  if (p.status === "approved")
    add("开始试运行", "yellow", () => api(`/api/plans/${p.id}/sessions`, { method: "POST" })
      .then(s => { closeModal(); state.runSession = s.id; location.hash = "#/run"; })
      .catch(e => toast(e.message, "err")));
  if (["draft", "pending", "approved"].includes(p.status))
    add("取消计划", "danger", () => {
      const reason = prompt("取消原因（将留痕）");
      if (reason === null) return;
      api(`/api/plans/${p.id}/cancel`, { method: "POST", body: { reason } })
        .then(() => { closeModal(); toast("计划已取消并留痕"); viewCalendar(); })
        .catch(e => toast(e.message, "err"));
    });
  add("查看事件", "ghost", () => { closeModal(); location.hash = "#/faults"; });
}

// ============================================================
// 视图：步骤执行
// ============================================================
async function viewRun() {
  setTimer(viewRun, 1800);
  const active = await api("/api/sessions?status=running");
  const plans = await api("/api/plans?status=approved");
  const v = $("#view");
  v.innerHTML = `<div class="page-head"><h1>步骤执行</h1>
      <span class="hint">外观 → 油位 → 电池 → 空载启动 → 带载测试，须按序完成；越过检查须工程主管批准</span></div>
    <div id="runBody"></div>`;
  const body = $("#runBody");

  let sid = state.runSession;
  if (sid && !active.find(s => s.id === sid)) {
    const s = await api(`/api/sessions/${sid}`).catch(() => null);
    if (s) return renderSession(s);
    state.runSession = null; sid = null;
  }
  if (active.length) return renderSession(await api(`/api/sessions/${active[0].id}`));
  state.runSession = null;

  body.innerHTML = `<div class="card"><div class="card-hd"><h2>待执行计划（已批准）</h2></div>
    <div id="startable"></div></div>
    <div class="card"><div class="card-hd"><h2>历史会话</h2></div><div id="histSessions"></div></div>`;
  const st = $("#startable");
  if (!plans.length) st.innerHTML = '<div class="empty">没有已批准计划，请先在日历中新建并完成审批</div>';
  else st.innerHTML = `<table class="grid"><thead><tr><th>计划时间</th><th>机组</th><th>说明</th><th></th></tr></thead>
    <tbody>${plans.map(p => `<tr><td class="num">${esc(fmtDT(p.scheduled_at))}</td>
      <td><b>${esc(p.unit_code)}</b></td><td>${esc(p.note || "")}</td>
      <td class="right"><button class="btn sm yellow" data-pid="${p.id}">开始试运行</button></td></tr>`).join("")}</tbody></table>`;
  st.querySelectorAll("button[data-pid]").forEach(b => b.onclick = () =>
    api(`/api/plans/${b.dataset.pid}/sessions`, { method: "POST" })
      .then(s => { state.runSession = s.id; toast(`会话 #${s.id} 已开始，仅此一个运行会话`); viewRun(); })
      .catch(e => toast(e.message, "err")));

  const hist = await api("/api/sessions");
  const hs = $("#histSessions");
  if (!hist.length) hs.innerHTML = '<div class="empty">暂无历史会话</div>';
  else hs.innerHTML = `<table class="grid"><thead><tr><th>会话</th><th>机组</th><th>开始</th><th>时长</th>
    <th>耗油</th><th>状态</th><th></th></tr></thead><tbody>${hist.map(s =>
    `<tr><td>#${s.id}</td><td><b>${esc(s.unit_code)}</b></td>
     <td class="num">${esc(fmtDT(s.started_at))}</td><td class="num">${dur(s.duration_sec)}</td>
     <td class="num">${s.fuel_consumed_l != null ? fmtNum(s.fuel_consumed_l, 1) + " L" : "—"}</td>
     <td>${badge(s.status)}</td>
     <td class="right"><button class="btn sm ghost" data-sid="${s.id}">查看</button></td></tr>`).join("")}</tbody></table>`;
  hs.querySelectorAll("button[data-sid]").forEach(b => b.onclick = async () =>
    renderSession(await api(`/api/sessions/${b.dataset.sid}`)));
}

const METRIC_SPEC = {
  fuel: [{ key: "fuel_level", label: "燃油液位", unit: "%", min: 25, max: 100 }],
  battery: [{ key: "battery_voltage", label: "电池电压", unit: "V", min: 12.4, max: 13.8 }],
  no_load: [
    { key: "voltage", label: "空载电压", unit: "V", min: 390, max: 410 },
    { key: "frequency", label: "频率", unit: "Hz", min: 49.5, max: 50.5 },
  ],
  loaded: [
    { key: "voltage", label: "带载电压", unit: "V", min: 385, max: 415 },
    { key: "frequency", label: "频率", unit: "Hz", min: 49, max: 51 },
    { key: "load_kw", label: "负载", unit: "kW" },
  ],
};

async function renderSession(s) {
  state.runSession = s.id;
  setTimer(() => api(`/api/sessions/${s.id}`).then(renderSession).catch(() => {}), 1800);
  const readings = await api(`/api/units/${s.unit_id}/readings?session_id=${s.id}&limit=1`);
  const r = readings[0];
  const running = s.status === "running";
  const firstPending = (s.steps.find(x => x.status === "pending") || {}).step_code;
  const v = $("#view");
  v.innerHTML = `
    <div class="page-head"><h1>会话 #${s.id} · ${esc(s.unit_code)} ${esc(s.unit_name)}</h1>
      ${badge(s.status)}
      <span class="hint">开始 ${esc(fmtDT(s.started_at))} · 工况 ${({ normal: "正常", start_fail: "启动失败", leak: "漏油" })[s.sim_mode]}</span>
      <div style="flex:1"></div>
      <button class="btn ghost sm" id="backRun">‹ 返回列表</button>
    </div>
    <div id="sessSummary"></div>
    <div class="card" style="margin-bottom:14px"><div class="card-hd"><h2>检查步骤（按序执行）</h2>
      <div class="spacer"></div><div id="sessActions" class="btn-row"></div></div>
      <div class="card-bd"><div class="steps" id="stepList"></div></div></div>`;
  $("#backRun").onclick = () => { state.runSession = null; viewRun(); };

  // 会话摘要带
  if (!running) {
    $("#sessSummary").innerHTML = `<div class="cards-row">
      <div class="stat ${s.status === "done" ? "accent-green" : "accent-red"}">
        <div class="k">结果</div><div class="v" style="font-size:16px">${esc(STATUS_LABEL[s.status])}</div></div>
      <div class="stat"><div class="k">运行时长</div><div class="v" style="font-size:16px">${dur(s.duration_sec)}</div></div>
      <div class="stat"><div class="k">液位 ${fmtNum(s.fuel_start_pct, 1)}% → ${fmtNum(s.fuel_end_pct, 1)}%</div>
        <div class="v" style="font-size:16px">${s.fuel_consumed_l != null ? fmtNum(s.fuel_consumed_l, 1) : "—"} <small>L 实耗</small></div></div>
      <div class="stat accent-yellow"><div class="k">账面耗油 / 差异</div>
        <div class="v" style="font-size:16px">${s.fuel_burned_runtime_l != null ? fmtNum(s.fuel_burned_runtime_l, 1) : "—"}
        <small>L / ${s.fuel_discrepancy_l != null ? fmtNum(s.fuel_discrepancy_l, 1) : "—"} L</small></div></div>
    </div>
    ${s.summary ? `<div class="card"><div class="card-bd step-note">说明：${esc(s.summary)}</div></div>` : ""}`;
  }

  const list = $("#stepList");
  const specOf = code => STEP_DEFS.find(x => x.code === code);
  for (const st of s.steps) {
    const spec = specOf(st.step_code);
    const isActive = running && st.step_code === firstPending;
    const node = el("div", `step ${st.status === "pending" ? (isActive ? "active" : "") : st.status}`);
    node.innerHTML = `
      <div class="node">${st.status === "pass" ? ICON.check : spec.order}</div>
      <div class="step-body">
        <div class="step-hd"><h3>${spec.order}. ${esc(spec.name)}</h3>
          <span>${badge(st.status === "pending" ? (isActive ? "pending" : "draft") : st.status)}</span>
          <div class="spacer"></div>
          <span class="muted" style="font-size:11.5px">${st.completed_at ? esc(fmtTime(st.completed_at)) : ""}</span>
        </div>
        <div class="step-desc">${esc(stepDesc(st.step_code))}</div>
        <div class="step-metrics" data-metrics></div>
        <div class="step-ft" data-foot></div>
      </div>`;
    const mWrap = node.querySelector("[data-metrics]");
    const foot = node.querySelector("[data-foot]");

    // 实时数值 vs 合格区间
    const specs = METRIC_SPEC[st.step_code] || [];
    for (const m of specs) {
      const val = r ? r[m.key] : null;
      const hasRange = m.min !== undefined;
      const inRange = val !== null && (!hasRange || (val >= m.min && val <= m.max));
      const tile = el("div", "metric-tile");
      tile.innerHTML = `
        <span class="ml">${esc(m.label)}</span>
        <span class="mr">合格区间</span>
        <span class="mv ${val == null ? "wait" : hasRange ? (inRange ? "ok" : "bad") : "ok"}">
          ${val == null ? "等待数据" : fmtNum(val, m.unit === "Hz" ? 2 : 1)}</span>
        <span class="range">${hasRange ? `[${m.min} ~ ${m.max}] ${m.unit}` : m.unit}</span>`;
      mWrap.appendChild(tile);
    }
    if (st.step_code === "visual") {
      mWrap.innerHTML += `<div class="metric-tile" style="grid-column:1/-1">
        <span class="ml">检查项</span><span class="mr">人工确认</span>
        <span class="mv" style="font-size:13px;font-family:inherit">外观 / 排烟 / 接地 / 渗漏痕迹</span><span class="range">逐项目检</span></div>`;
    }

    if (st.status !== "pending") {
      if (st.fail_reason) foot.innerHTML = `<span class="step-fail">${ICON.warn} ${esc(st.fail_reason)}</span>`;
      if (st.note) foot.innerHTML += `<span class="step-note">${esc(st.note)}</span>`;
      if (st.status === "skipped") foot.innerHTML += `<span class="step-note">🔒 ${esc(st.note || "已凭主管批准越过")}</span>`;
      if (st.status === "pass" && st.measured_json && st.measured_json !== "{}")
        foot.innerHTML += `<span class="step-note muted">记录值 <code>${esc(st.measured_json)}</code></span>`;
    } else if (isActive) {
      renderStepActions(foot, s, st, r);
    } else if (running) {
      foot.innerHTML = `<span class="muted" style="font-size:12px">等待前置步骤完成</span>`;
    }
    list.appendChild(node);
  }

  // 会话级操作
  const acts = $("#sessActions");
  if (running) {
    const pendingCnt = s.steps.filter(x => x.status === "pending").length;
    const finish = el("button", "btn sm green", "✓ 全部合格，结束并核算燃油");
    finish.disabled = pendingCnt > 0;
    finish.title = pendingCnt ? "尚有未完成步骤" : "";
    finish.onclick = () => api(`/api/sessions/${s.id}/finish`, { method: "POST" })
      .then(x => { toast("机组回到待命，燃油已核算"); renderSession(x); })
      .catch(e => toast(e.message, "err"));
    acts.appendChild(finish);
    const noise = el("button", "btn sm danger", "⚠ 异常噪声停机");
    noise.onclick = () => incidentDialog(s, "noise");
    const leak = el("button", "btn sm danger", "⛽ 漏油停机");
    leak.onclick = () => incidentDialog(s, "leak");
    acts.append(noise, leak);
    const abort = el("button", "btn sm ghost", "中止会话");
    abort.onclick = () => {
      const reason = prompt("中止原因");
      if (reason) api(`/api/sessions/${s.id}/abort`, { method: "POST", body: { reason } })
        .then(x => renderSession(x)).catch(e => toast(e.message, "err"));
    };
    acts.appendChild(abort);
  }
  const trend = el("button", "btn sm ghost", "参数趋势");
  trend.onclick = () => { state.trendUnit = s.unit_id; state.trendSession = String(s.id); location.hash = "#/trends"; };
  acts.appendChild(trend);
}

function stepDesc(code) {
  return {
    visual: "机组外观、排烟、接地与周边环境，确认无渗漏痕迹。",
    fuel: "核对柴油液位，低于 25% 不得继续带载测试。",
    battery: "测量启动电池端电压，低于 12.4V 判为亏电。",
    no_load: "空载启动并稳定至额定转速，核对电压 390–410V、频率 49.5–50.5Hz。",
    loaded: "分级加载至额定负载约 80%，核对带载电压 385–415V、频率 49–51Hz。",
  }[code];
}

function renderStepActions(foot, s, st, r) {
  const post = (body) => api(`/api/sessions/${s.id}/steps/${st.step_code}`, { method: "POST", body });
  const refresh = () => api(`/api/sessions/${s.id}`).then(renderSession);

  if (st.step_code === "visual") {
    foot.innerHTML = `<label class="confirm-box"><input type="checkbox" id="vc">
      <span>我已逐项确认外观、排烟、接地正常，无渗漏痕迹</span></label>`;
    const ok = el("button", "btn sm green", "确认合格");
    ok.onclick = () => {
      if (!foot.querySelector("#vc").checked) return toast("请先勾选逐项确认", "err");
      post({ visual_confirmed: true }).then(refresh).catch(e => {
        toast(e.message, "err"); refresh();
      });
    };
    const bad = el("button", "btn sm danger", "报告外观异常");
    bad.onclick = () => {
      const reason = prompt("描述外观异常（将中止本会话）");
      if (reason) post({ fail_reason: reason }).then(refresh).catch(e => toast(e.message, "err"));
    };
    foot.append(ok, bad, skipBtn(s, st.step_code, refresh));
    return;
  }

  const ok = el("button", "btn sm green", "记录为合格，进入下一步");
  ok.onclick = () => post({}).then(refresh).catch(e => { toast(e.message, "err"); refresh(); });
  foot.appendChild(ok);
  const skip = skipBtn(s, st.step_code, refresh);
  foot.appendChild(skip);
  foot.insertAdjacentHTML("beforeend",
    `<span class="muted" style="font-size:11.5px">数值不在区间时提交将自动判失败并停机/中止</span>`);
}

function skipBtn(s, stepCode, refresh) {
  const b = el("button", "btn sm ghost", "申请越过（主管批准）");
  b.onclick = () => {
    const reason = prompt(`申请越过「${STEP_NAME[stepCode]}」的原因`);
    if (!reason) return;
    api("/api/skips", { method: "POST", body: { session_id: s.id, step_code: stepCode, reason } })
      .then(() => toast("已提交越步申请，等待工程主管批准（可在审批记录页处理）"))
      .catch(e => toast(e.message, "err"));
  };
  return b;
}

function incidentDialog(s, kind) {
  const isLeak = kind === "leak";
  const box = modal(isLeak ? "漏油 · 紧急停机" : "异常噪声 · 紧急停机",
    `<label class="fld">现场描述<textarea id="incNote" rows="3"
       placeholder="${isLeak ? "渗漏部位、油迹范围…" : "噪声部位、工况、异响特征…"}"></textarea></label>
      <p style="margin-top:10px"><label class="fld">现场附件（照片/录音，可选）
        <input type="file" id="incFile"></label></p>
      <p class="muted" style="font-size:12px">提交后机组立即故障停机并全程留痕，附件存入本地 attachments/。</p>`,
    `<button class="btn ghost" data-close>取消</button>
     <button class="btn danger" id="incOk">确认${isLeak ? "漏油" : "异常噪声"}并停机</button>`);
  box.querySelector("#incOk").onclick = async () => {
    const note = box.querySelector("#incNote").value.trim();
    if (!note) return toast("请填写现场描述", "err");
    let attachmentId = null;
    const f = box.querySelector("#incFile").files[0];
    try {
      if (f) {
        const buf = await f.arrayBuffer();
        const q = new URLSearchParams({ unit_id: s.unit_id, session_id: s.id,
          kind, filename: f.name }).toString();
        const up = await fetch(`/api/attachments?${q}`, {
          method: "POST", headers: { "X-User-Id": state.uid }, body: buf });
        const data = await up.json();
        if (!up.ok) throw new Error(data.message);
        attachmentId = data.id;
      }
      const x = await api(`/api/sessions/${s.id}/incidents`, {
        method: "POST", body: { kind, note, attachment_id: attachmentId } });
      closeModal(); toast("已故障停机并留痕"); renderSession(x);
    } catch (e) { toast(e.message, "err"); }
  };
}

// ============================================================
// 视图：参数趋势
// ============================================================
async function viewTrends() {
  setTimer(viewTrends, 4000);
  const data = await api("/api/overview");
  if (!state.trendUnit) state.trendUnit = data.units[0]?.id;
  const sessions = await api(`/api/sessions?unit_id=${state.trendUnit}`);
  if (state.trendSession === undefined)
    state.trendSession = sessions[0] ? String(sessions[0].id) : "";
  const v = $("#view");
  v.innerHTML = `
    <div class="page-head"><h1>参数趋势</h1>
      <span class="hint">电压 / 频率 / 液位分轴展示（非仪表盘）· 红点=越限读数，三角=漏油标记</span>
      <div style="flex:1"></div>
      <label class="fld" style="flex-direction:row;align-items:center;gap:8px">机组
        <select id="trUnit">${data.units.map(u =>
          `<option value="${u.id}" ${u.id === state.trendUnit ? "selected" : ""}>${esc(u.code)}</option>`).join("")}
        </select></label>
      <label class="fld" style="flex-direction:row;align-items:center;gap:8px">会话
        <select id="trSession"><option value="">最近读数</option>${sessions.map(s =>
          `<option value="${s.id}" ${String(s.id) === state.trendSession ? "selected" : ""}>#${s.id} ${esc(STATUS_LABEL[s.status])}</option>`).join("")}
        </select></label>
      <button class="btn ghost sm" id="trTableBtn">表格视图</button>
    </div>
    <div class="charts" id="charts"></div>
    <div class="card" id="tableCard" hidden><div class="card-hd"><h2>读数表</h2></div>
      <div style="max-height:340px;overflow:auto" id="trTable"></div></div>`;
  $("#trUnit").onchange = e => { state.trendUnit = Number(e.target.value); state.trendSession = null; viewTrends(); };
  $("#trSession").onchange = e => { state.trendSession = e.target.value; viewTrends(); };
  $("#trTableBtn").onclick = () => {
    const c = $("#tableCard"); c.hidden = !c.hidden;
    $("#trTableBtn").textContent = c.hidden ? "表格视图" : "收起表格";
  };

  const q = `?limit=120${state.trendSession ? `&session_id=${state.trendSession}` : ""}`;
  const rows = await api(`/api/units/${state.trendUnit}/readings${q}`);
  const charts = [
    { key: "voltage", title: "电压", unit: "V", cls: "v", min: 385, max: 415,
      refMin: 390, refMax: 410, decimals: 1 },
    { key: "frequency", title: "频率", unit: "Hz", cls: "f", min: 48.5, max: 51.5,
      refMin: 49.5, refMax: 50.5, decimals: 2 },
    { key: "fuel_level", title: "燃油液位", unit: "%", cls: "l", min: 0, max: 100,
      refMin: 25, refMax: 100, decimals: 1, lowLine: 25 },
  ];
  $("#charts").innerHTML = charts.map(c =>
    `<div class="card chart-card"><div class="card-bd">
      <div class="chart-title"><span class="sw ${c.cls}"></span>${c.title}
        <span class="chart-sub">单位 ${c.unit}</span></div>
      <div id="chart_${c.key}"></div></div></div>`).join("");
  charts.forEach(c => drawChart(`#chart_${c.key}`, rows, c));

  $("#trTable").innerHTML = `<table class="grid"><thead><tr>
    <th>时间</th><th>电压 V</th><th>频率 Hz</th><th>负载 kW</th><th>液位 %</th><th>电池 V</th><th>标记</th>
    </tr></thead><tbody>${[...rows].reverse().map(r => `<tr>
      <td class="num">${esc(fmtTime(r.ts))}</td><td class="num">${fmtNum(r.voltage, 1)}</td>
      <td class="num">${fmtNum(r.frequency, 2)}</td><td class="num">${fmtNum(r.load_kw, 1)}</td>
      <td class="num">${fmtNum(r.fuel_level, 1)}</td><td class="num">${fmtNum(r.battery_voltage, 2)}</td>
      <td>${r.flags ? `<span class="badge sev-critical flag-chip">${esc(r.flags)}</span>` : ""}</td>
    </tr>`).join("")}</tbody></table>`;
}

function drawChart(sel, rows, cfg) {
  const W = 520, H = 170, P = { l: 44, r: 12, t: 12, b: 22 };
  const vals = rows.map(r => r[cfg.key]).filter(v => v !== null && v !== undefined);
  const host = $(sel);
  if (!vals.length) { host.innerHTML = '<div class="empty">暂无读数</div>'; return; }
  const lo = Math.min(cfg.min, Math.min(...vals)) - 2;
  const hi = Math.max(cfg.max, Math.max(...vals)) + 2;
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const X = i => P.l + (rows.length <= 1 ? iw / 2 : i * iw / (rows.length - 1));
  const Y = v => P.t + ih - (v - lo) / (hi - lo) * ih;

  const inRef = v => v >= cfg.refMin && v <= cfg.refMax;
  let path = "", pts = [];
  rows.forEach((r, i) => {
    const v = r[cfg.key];
    if (v === null || v === undefined) return;
    path += `${pts.length ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)} `;
    pts.push({ i, v, r });
  });
  const bandY1 = Y(cfg.refMax), bandY2 = Y(cfg.refMin);
  const ticks = 4;
  let grid = "";
  for (let t = 0; t <= ticks; t++) {
    const val = lo + (hi - lo) * t / ticks, yy = Y(val);
    grid += `<line x1="${P.l}" y1="${yy}" x2="${W - P.r}" y2="${yy}" stroke="#E9EDE9" stroke-width="1"/>
      <text x="${P.l - 6}" y="${yy + 3.5}" text-anchor="end" font-size="10" fill="#88909A" font-family="ui-monospace">${val.toFixed(cfg.decimals)}</text>`;
  }
  const marks = pts.filter(p => !inRef(p.v) && p.v > (cfg.key === "voltage" ? 50 : 0)
    || p.r.flags && p.r.flags.includes("leak")).map(p => {
      const leak = p.r.flags && p.r.flags.includes("leak");
      if (leak)
        return `<path d="M${X(p.i)},${Y(p.v) - 6} L${X(p.i) - 5},${Y(p.v) + 3} L${X(p.i) + 5},${Y(p.v) + 3} Z" fill="#C62828"><title>${esc(fmtTime(p.r.ts))} ${p.v.toFixed(2)}${cfg.unit} · 漏油</title></path>`;
      return `<circle cx="${X(p.i)}" cy="${Y(p.v)}" r="3.2" fill="#C62828" stroke="#fff" stroke-width="1.2"><title>${esc(fmtTime(p.r.ts))} ${p.v.toFixed(2)}${cfg.unit} · 越限</title></circle>`;
    }).join("");

  host.innerHTML = `
    <svg class="trend" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <defs><clipPath id="clip_${cfg.key}"><rect x="${P.l}" y="${P.t}" width="${iw}" height="${ih}"/></clipPath></defs>
      ${grid}
      <rect x="${P.l}" y="${bandY1}" width="${iw}" height="${Math.max(0, bandY2 - bandY1)}" fill="#2F9E44" opacity="0.08"/>
      <line x1="${P.l}" y1="${bandY1}" x2="${W - P.r}" y2="${bandY1}" stroke="#2F9E44" stroke-width="1" stroke-dasharray="4 3" opacity=".7"/>
      <line x1="${P.l}" y1="${bandY2}" x2="${W - P.r}" y2="${bandY2}" stroke="#2F9E44" stroke-width="1" stroke-dasharray="4 3" opacity=".7"/>
      <path d="${path}" fill="none" stroke="${cfg.cls === "v" ? "#3A434A" : cfg.cls === "f" ? "#9A6E12" : "#2F9E44"}"
        stroke-width="2" stroke-linejoin="round" stroke-linecap="round" clip-path="url(#clip_${cfg.key})"/>
      <g>${marks}</g>
      <line x1="${P.l}" y1="${H - P.b}" x2="${W - P.r}" y2="${H - P.b}" stroke="#C6CDD2"/>
      <text x="${P.l}" y="${H - 6}" font-size="10" fill="#88909A">${rows.length ? esc(fmtTime(rows[0].ts)) : ""}</text>
      <text x="${W - P.r}" y="${H - 6}" text-anchor="end" font-size="10" fill="#88909A">${rows.length ? esc(fmtTime(rows[rows.length - 1].ts)) : ""}</text>
      <line id="cx_${cfg.key}" x1="0" y1="${P.t}" x2="0" y2="${H - P.b}" stroke="#3A434A" stroke-width="1" opacity="0" stroke-dasharray="3 3"/>
    </svg>
    <div id="tip_${cfg.key}" style="font-size:11.5px;min-height:16px;color:#444C53;font-family:ui-monospace"></div>`;

  // hover 十字线 + 数值
  const svg = host.querySelector("svg");
  const cx = host.querySelector(`#cx_${cfg.key}`);
  const tip = host.querySelector(`#tip_${cfg.key}`);
  svg.addEventListener("mousemove", e => {
    const rect = svg.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width * W;
    if (x < P.l || x > W - P.r || !pts.length) { cx.setAttribute("opacity", "0"); tip.textContent = ""; return; }
    const idx = Math.round((x - P.l) / iw * (rows.length - 1));
    const p = pts.reduce((a, b) => Math.abs(b.i - idx) < Math.abs(a.i - idx) ? b : a);
    cx.setAttribute("x1", X(p.i)); cx.setAttribute("x2", X(p.i));
    cx.setAttribute("opacity", ".5");
    tip.textContent = `${fmtDT(p.r.ts)}  ${p.v.toFixed(cfg.decimals)} ${cfg.unit}` +
      (p.r.flags ? `  [${p.r.flags}]` : "") + (!inRef(p.v) && p.v > 50 ? "  越限" : "");
  });
  svg.addEventListener("mouseleave", () => { cx.setAttribute("opacity", "0"); tip.textContent = ""; });
}

// ============================================================
// 视图：故障停机
// ============================================================
const FAULT_TYPES = ["start_failure", "voltage_out_of_range", "abnormal_noise",
  "oil_leak", "load_interrupted", "fuel_discrepancy", "fault_shutdown",
  "session_blocked", "plan_cancelled", "session_aborted"];
async function viewFaults() {
  setTimer(viewFaults, 6000);
  const events = (await api("/api/events?limit=300"))
    .filter(e => FAULT_TYPES.includes(e.type) || e.severity === "critical");
  const sessions = await api("/api/sessions");
  const v = $("#view");
  v.innerHTML = `
    <div class="page-head"><h1>故障停机与异常留痕</h1>
      <span class="hint">启动失败 / 电压越限 / 异常噪声 / 漏油 / 带载中断 / 燃油差异 / 计划取消</span></div>
    <div class="cards-row" id="faultStats"></div>
    <div class="card"><div class="card-hd"><h2>异常会话</h2></div><div id="faultSessions"></div></div>
    <div class="card"><div class="card-hd"><h2>事件留痕</h2></div>
      <div class="card-bd" id="faultEvents"></div></div>`;
  const faultS = sessions.filter(s => s.status === "failed");
  const abortS = sessions.filter(s => s.status === "aborted");
  const disc = events.filter(e => e.type === "fuel_discrepancy").length;
  $("#faultStats").innerHTML = [
    ["故障停机", faultS.length, "accent-red"],
    ["检查中止", abortS.length, "accent-yellow"],
    ["燃油差异", disc, "accent-yellow"],
    ["留痕事件", events.length, "accent-gray"],
  ].map(([k, n, c]) => `<div class="stat ${c}"><div class="k">${k}</div><div class="v">${n}</div></div>`).join("");

  $("#faultSessions").innerHTML = faultS.length + abortS.length ? `<table class="grid"><thead><tr>
    <th>会话</th><th>机组</th><th>开始/结束</th><th>结果</th><th>原因</th><th></th></tr></thead><tbody>
    ${[...faultS, ...abortS].map(s => `<tr>
      <td>#${s.id}</td><td><b>${esc(s.unit_code)}</b></td>
      <td class="num">${esc(fmtDT(s.started_at))}<br>${esc(fmtTime(s.ended_at))}</td>
      <td>${badge(s.status)}</td><td>${esc(s.summary || "—")}</td>
      <td class="right"><button class="btn sm ghost" data-sid="${s.id}">步骤详情</button></td></tr>`).join("")}
    </tbody></table>` : '<div class="empty">暂无异常会话</div>';
  $("#faultSessions").querySelectorAll("button[data-sid]").forEach(b =>
    b.onclick = async () => {
      state.runSession = Number(b.dataset.sid); location.hash = "#/run";
    });

  $("#faultEvents").innerHTML = events.length ? events.map(e => `
    <div class="timeline-ev">
      <div class="ev-time">${esc(fmtDT(e.ts))}</div>
      <div class="ev-body">
        <div class="ev-title">
          <span class="badge sev-${esc(e.severity)}">${esc(EVENT_LABEL[e.type] || e.type)}</span>
          ${e.unit_id ? `<span class="muted">#${e.unit_id}</span>` : ""}
          ${e.session_id ? `<span class="muted">会话 #${e.session_id}</span>` : ""}
          ${e.step_code ? `<span class="muted">${esc(STEP_NAME[e.step_code] || e.step_code)}</span>` : ""}
        </div>
        <div class="ev-detail">${esc(JSON.stringify(e.detail, null, 0))}</div>
        <div class="ev-actor">${esc(e.actor_name || "系统")}</div>
      </div>
    </div>`).join("") : '<div class="empty">暂无异常事件</div>';
}

// ============================================================
// 视图：燃油台账
// ============================================================
async function viewFuel() {
  setTimer(viewFuel, 5000);
  const data = await api("/api/overview");
  if (!state.fuelUnit) state.fuelUnit = data.units[0]?.id;
  const uid = state.fuelUnit;
  const [fuelings, bal] = await Promise.all([
    api("/api/fuelings"), api(`/api/units/${uid}/fuel-balance`)]);
  const v = $("#view");
  v.innerHTML = `
    <div class="page-head"><h1>燃油台账</h1>
      <span class="hint">加油数量修订须第二人复核；试运行结束按液位差核算实耗并与账面耗油比对</span>
      <div style="flex:1"></div>
      <select id="fuelUnit">${data.units.map(u =>
        `<option value="${u.id}" ${u.id === uid ? "selected" : ""}>${esc(u.code)}</option>`).join("")}</select>
      <button class="btn yellow sm" id="addFuel">+ 加油登记</button>
    </div>
    <div class="cards-row">
      <div class="stat accent-yellow"><div class="k">当前存油</div>
        <div class="v">${fmtNum(bal.current_l, 1)} <small>L / ${fmtNum(bal.capacity_l, 0)} L</small></div></div>
      <div class="stat"><div class="k">当前液位</div><div class="v">${fmtNum(bal.current_pct, 1)} <small>%</small></div></div>
      <div class="stat accent-green"><div class="k">累计补入（已生效）</div><div class="v">${fmtNum(bal.added_l, 1)} <small>L</small></div></div>
      <div class="stat accent-red"><div class="k">试运行实耗</div><div class="v">${fmtNum(bal.consumed_l, 1)} <small>L</small></div></div>
    </div>
    <div class="card"><div class="card-hd"><h2>加油 / 复核记录</h2></div><div id="fuelTable"></div></div>
    <div class="card"><div class="card-hd"><h2>本机组燃油相关事件</h2></div>
      <div class="card-bd" id="fuelEvents"></div></div>`;
  $("#fuelUnit").onchange = e => { state.fuelUnit = Number(e.target.value); viewFuel(); };
  $("#addFuel").onclick = () => fuelDialog(uid);

  const rows = fuelings;
  $("#fuelTable").innerHTML = rows.length ? `<table class="grid"><thead><tr>
    <th>时间</th><th>机组</th><th>数量 L</th><th>液位前→后</th><th>登记人</th>
    <th>状态 / 复核</th><th>说明</th><th></th></tr></thead><tbody>
    ${rows.map(f => `<tr data-fid="${f.id}">
      <td class="num">${esc(fmtDT(f.ts))}</td><td><b>${esc(f.unit_code)}</b></td>
      <td class="num">${fmtNum(f.liters, 1)}</td>
      <td class="num">${fmtNum(f.level_before, 1)}% → ${fmtNum(f.level_after, 1)}%</td>
      <td>${esc(f.recorded_name || "—")}</td>
      <td>${fuelStatusBadge(f)}</td>
      <td>${esc(f.note || "")}${f.supersedes ? `<br><span class="muted">修订自 #${f.supersedes}</span>` : ""}</td>
      <td class="right" data-acts></td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">暂无加油记录</div>';

  $("#fuelTable").querySelectorAll("tr[data-fid]").forEach(tr => {
    const f = rows.find(x => x.id == tr.dataset.fid);
    const cell = tr.querySelector("[data-acts]");
    if (f.review_status === "approved") {
      const b = el("button", "btn sm ghost", "修订数量");
      b.onclick = () => reviseDialog(f); cell.appendChild(b);
    } else if (f.review_status === "pending") {
      const b = el("button", "btn sm yellow", "第二人复核");
      b.disabled = f.recorded_by === state.uid;
      b.title = f.recorded_by === state.uid ? "提交人不能自审，请切换身份" : "";
      b.onclick = () => reviewDialog(f);
      cell.appendChild(b);
    }
  });

  const events = (await api(`/api/units/${uid}/events?limit=50`))
    .filter(e => ["fueling_created", "fueling_revised", "fueling_reviewed",
      "fuel_discrepancy", "session_done"].includes(e.type));
  $("#fuelEvents").innerHTML = events.length ? events.map(e => `
    <div class="timeline-ev"><div class="ev-time">${esc(fmtDT(e.ts))}</div>
      <div class="ev-body"><div class="ev-title">
        <span class="badge sev-${esc(e.severity)}">${esc(EVENT_LABEL[e.type] || e.type)}</span></div>
        <div class="ev-detail">${esc(JSON.stringify(e.detail))}</div>
        <div class="ev-actor">${esc(e.actor_name || "系统")}</div></div></div>`).join("")
    : '<div class="empty">暂无燃油事件</div>';
}

function fuelStatusBadge(f) {
  const map = { approved: ["approved", "已生效"], pending: ["pending", "待第二人复核"],
    revised: ["aborted", "已被修订"], denied: ["cancelled", "复核驳回"], void: ["cancelled", "已冲销"] };
  const [cls, label] = map[f.review_status] || ["draft", f.review_status];
  return `${badge(cls, label)}${f.reviewed_name ? `<div class="muted" style="font-size:11px;margin-top:2px">复核：${esc(f.reviewed_name)}</div>` : ""}`;
}

function fuelDialog(unitId) {
  const box = modal("加油登记",
    `<div class="form-grid">
      <label class="fld">加油数量（L）<input type="number" id="fLiters" min="1" step="0.1"></label>
     </div><p style="margin-top:10px"><label class="fld">备注<textarea id="fNote" rows="2"></textarea></label></p>
     <p class="muted" style="font-size:12px">液位按油箱容量自动折算；加油员/主管身份可登记。</p>`,
    `<button class="btn ghost" data-close>取消</button><button class="btn yellow" id="fOk">登记</button>`);
  box.querySelector("#fOk").onclick = () => {
    const liters = Number(box.querySelector("#fLiters").value);
    if (!(liters > 0)) return toast("请输入正数升数", "err");
    api("/api/fuelings", { method: "POST", body: { unit_id: unitId, liters, note: box.querySelector("#fNote").value } })
      .then(() => { closeModal(); toast("加油已登记"); viewFuel(); })
      .catch(e => toast(e.message, "err"));
  };
}

function reviseDialog(f) {
  const box = modal(`修订加油记录 #${f.id}`,
    `<p>原数量：<b class="num">${fmtNum(f.liters, 1)} L</b>（${esc(f.unit_code)}）。
       修订将生成待复核记录，<b>经第二人复核后才改动液位</b>。</p>
       <label class="fld">修订后数量（L）<input type="number" id="rLiters" value="${f.liters}" step="0.1"></label>
       <p style="margin-top:10px"><label class="fld">原因<textarea id="rNote" rows="2"></textarea></label></p>`,
    `<button class="btn ghost" data-close>取消</button>
     <button class="btn yellow" id="rOk">提交修订（待复核）</button>`);
  box.querySelector("#rOk").onclick = () => {
    const n = Number(box.querySelector("#rLiters").value);
    if (!(n > 0)) return toast("请输入正数", "err");
    api(`/api/fuelings/${f.id}/revise`, { method: "POST", body: { liters: n, note: box.querySelector("#rNote").value } })
      .then(() => { closeModal(); toast("修订已提交，等待第二人复核"); viewFuel(); })
      .catch(e => toast(e.message, "err"));
  };
}

function reviewDialog(f) {
  const box = modal(`第二人复核 · 修订 #${f.id}`,
    `<dl class="kv">
      <dt>机组</dt><dd>${esc(f.unit_code)}</dd>
      <dt>修订自</dt><dd>原加油记录 #${f.supersedes}</dd>
      <dt>修订数量</dt><dd class="num"><b>${fmtNum(f.liters, 1)} L</b></dd>
      <dt>提交人</dt><dd>${esc(f.recorded_name || f.recorded_by)}</dd>
      <dt>说明</dt><dd>${esc(f.note || "—")}</dd>
     </dl>
     <p class="muted" style="font-size:12px">提交人本人不能复核；批准后冲销原记录并按新数量重算液位。</p>
     <p><label class="fld">复核意见<textarea id="rvNote" rows="2"></textarea></label></p>`,
    `<button class="btn ghost" data-close>取消</button>
     <button class="btn danger" id="rvNo">驳回</button>
     <button class="btn green" id="rvOk">复核通过</button>`);
  box.querySelector("#rvOk").onclick = () => doReview(f, true, box);
  box.querySelector("#rvNo").onclick = () => doReview(f, false, box);
}
function doReview(f, approve, box) {
  api(`/api/fuelings/${f.id}/review`, { method: "POST", body: { approve, note: box.querySelector("#rvNote").value } })
    .then(() => { closeModal(); toast(approve ? "复核通过，台账已更新" : "已驳回修订"); viewFuel(); })
    .catch(e => toast(e.message, "err"));
}

// ============================================================
// 视图：审批记录
// ============================================================
async function viewApprovals() {
  setTimer(viewApprovals, 4000);
  const [q, users] = await Promise.all([api("/api/approvals"), api("/api/users")]);
  const events = (await api("/api/events?limit=200")).filter(e =>
    ["plan_submitted", "plan_approved", "plan_rejected", "skip_requested",
      "skip_approved", "skip_denied", "fueling_revised", "fueling_reviewed",
      "step_skipped"].includes(e.type));
  const v = $("#view");
  v.innerHTML = `
    <div class="page-head"><h1>审批记录</h1>
      <span class="hint">计划批准 / 越步批准限工程主管；加油修订限第二人复核</span>
      <div style="flex:1"></div>
      <div class="pill-tabs" id="apTabs">
        <button class="active" data-t="queue">待办 ${q.plans.length + q.skips.length + q.fueling_revisions.length}</button>
        <button data-t="log">审批留痕</button>
      </div></div>
    <div id="apQueue"></div><div id="apLog" hidden></div>`;

  const queue = $("#apQueue");
  queue.innerHTML = `
    <div class="card"><div class="card-hd"><h2>试运行计划（${q.plans.length}）</h2></div>
      <div id="qPlans"></div></div>
    <div class="card"><div class="card-hd"><h2>越过检查步骤申请（${q.skips.length}）</h2></div>
      <div id="qSkips"></div></div>
    <div class="card"><div class="card-hd"><h2>加油数量修订复核（${q.fueling_revisions.length}）</h2></div>
      <div id="qFuel"></div></div>`;

  $("#qPlans").innerHTML = q.plans.length ? `<table class="grid"><thead><tr>
    <th>提交时间</th><th>计划时间</th><th>机组</th><th>说明</th><th class="right">操作</th></tr></thead><tbody>
    ${q.plans.map(p => `<tr><td class="num">${esc(fmtDT(p.submitted_at))}</td>
      <td class="num">${esc(fmtDT(p.scheduled_at))}</td><td><b>${esc(p.unit_code)}</b></td>
      <td>${esc(p.note || "")}</td>
      <td class="right"><button class="btn sm green" data-ap="${p.id}">批准</button>
      <button class="btn sm danger" data-rj="${p.id}">驳回</button></td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">无待批计划</div>';
  queue.querySelectorAll("[data-ap]").forEach(b => b.onclick = () =>
    api(`/api/plans/${b.dataset.ap}/review`, { method: "POST", body: { approve: true } })
      .then(() => { toast("计划已批准"); viewApprovals(); }).catch(e => toast(e.message, "err")));
  queue.querySelectorAll("[data-rj]").forEach(b => b.onclick = () =>
    api(`/api/plans/${b.dataset.rj}/review`, { method: "POST", body: { approve: false } })
      .then(() => { toast("已驳回"); viewApprovals(); }).catch(e => toast(e.message, "err")));

  $("#qSkips").innerHTML = q.skips.length ? `<table class="grid"><thead><tr>
    <th>申请时间</th><th>会话</th><th>越过步骤</th><th>申请人</th><th>原因</th><th class="right">操作</th></tr></thead><tbody>
    ${q.skips.map(r => `<tr><td class="num">${esc(fmtDT(r.created_at))}</td>
      <td>#${r.session_id}</td><td>${r.step_order ? "" : ""}${esc(STEP_NAME[r.step_code] || r.step_code)}</td>
      <td>${esc(r.requester_name || "")}</td><td>${esc(r.reason)}</td>
      <td class="right"><button class="btn sm green" data-sa="${r.id}">主管批准</button>
      <button class="btn sm danger" data-sd="${r.id}">驳回</button></td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">无越步申请</div>';
  queue.querySelectorAll("[data-sa]").forEach(b => b.onclick = () =>
    api(`/api/skips/${b.dataset.sa}/review`, { method: "POST", body: { approve: true } })
      .then(() => { toast("越步已批准"); viewApprovals(); }).catch(e => toast(e.message, "err")));
  queue.querySelectorAll("[data-sd]").forEach(b => b.onclick = () =>
    api(`/api/skips/${b.dataset.sd}/review`, { method: "POST", body: { approve: false } })
      .then(() => { toast("已驳回"); viewApprovals(); }).catch(e => toast(e.message, "err")));

  $("#qFuel").innerHTML = q.fueling_revisions.length ? `<table class="grid"><thead><tr>
    <th>提交时间</th><th>机组</th><th>修订数量</th><th>提交人</th><th>说明</th><th class="right">操作</th></tr></thead><tbody>
    ${q.fueling_revisions.map(f => `<tr><td class="num">${esc(fmtDT(f.ts))}</td>
      <td><b>${esc(f.unit_code)}</b></td><td class="num">${fmtNum(f.liters, 1)} L</td>
      <td>${esc(f.recorded_name || "")}</td><td>${esc(f.note || "")}</td>
      <td class="right"><button class="btn sm ${f.recorded_by === state.uid ? "" : "yellow"}"
        data-fa="${f.id}" ${f.recorded_by === state.uid ? "disabled title='不能自审'" : ""}>第二人复核通过</button>
      <button class="btn sm danger" data-fd="${f.id}" ${f.recorded_by === state.uid ? "disabled" : ""}>驳回</button></td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">无待复核修订</div>';
  queue.querySelectorAll("[data-fa]").forEach(b => b.onclick = () =>
    api(`/api/fuelings/${b.dataset.fa}/review`, { method: "POST", body: { approve: true } })
      .then(() => { toast("复核通过"); viewApprovals(); }).catch(e => toast(e.message, "err")));
  queue.querySelectorAll("[data-fd]").forEach(b => b.onclick = () =>
    api(`/api/fuelings/${b.dataset.fd}/review`, { method: "POST", body: { approve: false } })
      .then(() => { toast("已驳回"); viewApprovals(); }).catch(e => toast(e.message, "err")));

  $("#apLog").innerHTML = `<div class="card"><div class="card-bd">${events.map(e => `
    <div class="timeline-ev"><div class="ev-time">${esc(fmtDT(e.ts))}</div>
      <div class="ev-body"><div class="ev-title">
        <span class="badge sev-${esc(e.severity)}">${esc(EVENT_LABEL[e.type] || e.type)}</span>
        ${e.unit_id ? `<span class="muted">机组 #${e.unit_id}</span>` : ""}
        ${e.session_id ? `<span class="muted">会话 #${e.session_id}</span>` : ""}
      </div><div class="ev-detail">${esc(JSON.stringify(e.detail))}</div>
      <div class="ev-actor">${esc(e.actor_name || "系统")}</div></div></div>`).join("") || '<div class="empty">暂无审批留痕</div>'}</div></div>`;

  $("#apTabs").querySelectorAll("button").forEach(b => b.onclick = () => {
    $("#apTabs").querySelectorAll("button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    const isQ = b.dataset.t === "queue";
    $("#apQueue").hidden = !isQ; $("#apLog").hidden = isQ;
  });
}

// ============================================================
// 提醒 / 身份 / 路由
// ============================================================
async function refreshAlerts() {
  const alerts = await api("/api/alerts").catch(() => []);
  const bar = $("#alertBar");
  $("#bellDot").hidden = !alerts.length;
  bar.hidden = !alerts.length;
  bar.innerHTML = "";
  for (const a of alerts.slice(0, 4)) {
    const item = el("div", `alert-item ${a.severity === "critical" ? "critical" : "warning"}`);
    item.innerHTML = `${a.kind === "low_fuel" ? ICON.fuel : ICON.warn}
      <span>${esc(a.message)}</span><span class="muted">${esc(fmtTime(a.ts))}</span>
      <button class="btn sm ghost">知道了</button>`;
    item.querySelector("button").onclick = () =>
      api(`/api/alerts/${a.id}/ack`, { method: "POST" }).then(refreshAlerts);
    bar.appendChild(item);
  }
}

async function initIdentity() {
  state.users = await api("/api/users");
  const sel = $("#userSelect");
  sel.innerHTML = state.users.map(u =>
    `<option value="${u.id}" ${u.id === state.uid ? "selected" : ""}>${esc(u.name)}（${ROLE_LABEL[u.role]}）</option>`).join("");
  sel.onchange = () => {
    state.uid = sel.value; localStorage.setItem("gp_uid", state.uid);
    toast(`已切换身份：${sel.selectedOptions[0].textContent}`, "info"); route();
  };
  state.me = state.users.find(u => u.id === state.uid);
}

const ROUTES = {
  units: viewUnits, calendar: viewCalendar, run: viewRun,
  trends: viewTrends, faults: viewFaults, fuel: viewFuel, approvals: viewApprovals,
};
async function route() {
  const name = (location.hash.replace("#/", "") || "units").split("?")[0];
  const fn = ROUTES[name] || viewUnits;
  document.querySelectorAll(".mainnav a").forEach(a =>
    a.classList.toggle("active", a.dataset.view === name));
  try { await fn(); } catch (e) {
    $("#view").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

(async function boot() {
  await initIdentity();
  await route();
  refreshAlerts();
  setInterval(refreshAlerts, 8000);
})();
