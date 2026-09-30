/* 发电机运维平台前端（原生 JS，无依赖） */
'use strict';

const API = {
  async get(path){ const r = await fetch(path); if(!r.ok) throw await err(r); return r.json(); },
  async post(path, body={}){
    body.user = state.user;
    const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    if(!r.ok) throw await err(r);
    return r.json();
  }
};
async function err(r){ try{ const j = await r.json(); return new Error(j.error || ('HTTP '+r.status)); }catch(e){ return new Error('HTTP '+r.status);} }

const state = {
  user: 'zhangwei', meta:null, view:'overview',
  cal: {year:null, month:null}, sessionId:null, poll:null,
  trend:{gen:null, minutes:5}
};
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt1 = (v,u='') => v==null ? '—' : (Number(v).toFixed(1)+u);

function toast(msg, kind=''){
  const t = $('#toast'); t.textContent = msg; t.className = 'toast '+kind;
  clearTimeout(t._h); t._h = setTimeout(()=>t.classList.add('hidden'), 3200);
  t.classList.remove('hidden');
}
function isSupervisor(){ return meta('users')[state.user]?.role === 'supervisor'; }
function meta(k){ return state.meta?.[k]; }
function stLabel(s){ return meta('status_text')?.[s] || s; }
function stepLabel(c){ return meta('step_text')?.[c] || c; }

/* ---------------- 模态 ---------------- */
function modal({title, body, okText='确定', okClass='btn-yellow', onOk, onCancel, cancelText='取消'}){
  const root = $('#modalRoot');
  root.innerHTML = `<div class="modal-mask"><div class="modal">
    <header><span>${esc(title)}</span><button class="btn" id="mClose">×</button></header>
    <div class="body">${body}</div>
    <footer><button class="btn" id="mCancel">${esc(cancelText)}</button>
      <button class="btn ${okClass}" id="mOk">${esc(okText)}</button></footer></div></div>`;
  const close = () => { root.innerHTML = ''; if(onCancel) onCancel(); };
  root.querySelector('.modal-mask').addEventListener('mousedown', e => { if(e.target.classList.contains('modal-mask')) close(); });
  $('#mClose').onclick = close; $('#mCancel').onclick = close;
  $('#mOk').onclick = async () => {
    try{ const keep = await onOk(root); if(keep!==true) close(); }catch(e){ const p=$('#mPerm'); if(p) p.textContent=e.message; else toast(e.message,'err'); }
  };
  return root;
}

/* ---------------- 初始化与导航 ---------------- */
async function init(){
  state.meta = (await API.get('/api/meta'));
  const users = meta('users');
  $('#userSelect').innerHTML = Object.entries(users)
    .map(([id,u])=>`<option value="${id}">${esc(u.name)}（${u.role==='supervisor'?'工程主管':'值班员'}）</option>`).join('');
  $('#userSelect').value = state.user;
  $('#userSelect').onchange = e => { state.user = e.target.value; render(); };

  $$('#nav button').forEach(b => b.onclick = () => switchView(b.dataset.view));
  bindStaticForms();
  switchView('overview');
  setInterval(tickReminders, 8000); tickReminders();
  setInterval(() => { if(!document.hidden) refreshCurrent(); }, 3000);
}
function switchView(v){
  state.view = v;
  $$('#nav button').forEach(b => b.classList.toggle('active', b.dataset.view===v));
  $$('.view').forEach(s => s.classList.add('hidden'));
  $('#view-'+v).classList.remove('hidden');
  render();
}
async function refreshCurrent(){
  const v = state.view;
  if(document.querySelector('.modal-mask')) return;  // 模态打开时不重绘
  if(['overview','trends','shutdowns','fuel','calendar','approvals'].includes(v)){
    try{ await render(); }catch(e){ /* 轮询静默 */ }
  }
  if(v==='run'){
    // 值班员正在填写备注/勾选异常项时不重绘，避免清空输入
    const panel = $('#runPanel');
    const typing = panel && (document.activeElement?.closest('#runPanel') ||
      panel.querySelector('input[type=text]:not(:placeholder-shown),input[type=checkbox]:checked'));
    if(!typing){ try{ await render(); }catch(e){} }
  }
}

/* ---------------- 提醒条 ---------------- */
async function tickReminders(){
  try{
    const {reminders} = await API.get('/api/reminders');
    const bar = $('#reminderBar');
    if(!reminders.length){ bar.classList.add('hidden'); bar.innerHTML=''; return; }
    bar.classList.remove('hidden');
    bar.innerHTML = reminders.slice(0,5).map(r =>
      `<div class="reminder-item"><span class="dot"></span><span>🔔 ${esc(r.message)}</span>
       <button class="btn" data-rid="${r.id}">知道了</button></div>`).join('');
    bar.querySelectorAll('button').forEach(b => b.onclick = async () => {
      await API.post('/api/reminders/ack', {id: Number(b.dataset.rid)}); tickReminders();
    });
  }catch(e){}
}

