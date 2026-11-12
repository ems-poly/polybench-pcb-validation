/* Live multimeter card (docs/MULTIMETER.md). The server decides and proposes; this card shows the meter, the target test, why a
   reading is not proposed, the auto-accept switch and Undo. State arrives as {kind:'meter'} events and from GET /api/meter. */
const METER = (() => {
  let M = null, rcv = 0, ticker = null;
  const last = {};
  const text = (s, n) => String(s == null ? '' : s).slice(0, n || 400);
  const dotCls = c => c.state === 'connected' ? 'ok' : (c.state === 'lost' ? 'bad' : (c.state === 'silent' ? 'warn' : 'off'));
  function connLine(m) {
    const c = m.conn || {};
    if (c.paused) return 'Paused: a dmm.read action holds the cable';
    switch (c.state) {
      case 'connected': return 'Connected: ' + esc(c.model || 'meter') + (c.rate_hz ? ' · ' + c.rate_hz + ' readings/s' : '') + (c.frames_bad ? ' · ' + c.frames_bad + ' bad frame(s) dropped' : '');
      case 'lost': return 'Lost: ' + esc(c.reason || 'no answer from the meter') + '. Reconnects by itself.';
      case 'silent': return esc(c.reason || 'waiting for the meter');
      case 'not_installed': return 'Not connected: hidapi is not installed. Run <code>' + esc(m.install || "pip install -e '.[dmm]'") + '</code> and restart Polybench.';
      default: return 'Not connected: plug in the CH9329 cable (USB 1A86:E429) and switch the meter on.';
    }
  }
  function readingHtml(m) {
    const r = m.reading;
    if (!r) return '<div class="mrd faint">no reading</div>';
    const chips = [];
    chips.push(`<span class="mchip">${esc(r.mode)}</span>`);
    if (r.ac) chips.push('<span class="mchip warn">AC</span>');
    for (const f of r.flags) chips.push(`<span class="mchip ${f === 'LOW BATTERY' ? 'bad' : 'warn'}">${esc(f)}</span>`);
    if (r.status) chips.push(`<span class="mchip faint" title="Where the unit of this mode and range comes from: seen on a real meter, or the UNI-T manual only">${r.status === 'verified' ? 'unit seen on meter' : 'unit per manual'}</span>`);
    const ol = r.kind === 'ol' || r.kind === 'negol';
    return `<div class="mrd"><span class="mval mono ${ol ? 'ol' : ''}" data-kind="${esc(r.kind)}">${esc(r.kind === 'number' ? r.text : (r.kind === 'ol' ? 'OL' : r.kind === 'negol' ? '-OL' : r.text || '--'))}</span><span class="munit">${esc(r.kind === 'number' ? r.unit : '')}</span><span class="mchips">${chips.join('')}</span></div>`;
  }
  function statusHtml(m) {
    const t = m.target, c = m.conn || {};
    let h = '';
    if (c.state === 'connected') {
      h += t ? `<div class="mtgt">To <b class="mono">${esc(t.board)}</b> · <b class="mono">${esc(t.test_id)}</b> ${esc(t.title)} <span class="muted">(${esc(t.unit || 'no unit')})</span></div>` : '<div class="mtgt muted">No test in focus.</div>';
      const p = m.proposal;
      if (p) h += `<div class="mprop">Proposed <b class="mono">${esc(p.value)} ${esc(p.unit)}</b> to ${esc(p.test_id)}. Accept it like any proposal (<kbd>Enter</kbd> or the Accept button).${m.auto && m.auto.on && m.auto.due_in_s != null ? ` <span class="mauto" id="mauto">Accepting in ${m.auto.due_in_s.toFixed(1)} s</span>` : ''}</div>`;
      if (m.reason && !(p && /^proposed/.test(m.reason))) h += `<div class="mwhy ${/^(waiting|proposed|this reading was already)/.test(m.reason) ? '' : 'warn'}">${esc(m.reason)}</div>`;
    }
    if (m.note) h += `<div class="mnote">${esc(m.note)}</div>`;
    return h;
  }
  function ctlHtml(m) {
    const a = m.auto || {}, u = m.undo;
    return `<label class="sw" title="Accept a stable proposal after ${a.delay_s} s, speak it, move to the next test. Off by default."><input type="checkbox" data-meter="auto" ${a.on ? 'checked' : ''}> Auto-accept</label>`
      + (u ? ` <button class="sm" data-meter="undo" title="Take the last auto-accepted value back">Undo ${esc(u.value)} ${esc(u.unit)} <span id="mundo">(${u.left_s.toFixed(0)} s)</span></button>` : '');
  }
  function html() {
    if (!M || !M.enabled) return '';
    const c = M.conn || {};
    return `<div class="mcard" id="metercard" role="region" aria-label="Multimeter"><div class="mhead"><i class="mdot ${dotCls(c)}"></i><b>Multimeter</b><span class="mconn" id="mconn">${connLine(M)}</span><span class="sp"></span><span class="mctl" id="mctl">${ctlHtml(M)}</span></div>
      <div id="mread">${readingHtml(M)}</div><div id="mstat">${statusHtml(M)}</div></div>`;
  }
  function paint() {
    const card = document.getElementById('metercard');
    if (!card) return;
    if (!M || !M.enabled) { card.remove(); return; }
    const set = (id, h) => { const n = document.getElementById(id); if (n && last[id] !== h) { n.innerHTML = h; last[id] = h; } };
    const c = M.conn || {};
    const dot = card.querySelector('.mdot'); if (dot) dot.className = 'mdot ' + dotCls(c);
    set('mconn', connLine(M)); set('mread', readingHtml(M)); set('mstat', statusHtml(M)); set('mctl', ctlHtml(M));
  }
  function tick() {
    if (!M) return;
    const dt = (performance.now() - rcv) / 1000;
    const a = document.getElementById('mauto'); if (a && M.auto && M.auto.due_in_s != null) a.textContent = 'Accepting in ' + Math.max(0, M.auto.due_in_s - dt).toFixed(1) + ' s';
    const u = document.getElementById('mundo'); if (u && M.undo) { const left = M.undo.left_s - dt; if (left <= 0) { M.undo = null; paint(); } else u.textContent = '(' + left.toFixed(0) + ' s)'; }
  }
  function set(m) {
    const had = !!(M && M.enabled);
    M = m; rcv = performance.now();
    if (!ticker) ticker = setInterval(tick, 200);
    const card = document.getElementById('metercard');
    if (card) paint(); else if (had !== !!(m && m.enabled) && typeof render === 'function') render();
  }
  async function init() {
    const r = await api('/api/meter');
    if (r.ok) { const first = !M; M = r.data; rcv = performance.now(); if (first && M.enabled && !ticker) ticker = setInterval(tick, 200); if (typeof render === 'function' && S && M.enabled) render(); }
  }
  document.addEventListener('click', async e => {
    const b = e.target.closest('button[data-meter="undo"]');
    if (!b) return;
    const r = await api('/api/meter/undo', {});
    if (!r.ok) toast(r.data.error || 'could not undo', true);
  });
  document.addEventListener('change', async e => {
    const c = e.target.closest('input[data-meter="auto"]');
    if (!c) return;
    const r = await api('/api/meter/auto', { on: c.checked });
    if (r.ok) set(r.data); else toast(r.data.error || 'could not change auto-accept', true);
  });
  function onEvent(m) {
    if (m.kind === 'meter') set(m.state);
    else if (m.kind === 'meter_advance') refresh().then(() => { if (typeof ui !== 'undefined' && ui.board === m.board && ui.test === m.test_id) advance(); });
    else if (m.kind === 'meter_focus') refresh().then(() => { ui.board = m.board; ui.test = m.test_id; ui.pv = null; ui.draft = ''; render(); });
  }
  return { html, paint, set, init, onEvent, get state() { return M; } };
})();