/* ---------------- 机组总览 ---------------- */
async function renderOverview(){
  const [{generators, capacity}] = await Promise.all([API.get('/api/generators')]);
  const cap = $('#capacityBand');
  cap.innerHTML = `
    <div class="cap-cell total"><div class="lab">装机总容量</div><div class="val">${fmt1(capacity.total_kw,' kW')}</div></div>
    <div class="cap-cell ok"><div class="lab">应急可用容量</div><div class="val">${fmt1(capacity.available_kw,' kW')} · ${capacity.available_percent}%</div></div>
    <div class="cap-cell lock"><div class="lab">🔒 维修封锁剔除</div><div class="val">${fmt1(capacity.locked_kw,' kW')}</div></div>
    <div class="cap-cell fault"><div class="lab">故障停机剔除</div><div class="val">${fmt1(capacity.fault_kw,' kW')}</div></div>`;

  const R = meta('ranges');
  $('#genList').innerHTML = generators.map(g => {
    const t = g.telemetry || {};
    const running = g.status==='running';
    const vCls = !running ? 'idle' : (t.voltage>=R.voltage[0]&&t.voltage<=R.voltage[1] ? 'ok':'bad');
    const fCls = !running ? 'idle' : (t.frequency>=R.frequency[0]&&t.frequency<=R.frequency[1] ? 'ok':'bad');
    const bCls = (t.battery>=R.battery[0]&&t.battery<=R.battery[1]) ? 'ok':'bad';
    const low = g.fuel_percent < R.low_fuel_percent;
    const lock = g.locked_out ? `<span class="lock-note">🔒 ${esc(g.lock_reason||'维修封锁中')}</span>` :
      (g.fault ? `<span class="lock-note" style="color:var(--red)">⚠ ${esc(g.fault)}</span>` : '');
    const badge = g.locked_out
      ? `<span class="badge locked_out">🔒 维修封锁</span>${lock}`
      : `<span class="badge ${g.status}">${running?'● ':''}${esc(stLabel(g.status))}</span>${lock}`;
    return `<div class="gen-band ${g.locked_out?'locked':''}">
      <div class="gen-id"><div class="gname">${esc(g.name)} <span style="color:var(--ink-3);font-weight:400;font-size:12px">${esc(g.id)}</span></div>
        <div class="gbuilding">${esc(g.building)} · ${fmt1(g.rated_power_kw,' kW')}</div></div>
      <div class="gauge ${vCls}"><div class="lab"><span>电压</span><span class="range">${R.voltage[0]}–${R.voltage[1]} V</span></div>
        <div class="num">${running?fmt1(t.voltage):'<span style="font-size:13px">停机</span>'}<small>${running?' V':''}</small></div></div>
      <div class="gauge ${fCls}"><div class="lab"><span>频率</span><span class="range">${R.frequency[0]}–${R.frequency[1]} Hz</span></div>
        <div class="num">${running?fmt1(t.frequency,2):'<span style="font-size:13px">停机</span>'}<small>${running?' Hz':''}</small></div></div>
      <div class="gauge ${bCls}"><div class="lab"><span>启动电池</span><span class="range">${R.battery[0]}–${R.battery[1]} V</span></div>
        <div class="num">${fmt1(t.battery ?? g.battery_voltage)}<small> V</small></div></div>
      <div class="gauge"><div class="lab"><span>燃油 ${g.fuel_percent}%${low?' ⚠':''}</span><span class="range">阈值 ${R.low_fuel_percent}%</span></div>
        <div class="num">${fmt1(g.fuel_level_l)}<small> /${g.fuel_capacity_l} L</small></div>
        <div class="fuel-track"><div class="fuel-fill ${low?'low':''}" style="width:${Math.min(100,g.fuel_percent)}%"></div></div></div>
      <div class="gauge"><div class="lab"><span>模拟模式</span></div>
        <div class="num" style="font-size:13px">${({normal:'正常',start_fail:'启动失败',oil_leak:'漏油'})[g.sim_mode]||g.sim_mode}</div></div>
      <div class="gen-status">${badge}
        <div class="gen-actions">
          <button class="btn" data-act="run" data-gen="${g.id}">去试运行</button>
          ${g.locked_out
            ? `<button class="btn btn-green" data-act="unlock" data-gen="${g.id}" ${!isSupervisor()?'disabled title="仅工程主管可解封"':''}>🔓 解封</button>`
            : `<button class="btn btn-red" data-act="lock" data-gen="${g.id}">🔒 封锁</button>`}
        </div>
      </div>
    </div>`;
  }).join('');
  $('#genList').querySelectorAll('button[data-act]').forEach(b => b.onclick = () => overviewAction(b));
}
function overviewAction(b){
  const gid = b.dataset.gen;
  if(b.dataset.act==='run'){ switchView('run'); }
  if(b.dataset.act==='lock'){
    modal({title:'维修封锁', okText:'确认封锁', okClass:'btn-red',
      body:`<div class="ro-line">封锁后机组不能启动，也不计入应急可用容量；进行中的试运行将立即中止。</div>
        <label>封锁原因（必填）<input type="text" id="lockReason" placeholder="如：喷油泵维修，等待备件"></label>
        <div class="perm" id="mPerm"></div>`,
      onOk: async () => {
        const reason = $('#lockReason').value.trim();
        await API.post('/api/lockout', {generator:gid, reason});
        toast('已封锁并留痕','ok'); render();
      }});
  }
  if(b.dataset.act==='unlock'){
    modal({title:'解除维修封锁', okText:'确认解封（工程主管）', okClass:'btn-green',
      body:`<div class="ro-line">仅工程主管可执行解封操作。当前人员：${esc(meta('users')[state.user].name)}</div><div class="perm" id="mPerm"></div>`,
      onOk: async () => { await API.post('/api/lockout/clear', {generator:gid}); toast('机组已恢复待命','ok'); render(); }});
  }
}

/* ---------------- 日历与计划 ---------------- */
function fillPlanSelectors(gens){
  const opts = gens.map(g=>`<option value="${g.id}">${esc(g.name)}（${esc(g.building)}）</option>`).join('');
  $$('select[name=generator]').forEach(s => {
    const sig = Array.from(s.options).map(o=>o.value).join(',');
    const want = gens.map(g=>g.id).join(',');
    if(sig !== want){ const v=s.value; s.innerHTML=opts; if(v)s.value=v; }
  });
  const ass = document.querySelector('select[name=assignee]');
  if(ass && !ass.options.length){
    ass.innerHTML = Object.entries(meta('users')).filter(([,u])=>u.role==='operator')
      .map(([id,u])=>`<option value="${id}">${esc(u.name)}</option>`).join('');
  }
}
function bindStaticForms(){
  $('#planForm').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    const local = f.scheduled_local.value.replace('T',' ') + ':00';
    try{
      await API.post('/api/plans', {generator:f.generator.value, scheduled_at:local,
        duration_min:Number(f.duration_min.value), assignee:f.assignee.value});
      toast('计划已提交，等待工程主管审批','ok'); f.reset(); f.duration_min.value=30; render();
    }catch(err){ toast(err.message,'err'); }
  };
  $('#refuelForm').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    try{
      const r = await API.post('/api/fuel/refuel', {generator:f.generator.value,
        liters:Number(f.liters.value), note:f.note.value});
      toast(`加油 ${r.refuel.liters}L 已登记，等待第二人复核`,'ok'); f.reset(); render();
    }catch(err){ toast(err.message,'err'); }
  };
  $$('input[name=trendRange]').forEach(r => r.onchange = () => { state.trend.minutes = Number(r.value); render(); });
  $$('#trendCharts, .trend-controls').forEach(()=>{});
  $$('.trend-controls button[data-inject]').forEach(b => b.onclick = async () => {
    if(!state.trend.gen) return;
    await API.post('/api/sim/inject', {generator:state.trend.gen, kind:b.dataset.inject, seconds:40});
    toast('已注入模拟波形（约 40 秒）','ok');
  });
  $('#trendGen').onchange = e => { state.trend.gen = e.target.value; render(); };
  $('#attachForm').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    const file = f.file.files[0];
    if(!file){ toast('请选择文件','err'); return; }
    if(file.size > 8*1024*1024){ toast('附件不超过 8MB','err'); return; }
    const b64 = await new Promise((res, rej) => {
      const rd = new FileReader();
      rd.onload = () => res(rd.result.split(',')[1]);
      rd.onerror = rej; rd.readAsDataURL(file);
    });
    try{
      await API.post('/api/attachments', {category:f.category.value, ref_id:f.ref_id.value.trim(),
        filename:file.name, content_type:file.type||'application/octet-stream', data_b64:b64});
      toast('附件已保存到本地目录','ok'); f.reset(); render();
    }catch(err){ toast(err.message,'err'); }
  };
}

async function renderCalendar(gens){
  const {plans} = await API.get('/api/plans');
  const now = new Date();
  if(state.cal.year===null){ state.cal.year=now.getFullYear(); state.cal.month=now.getMonth(); }
  const y=state.cal.year, m=state.cal.month;
  const first = new Date(y,m,1), startDow = first.getDay();
  const days = new Date(y,m+1,0).getDate();
  const prevDays = new Date(y,m,0).getDate();
  let html = `<div class="card" style="display:flex;align-items:center;gap:14px;padding:10px 16px">
      <button class="btn" id="calPrev">‹ 上月</button><b>${y} 年 ${m+1} 月</b>
      <button class="btn" id="calNext">下月 ›</button><span style="flex:1"></span>
      <button class="btn" id="calToday">回到今天</button></div><div class="calendar-grid">`;
  ['日','一','二','三','四','五','六'].forEach(d=>html+=`<div class="cal-dow">${d}</div>`);
  const byDay = {};
  plans.forEach(p => { const d=p.scheduled_at.slice(0,10); (byDay[d]=byDay[d]||[]).push(p); });
  const cell = (day, dkey, other) => {
    const evs = (byDay[dkey]||[]).slice(0,3).map(p =>
      `<div class="cal-ev ${p.status}" title="${esc(p.id)} ${esc(stLabel(p.status))}">${esc(p.generator_name)} · ${p.scheduled_at.slice(11,16)}</div>`).join('');
    const extra = (byDay[dkey]?.length||0) > 3 ? `<div class="cal-ev">+${byDay[dkey].length-3} 更多</div>`:'';
    const today = dkey === nowStr();
    return `<div class="cal-cell ${other?'other':''} ${today?'today':''}"><div class="cal-date">${day}</div>${evs}${extra}</div>`;
  };
  const pad = n => String(n).padStart(2,'0');
  for(let i=0;i<startDow;i++){ const d=prevDays-startDow+i+1; const pm=m-1<0?11:m-1, py=m-1<0?y-1:y;
    html += cell(d, `${py}-${pad(pm+1)}-${pad(d)}`, true); }
  for(let d=1; d<=days; d++){ html += cell(d, `${y}-${pad(m+1)}-${pad(d)}`, false); }
  const total = startDow+days;
  for(let d=1; d<=(7-total%7)%7; d++){ html += cell(d, `${y}-${pad(m+2>12?1:m+2)}-${pad(d)}`, true); }
  html += '</div>';
  $('#calendarGrid').innerHTML = html;
  $('#calPrev').onclick = () => { state.cal.m--; if(state.cal.m<0){state.cal.m=11;state.cal.year--;} render(); };
  $('#calNext').onclick = () => { state.cal.m++; if(state.cal.m>11){state.cal.m=0;state.cal.year++;} render(); };
  $('#calToday').onclick = () => { state.cal={year:now.getFullYear(),month:now.getMonth()}; render(); };

  const users = meta('users');
  const tb = $('#plansTable tbody');
  tb.innerHTML = plans.map(p => {
    let acts = '';
    if(p.status==='pending'){
      acts += `<button class="btn btn-yellow" data-a="approve" data-id="${p.id}" ${!isSupervisor()?'disabled title="仅工程主管"':''}>批准</button>`;
      acts += `<button class="btn" data-a="reject" data-id="${p.id}" ${!isSupervisor()?'disabled':''}>驳回</button>`;
    }
    if(['pending','approved'].includes(p.status)){
      acts += `<button class="btn btn-red" data-a="cancel" data-id="${p.id}">取消留痕</button>`;
    }
    if(p.status==='approved'){
      acts += `<button class="btn btn-green" data-a="start" data-id="${p.id}" ${p.g_locked?'disabled title="机组维修封锁"':''}>开始试运行</button>`;
    }
    if(p.status==='running' || ['completed','failed'].includes(p.status)){
      acts += `<button class="btn" data-a="view" data-id="${p.id}">查看会话</button>`;
    }
    return `<tr><td>${esc(p.id)}</td><td>${esc(p.generator_name)}${p.g_locked?' 🔒':''}</td>
      <td>${esc(p.building||'')}</td><td class="mono">${esc(p.scheduled_at)}</td>
      <td>${p.duration_min} 分</td><td>${esc(users[p.assignee]?.name||p.assignee)}</td>
      <td><span class="badge ${p.status}">${esc(stLabel(p.status))}</span></td><td>${acts}</td></tr>`;
  }).join('');
  tb.querySelectorAll('button[data-a]').forEach(b => b.onclick = () => planAction(b.dataset));
}
function nowStr(){ const d=new Date(); return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`; }
function pad2(n){ return String(n).padStart(2,'0'); }

function planAction({a,id}){
  if(a==='start'){ switchView('run'); setTimeout(()=>startPlan(id), 50); return; }
  if(a==='view'){ state.sessionId=null; switchView('run'); setTimeout(()=>openPlanSession(id), 50); return; }
  if(a==='approve'||a==='reject'){
    const decision = a==='approve'?'approved':'rejected';
    modal({title: a==='approve'?'批准试运行计划':'驳回试运行计划',
      okText: a==='approve'?'批准':'驳回', okClass: a==='approve'?'btn-yellow':'btn-red',
      body:`<label>审批意见（留痕）<input type="text" id="apReason" placeholder="如：月度例行，同意"></label>
        <div class="ro-line">审批人：${esc(meta('users')[state.user].name)}（仅工程主管）</div><div class="perm" id="mPerm"></div>`,
      onOk: async () => { await API.post('/api/plans/approve', {plan:id, decision, reason:$('#apReason').value});
        toast('已提交审批结论并留痕','ok'); render(); }});
  }
  if(a==='cancel'){
    modal({title:'取消试运行计划', okText:'确认取消', okClass:'btn-red',
      body:`<label>取消原因（必填，将留痕）<input type="text" id="cxReason"></label><div class="perm" id="mPerm"></div>`,
      onOk: async () => { await API.post('/api/plans/cancel', {plan:id, reason:$('#cxReason').value});
        toast('计划已取消并留痕','ok'); render(); }});
  }
}
async function openPlanSession(pid){
  const {session} = await API.get('/api/session?plan='+pid);
  if(session){ state.sessionId = session.id; render(); }
}

/* ---------------- 步骤执行 ---------------- */
async function renderRun(){
  if(!state.sessionId){
    const [{plans}] = await Promise.all([API.get('/api/plans')]);
    const startable = plans.filter(p=>['approved','running'].includes(p.status));
    $('#sessionPicker').classList.remove('hidden'); $('#runPanel').classList.add('hidden');
    $('#startablePlans').innerHTML = startable.length ? startable.map(p =>
      `<div class="startable"><div class="t">${esc(p.generator_name)} ${p.g_locked?'🔒':''}</div>
        <div class="m">${esc(p.id)} · ${esc(p.scheduled_at)} · 值班员 ${esc(meta('users')[p.assignee]?.name||'')}
          · <span class="badge ${p.status}">${esc(stLabel(p.status))}</span></div>
        <button class="btn ${p.status==='running'?'btn-yellow':'btn-green'}" data-pid="${p.id}"
          ${p.status==='approved'&&p.g_locked?'disabled':''}>
        ${p.status==='running'?'继续运行会话':(p.g_locked?'🔒 维修封锁，禁止启动':'开始试运行')}</button></div>`).join('')
      : '<div class="hint">暂无已获批待执行的计划，请到“试运行日历”提交计划。</div>';
    $$('#startablePlans button[data-pid]').forEach(b => b.onclick = () => startPlan(b.dataset.pid));
    return;
  }
  let s;
  try{ s = (await API.get('/api/session?id='+state.sessionId)).session; }
  catch(e){ state.sessionId=null; return render(); }
  $('#sessionPicker').classList.add('hidden'); $('#runPanel').classList.remove('hidden');
  const g = s.generator, t = (await API.get('/api/telemetry?generator='+g.id)).latest;
  const ended = !!s.ended_at;
  const lockBadge = g.locked_out ? `<span class="badge locked_out">🔒 维修封锁：${esc(g.lock_reason||'')}</span>` : '';
  $('#runHead').className = 'card run-head-card ' + (ended?'':'running');
  $('#runHead').innerHTML = `<h3>${esc(g.name)}（${esc(g.building)}） ${lockBadge}</h3>
    <div class="run-meta">
      <span>会话 <b>${esc(s.id)}</b></span><span>计划 <b>${esc(s.plan_id)}</b></span>
      <span>值班员 <b>${esc(s.operator_name)}</b></span>
      <span>开始 <b>${esc(s.started_at)}</b></span>
      ${ended?`<span>结束 <b>${esc(s.ended_at)}</b></span><span>结果 <b>${s.outcome==='completed'?'✅ 全部合格，机组待命':'❌ '+s.outcome}</b></span>`:''}
      ${s.run_minutes?`<span>运行 <b>${s.run_minutes} 分钟</b></span>`:''}
      ${s.fuel_delta_l!=null?`<span>耗油 <b>${s.fuel_delta_l} L</b></span>`:''}
      <button class="btn" id="quitSession" style="margin-left:auto">选择其他计划</button>
    </div>`;
  $('#quitSession').onclick = () => { state.sessionId=null; render(); };

  const R = meta('ranges');
  const live = (key, range, unit, digits=1) => {
    if(!t) return ['dim','—'];
    const off = t.engine ? (t[key]<range[0]||t[key]>range[1]) : false;
    return [off?'bad':'ok', `${Number(t[key]).toFixed(digits)}<small> ${unit}（${range[0]}–${range[1]}）</small>`];
  };
  const [vC,vH] = live('voltage',R.voltage,'V');
  const [fC,fH] = live('frequency',R.frequency,'Hz',2);
  const [bC,bH] = live('battery',R.battery,'V');
  const fuelPct = t ? t.fuel_percent : g.fuel_percent;
  const fCls2 = fuelPct>=R.fuel_ready_percent?'ok':'bad';

  const stepEls = s.steps.map((st,i) => {
    const code = st.step_code, seq = st.seq, active = !ended && s.current_step===code && st.status==='pending';
    const cls = st.status==='pass'?'done':st.status==='fail'?'fail':st.status==='skipped'?'skipped':active?'active':'';
    let readouts = '', actions = '';
    if(code==='visual'){
      readouts = `<div class="ro dim"><div class="k">目检项</div><div class="v" style="font-size:12.5px">机身/管路/接地/异响</div></div>`;
      if(active) actions = `
        <div style="grid-column:1/-1">
          <div class="step-checkrow">
            <label><input type="checkbox" id="ckNoise"> 发现异常噪声</label>
            <label><input type="checkbox" id="ckLeak"> 发现漏油/渗油</label>
            <input type="text" id="visualNote" placeholder="现场备注" style="flex:1;min-width:160px">
          </div>
          <div class="step-actions" style="justify-content:flex-end">
            <button class="btn btn-red" data-do="visual-fail">外观不合格</button>
            <button class="btn btn-green" data-do="visual-pass">检查合格，下一步</button></div></div>`;
    }
    if(code==='fuel'){
      readouts = `<div class="ro ${fCls2}"><div class="k">实测油位（合格 ≥${R.fuel_ready_percent}%）</div><div class="v">${fuelPct}<small> % · ${t?fmt1(t.fuel):fmt1(g.fuel_level_l)} L</small></div></div>`;
      if(active) actions = `<button class="btn btn-green" data-do="fuel">确认油位合格</button>`;
    }
    if(code==='battery'){
      readouts = `<div class="ro ${bC}"><div class="k">电池电压</div><div class="v">${bH}</div></div>`;
      if(active) actions = `<button class="btn btn-green" data-do="battery">电池正常，下一步</button>`;
    }
    if(code==='start_noload'){
      readouts = `<div class="ro ${vC}"><div class="k">空载电压</div><div class="v">${t&&t.engine?vH:'<span style="font-size:12px">未启动</span>'}</div></div>
        <div class="ro ${fC}"><div class="k">空载频率</div><div class="v">${t&&t.engine?fH:'<span style="font-size:12px">未启动</span>'}</div></div>`;
      if(active) actions = `<button class="btn btn-yellow" data-do="start">▶ 空载启动</button>`;
    }
    if(code==='load_test'){
      const loaded = t && t.loaded;
      readouts = `<div class="ro ${vC}"><div class="k">带载电压</div><div class="v">${loaded?vH:'<span style="font-size:12px">未带载</span>'}</div></div>
        <div class="ro ${fC}"><div class="k">带载频率</div><div class="v">${loaded?fH:'<span style="font-size:12px">未带载</span>'}</div></div>
        <div class="ro"><div class="k">负载率</div><div class="v" style="font-size:13px">${loaded?'约 50%':'—'}</div></div>`;
      if(active) actions = loaded
        ? `<span class="load-live">带载运行中，实时值与合格区间并排显示</span>
           <button class="btn btn-red" data-do="load-abort">带载中断</button>
           <button class="btn btn-green" data-do="load-complete">参数合格，结束带载</button>`
        : `<button class="btn btn-yellow" data-do="load-begin">投入负载（约 50%）</button>`;
    }
    if(active && !ended){
      actions += ` <button class="btn btn-ghost" data-do="skip" data-step="${code}"
        ${!isSupervisor()?'disabled title="越过步骤须工程主管批准"':''}>越过（主管批准）</button>`;
    }
    const stTxt = {pass:'✅ 合格',fail:'❌ 不合格',skipped:'⤼ 已越过（主管批准）',pending: st.status}[
      st.status==='pending' ? (active?'待执行':'未解锁') : st.status] || st.status;
    const note = st.note ? `<div class="step-note">${esc(st.note)}${st.checked_at?` · ${esc(st.checked_at)}`:''}</div>` : '';
    return `<div class="step ${cls}">
      <div class="step-num">${seq}</div>
      <div class="step-title">${esc(stepLabel(code))}<span class="st st-${st.status==='pending'?'pending':st.status}">${esc(stTxt)}</span></div>
      <div class="step-readouts">${readouts}</div>
      <div class="step-actions">${actions}</div>${note}</div>`;
  }).join('');
  $('#stepsCol').innerHTML = stepEls;
  $$('#stepsCol button[data-do]').forEach(b => b.onclick = () => stepAction(b, s));

  // 结束面板
  const allDone = s.steps.every(x => x.status!=='pending');
  if(ended){
    $('#endPanel').innerHTML = endSummary(s);
  } else if(allDone){
    const startFuel = s.start_fuel_l ?? g.fuel_level_l;
    $('#endPanel').innerHTML = `<h3>结束试运行：记录运行时长与燃油差额</h3>
      <form id="endForm" class="form-row">
        <label>运行时长（分钟）<input type="number" name="run_minutes" min="0" step="0.1" style="width:110px"></label>
        <label>实测剩余燃油（L）<input type="number" name="fuel" min="0" max="${startFuel}" step="0.1"
          value="${(t? t.fuel: startFuel).toFixed(1)}" style="width:120px"></label>
        <span class="hint">会话开始账面 ${fmt1(startFuel)} L；差额超 ${R.fuel_tolerance_l}L 将自动留痕</span>
        <button class="btn btn-green" type="submit" style="margin-left:auto">结束并恢复待命</button></form>
      <div class="perm" id="endPerm" style="color:var(--red);font-size:12.5px"></div>`;
    $('#endForm').onsubmit = async e => { e.preventDefault();
      try{ await API.post('/api/session/end', {session:s.id,
        measured_remaining_l: Number(e.target.fuel.value),
        run_minutes: e.target.run_minutes.value===''?null:Number(e.target.run_minutes.value)});
        toast('试运行结束，全部合格时机组恢复待命','ok'); render();
      }catch(err){ $('#endPerm').textContent = err.message; } };
  } else {
    $('#endPanel').innerHTML = `<h3>试运行进行中</h3><div class="hint">请按纵向顺序完成全部检查步骤；故障将立即中止并留痕。</div>`;
  }
}
function endSummary(s){
  const d = s.fuel_delta_l;
  return `<h3>本次试运行结算</h3>
    <div class="run-meta">
      <span>结果 <b style="color:${s.outcome==='completed'?'var(--green)':'var(--red)'}">${s.outcome==='completed'?'全部合格，机组已回待命':'存在不合格项，机组未恢复待命'}</b></span>
      <span>运行时长 <b>${s.run_minutes ?? '—'} 分钟</b></span>
      <span>起始油位 <b>${fmt1(s.start_fuel_l)} L</b></span>
      <span>实测剩余 <b>${fmt1(s.measured_remaining_l)} L</b></span>
      <span>燃油差额（消耗） <b>${fmt1(d)} L</b></span></div>`;
}

async function startPlan(pid){
  try{
    const r = await API.post('/api/session/start', {plan:pid});
    state.sessionId = r.session.id;
    toast(r.created ? '运行会话已建立（同计划重复开始将复用此会话）' : '该计划已有运行会话，已复用','ok');
    render();
  }catch(e){ toast(e.message,'err'); }
}
async function stepAction(b, s){
  const doWhat = b.dataset.do;
  try{
    if(doWhat==='visual-pass') await API.post('/api/steps/visual', {session:s.id, passed:true,
      noise:$('#ckNoise').checked, oil_leak:$('#ckLeak').checked, note:$('#visualNote').value});
    if(doWhat==='visual-fail') await API.post('/api/steps/visual', {session:s.id, passed:false, note:$('#visualNote').value});
    if(doWhat==='fuel') await API.post('/api/steps/fuel', {session:s.id});
    if(doWhat==='battery') await API.post('/api/steps/battery', {session:s.id});
    if(doWhat==='start') await API.post('/api/steps/start', {session:s.id});
    if(doWhat==='load-begin') await API.post('/api/steps/load/begin', {session:s.id});
    if(doWhat==='load-complete') await API.post('/api/steps/load/complete', {session:s.id});
    if(doWhat==='load-abort'){
      const reason = await new Promise((resolve,reject) => modal({title:'带载中断', okText:'确认中断（故障停机）', okClass:'btn-red',
        body:`<label>中断现象/原因（必填，留痕）<input type="text" id="abortReason" placeholder="如：转速波动、排烟异常"></label><div class="perm" id="mPerm"></div>`,
        onCancel: reject,
        onOk: () => { const v=$('#abortReason').value.trim()||'未说明'; resolve(v); }}));
      await API.post('/api/steps/load/abort', {session:s.id, reason});
    }
    if(doWhat==='skip'){
      const step = b.dataset.step;
      await new Promise((resolve,reject) => modal({title:`越过检查步骤：${stepLabel(step)}`,
        okText:'主管批准越步', okClass:'btn-yellow',
        body:`<div class="ro-line">仅工程主管可批准；批准后该步骤标记为“已越过”并写入审批记录。</div>
          <label>越步理由（必填）<input type="text" id="skipReason"></label>
          <div class="ro-line">审批人：${esc(meta('users')[state.user].name)}</div><div class="perm" id="mPerm"></div>`,
        onOk: async () => { try{ await API.post('/api/steps/skip', {session:s.id, step, reason:$('#skipReason').value}); resolve(); }catch(e){ reject(e); return true; } }}));
    }
    toast('步骤已提交','ok'); render();
  }catch(e){ if(e && e.message){ toast(e.message,'err'); render(); } }
}

/* ---------------- 参数趋势（SVG） ---------------- */
async function renderTrends(gens){
  const sel = $('#trendGen');
  if(!sel.options.length){ sel.innerHTML = gens.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('');
    state.trend.gen = state.trend.gen || gens[0].id; sel.value = state.trend.gen;
  }
  const gid = state.trend.gen || sel.value;
  const {series, latest} = await API.get(`/api/telemetry?generator=${gid}&minutes=${state.trend.minutes}`);
  const R = meta('ranges');
  const g = gens.find(x=>x.id===gid);
  const defs = [
    {key:'voltage', name:'电压', unit:'V', range:R.voltage, digits:1, cls:''},
    {key:'frequency', name:'频率', unit:'Hz', range:R.frequency, digits:2, cls:''},
    {key:'fuel', name:'燃油液位', unit:'L', range:[0,g.fuel_capacity_l], digits:1, cls:'fuel',
      softLow: g.fuel_capacity_l * R.low_fuel_percent/100},
    {key:'battery', name:'电池电压', unit:'V', range:R.battery, digits:2, cls:''}
  ];
  $('#trendCharts').innerHTML = defs.map(d => chartCard(d, series, latest)).join('');
  $$('#trendCharts svg.chart').forEach(svg => bindCrosshair(svg));
}
function chartCard(d, series, latest){
  const W=520,H=170, pad={l:44,r:14,t:14,b:24};
  const cur = latest ? latest[d.key] : null;
  const inRange = cur!=null && cur>=d.range[0] && cur<=d.range[1];
  const pts = series.map(p=>({t:p.t, v:p[d.key], ts:p.ts, loaded:p.loaded}));
  const [lo,hi] = d.range;
  const padv = (hi-lo)*0.25 || 1;
  const yMin = Math.min(lo-padv, ...pts.map(p=>p.v)), yMax = Math.max(hi+padv, ...pts.map(p=>p.v));
  const x = t => pts.length<2 ? pad.l : pad.l + (W-pad.l-pad.r)*(t-pts[0].t)/Math.max(1,pts[pts.length-1].t-pts[0].t);
  const y = v => H-pad.b - (H-pad.t-pad.b)*(v-yMin)/(yMax-yMin);
  let grid='';
  for(let i=0;i<=4;i++){ const yy=pad.t+(H-pad.t-pad.b)*i/4;
    grid += `<line class="chart-grid" x1="${pad.l}" y1="${yy}" x2="${W-pad.r}" y2="${yy}"/>`;
    grid += `<text class="chart-axis" x="${pad.l-6}" y="${yy+3}" text-anchor="end">${(yMax-(yMax-yMin)*i/4).toFixed(d.digits)}</text>`; }
  const band = `<rect class="chart-band" x="${pad.l}" y="${y(hi)}" width="${W-pad.l-pad.r}" height="${Math.max(0,y(lo)-y(hi))}"/>`;
  const lowBand = d.softLow!=null ? `<rect fill="var(--red-soft)" x="${pad.l}" y="${y(yMin<0?0:yMin)}" width="${W-pad.l-pad.r}" height="${y(d.softLow)-y(yMin<0?0:yMin)}"/>` : '';
  const path = pts.length ? `M ${pts.map(p=>`${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' L ')}` : '';
  const last = pts.length ? pts[pts.length-1] : null;
  const xlabels = pts.length ? [0,0.25,0.5,0.75,1].map(q=>{
    const p = pts[Math.min(pts.length-1, Math.floor(q*(pts.length-1)))];
    return `<text class="chart-axis" x="${x(p.t)}" y="${H-6}" text-anchor="middle">${p.ts.slice(11,16)}</text>`; }).join('') : '';
  const svgId = 'svg-'+d.key+'-'+Math.abs(pts.length*13+(cur?Math.round(cur*7):0));
  return `<div class="chart-card"><h3><span>${d.name}（${d.unit}）</span>
      <span class="now ${inRange?'ok':'bad'}">${cur==null?'—':Number(cur).toFixed(d.digits)+' '+d.unit}</span></h3>
    <svg class="chart" viewBox="0 0 ${W} ${H}" id="${svgId}" data-unit="${d.unit}"
      data-digits="${d.digits}" data-ymin="${yMin}" data-ymax="${yMax}">
      ${grid}${lowBand}${band}
      <path class="chart-line ${d.cls}" d="${path}"/>
      ${last?`<circle class="chart-dot ${d.cls}" cx="${x(last.t)}" cy="${y(last.v)}" r="3.5"/>`:''}
      <line class="chart-cross" x1="0" y1="0" x2="0" y2="${H-pad.b}" style="display:none"/>
      <circle class="hover-dot" r="4" style="display:none;fill:${d.cls?'var(--yellow-deep)':'var(--gray-700)'};stroke:#fff;stroke-width:1.5"/>
      ${xlabels}</svg>
    <div class="hint">浅色绿带：合格区间 ${d.range[0]}–${d.range[1]} ${d.unit}${d.softLow!=null?`；红带：低油位区`:''}</div></div>`;
}
function bindCrosshair(svg){
  const tip = document.createElement('div'); tip.className='chart-tip';
  const cross = svg.querySelector('.chart-cross');
  const dot = svg.querySelector('.hover-dot');
  const path = svg.querySelector('.chart-line');
  const unit = svg.dataset.unit, digits = Number(svg.dataset.digits);
  if(!path.getAttribute('d')) return;
  const H = 170, padT = 14, padB = 24;
  const yMin = Number(svg.dataset.ymin), yMax = Number(svg.dataset.ymax);
  const valueAt = py => yMax - (py-padT)/(H-padB-padT)*(yMax-yMin);
  svg.onmousemove = e => {
    const r = rect();
    const px = (e.clientX-r.left)*520/r.width;
    const len = path.getTotalLength();
    let best=null, bd=1e9;
    for(let i=0;i<=len;i+=2){ const pp=path.getPointAtLength(i); if(Math.abs(pp.x-px)<bd){bd=Math.abs(pp.x-px);best=pp;} }
    if(!best) return;
    cross.setAttribute('x1',best.x); cross.setAttribute('x2',best.x); cross.style.display='';
    dot.setAttribute('cx',best.x); dot.setAttribute('cy',best.y); dot.style.display='';
    if(!tip.parentNode) document.body.appendChild(tip);
    tip.textContent = `坐标值 ${valueAt(best.y).toFixed(digits)} ${unit}`;
    tip.style.left = (e.clientX + 12)+'px';
    tip.style.top = (e.clientY + 12)+'px';
  };
  svg.onmouseleave = () => { cross.style.display='none'; dot.style.display='none'; tip.remove(); };
}

/* ---------------- 故障停机 ---------------- */
const SHUT_CATS = [
  ['','全部'],['start_fail','启动失败'],['voltage','电压/频率越限'],['noise','异常噪声'],
  ['oil_leak','漏油'],['load_abort','带载中断'],['fuel_discrepancy','燃油差异'],['plan_cancel','计划取消']
];
async function renderShutdowns(){
  const cat = state.shutCat || '';
  const {events} = await API.get('/api/events'+(cat?`?category=${cat}`:''));
  $('#shutdownFilters').innerHTML = SHUT_CATS.map(([c,n]) =>
    `<button class="btn ${c===cat?'on':''}" data-c="${c}">${n}</button>`).join('');
  $$('#shutdownFilters button').forEach(b => b.onclick = () => { state.shutCat=b.dataset.c; render(); });
  const catName = Object.fromEntries(SHUT_CATS);
  $('#shutdownList').innerHTML = events.map(e =>
    `<div class="event ${e.severity}"><div class="ts">${esc(e.ts)}</div>
      <span class="sev ${e.severity}">${e.severity==='critical'?'严重':e.severity==='warning'?'警告':'信息'}</span>
      <div><div class="msg">${esc(e.message)}</div>
        <div class="cat">${esc(catName[e.category]||e.category)} · ${esc(e.generator_name||'')} · ${esc(e.session_id||'')}</div></div>
      <span class="badge ${e.severity==='critical'?'start_failed':'pending'}">${esc(e.category)}</span></div>`).join('')
    || '<div class="card hint">暂无事件。</div>';
  const {attachments} = await API.get('/api/attachments');
  const users = meta('users');
  $('#attachTable tbody').innerHTML = attachments.map(a =>
    `<tr><td class="mono">${esc(a.uploaded_at)}</td><td>${esc(a.category)}</td>
      <td>${esc(a.ref_id||'')}</td><td>${esc(a.filename)}</td>
      <td>${(a.size/1024).toFixed(1)} KB</td><td>${esc(users[a.uploaded_by]?.name||a.uploaded_by)}</td>
      <td><a class="btn" href="/api/attachments/${a.id}" target="_blank">查看/下载</a></td></tr>`).join('')
    || '<tr><td colspan="7" class="hint">暂无附件</td></tr>';
}

/* ---------------- 燃油台账 ---------------- */
async function renderFuel(gens){
  const [{refuels},{rows},{generators}] = await Promise.all([
    API.get('/api/fuel/refuels'), API.get('/api/fuel/ledger'), API.get('/api/generators')]);
  const users = meta('users');
  const total = gens.reduce((a,g)=>a+g.fuel_level_l,0), cap = gens.reduce((a,g)=>a+g.fuel_capacity_l,0);
  $('#fuelBalanceBand').innerHTML = `
    <div class="cap-cell total"><div class="lab">总库存</div><div class="val">${fmt1(total)} L</div></div>
    <div class="cap-cell lock"><div class="lab">总容量</div><div class="val">${fmt1(cap)} L</div></div>
    <div class="cap-cell ok"><div class="lab">账实相符机组</div><div class="val">${generators.filter(g=>Math.abs(g.fuel_level_l - (rows.filter(r=>r.generator_id===g.id).reduce((a,r)=>a+r.change_l,0)))<0.01).length}/${generators.length}</div></div>
    <div class="cap-cell fault"><div class="lab">低油位机组</div><div class="val">${generators.filter(g=>g.low_fuel).length}</div></div>`;
  const rfStatus = {pending_second_check:'待第二人复核', confirmed:'已确认', revised:'已修订', rejected:'已驳回'};
  $('#refuelsTable tbody').innerHTML = refuels.map(r => {
    let review = '';
    if(r.status==='pending_second_check'){
      const self = r.recorder_id===state.user;
      review = `<button class="btn btn-green" data-r="ok" data-id="${r.id}" ${self?'disabled title="登记人不能复核本人记录"':''}>确认无误</button>
        <button class="btn btn-yellow" data-r="rev" data-id="${r.id}" ${self?'disabled title="须第二人"':''}>修订数量</button>
        <button class="btn btn-red" data-r="no" data-id="${r.id}" ${self?'disabled':''}>驳回</button>`;
    } else {
      review = `<span class="hint">${esc(r.reviewer_name||'')}${r.reviewed_at? ' · '+esc(r.reviewed_at):''}${r.revised_liters!=null?` · 原登记 ${r.revised_liters}L`:''}</span>`;
    }
    return `<tr><td>${esc(r.id)}</td><td>${esc(r.generator_name)}</td>
      <td class="mono">${fmt1(r.liters)} L${r.status==='revised'?` <span class="hint">原 ${r.revised_liters}</span>`:''}</td>
      <td>${esc(r.recorder_name)}</td>
      <td><span class="badge ${r.status==='pending_second_check'?'start_failed':r.status==='rejected'?'start_failed':'running'}">${rfStatus[r.status]}</span></td>
      <td style="white-space:nowrap">${review}</td></tr>`;
  }).join('') || '<tr><td colspan="6" class="hint">暂无加油记录</td></tr>';
  $$('#refuelsTable button[data-r]').forEach(b => b.onclick = () => reviewAction(b.dataset, refuels));
  const reasonText = {init:'期初', run_consumption:'试运行消耗', refuel:'加油入库', fuel_discrepancy:'燃油差异调账'};
  $('#ledgerTable tbody').innerHTML = rows.map(r =>
    `<tr><td class="mono">${esc(r.created_at)}</td><td>${esc(r.generator_name)}</td>
      <td class="mono ${r.change_l>0?'pos':'neg'}">${r.change_l>0?'+':''}${fmt1(r.change_l)}</td>
      <td class="mono">${fmt1(r.balance_l)}</td><td>${reasonText[r.reason]||esc(r.reason)}</td>
      <td>${esc(r.ref_id||'')}</td><td>${esc(users[r.created_by]?.name||r.created_by)}</td></tr>`).join('');
}
function reviewAction({r,id}, refuels){
  const rec = refuels.find(x=>x.id===id);
  if(r==='ok' || r==='rev'){
    const rev = r==='rev';
    modal({title: rev?'修订加油数量（第二人复核）':'第二人复核加油记录',
      okText: rev?'修订并确认入账':'复核通过，入账', okClass:'btn-green',
      body:`<div class="ro-line">机组：${esc(rec.generator_name)}；登记人：${esc(rec.recorder_name)}；登记数量：${rec.liters} L</div>
        ${rev?`<label>修订后实际加油量（L）<input type="number" id="revLiters" value="${rec.liters}" min="0.1" step="0.1"></label>`:''}
        <label>复核备注<input type="text" id="revNote" placeholder="如：核对油单与流量表一致"></label>
        <div class="ro-line">复核人：${esc(meta('users')[state.user].name)}（不得与登记人为同一人）</div><div class="perm" id="mPerm"></div>`,
      onOk: async () => {
        await API.post('/api/fuel/review', {refuel:id, approved:true,
          revised_liters: rev?Number($('#revLiters').value):null, note:$('#revNote').value});
        toast(rev?'修订后已复核入账':'加油已入账','ok'); render();
      }});
  } else {
    modal({title:'驳回加油记录', okText:'驳回', okClass:'btn-red',
      body:`<label>驳回原因<input type="text" id="revNote"></label><div class="perm" id="mPerm"></div>`,
      onOk: async () => { await API.post('/api/fuel/review', {refuel:id, approved:false, note:$('#revNote').value});
        toast('已驳回','ok'); render(); }});
  }
}

/* ---------------- 审批记录 ---------------- */
async function renderApprovals(){
  const {approvals} = await API.get('/api/approvals');
  const users = meta('users');
  $('#approvalsTable tbody').innerHTML = approvals.map(a =>
    `<tr><td class="mono">${esc(a.created_at)}</td><td>${esc(a.generator_name)}</td>
      <td>${a.kind==='plan'?'计划审批':'越过步骤'}</td>
      <td>${esc(a.plan_id)}${a.step_code?' · '+esc(stepLabel(a.step_code)):''}</td>
      <td><span class="badge ${a.decision==='approved'?'running':'start_failed'}">${a.decision==='approved'?'批准':'驳回'}</span></td>
      <td>${esc(a.reason)}</td>
      <td>${esc(users[a.approver]?.name||a.approver)}（${a.approver_role==='supervisor'?'工程主管':'值班员'}）</td></tr>`).join('')
    || '<tr><td colspan="7" class="hint">暂无审批记录</td></tr>';
}

/* ---------------- 总渲染 ---------------- */
async function render(){
  const {generators} = await API.get('/api/generators');
  fillPlanSelectors(generators);
  if(state.view==='overview') await renderOverview();
  if(state.view==='calendar') await renderCalendar(generators);
  if(state.view==='run') await renderRun();
  if(state.view==='trends') await renderTrends(generators);
  if(state.view==='shutdowns') await renderShutdowns();
  if(state.view==='fuel') await renderFuel(generators);
  if(state.view==='approvals') await renderApprovals();
}
init().catch(e => toast('初始化失败：'+e.message,'err'));
