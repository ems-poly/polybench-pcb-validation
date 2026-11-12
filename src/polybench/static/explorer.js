'use strict';
/* Results explorer (PW-3): what the instrument actually measured for one board in one step, collapsed to a single line by default.
   Rows come from GET /api/measurements (run evidence under runs/, derived answers, proposals). "Use this value" puts a row's value into the
   single box of the test it was proposed to, as an override that still needs Accept. Presentation only: nothing here records a result. */
const EXP = (() => {
  const C = {};                          // 'board|step' -> {ev, rows, busy}
  let OPEN = {};                         // step id -> expanded
  try { OPEN = JSON.parse(localStorage.getItem('pb.exp.open') || '{}') || {}; } catch (e) { OPEN = {}; }
  const save = () => { try { localStorage.setItem('pb.exp.open', JSON.stringify(OPEN)); } catch (e) {} };
  const key = (st, b) => b + '|' + st.id;
  const clock = ts => ts ? esc(String(ts).replace('T', ' ').slice(11, 19)) : '';
  const num = v => typeof v === 'number' ? String(+v.toPrecision(6)) : String(v == null ? '' : v);
  const rows = (st, b) => { const c = C[key(st, b)]; return c && c.rows ? c.rows : null; };

  function row(r, curTest) {
    const val = r.kind === 'analysis' ? '<span class="faint small">' + esc(r.value) + '</span>' : `<span class="mono">${esc(num(r.value))}</span>${r.unit ? ' <span class="faint">' + esc(r.unit) + '</span>' : ''}${r.mode ? ' <span class="faint small">' + esc(r.mode) + '</span>' : ''}`;
    const to = r.test ? `<span class="mono">${esc(r.test)}</span>` : '<span class="faint">&ndash;</span>';
    const use = r.test && r.kind !== 'analysis' ? `<button class="sm" data-act="expuse" data-i="${r.i}" title="Put this value into ${esc(r.test)} as an override; you still press Accept">Use this value</button>` : '';
    return `<tr class="xrow k-${r.kind}${r.test && r.test === curTest ? ' cur' : ''}"><td class="mono">${clock(r.ts)}</td><td>${esc(r.action || '')} <span class="faint">${esc(r.run_label || '')}</span></td>
      <td class="mono">${r.channel != null && r.channel !== '' ? esc(r.channel) : ''}</td><td>${esc(r.quantity || '')}${r.evidence ? `<div class="faint small">${esc(r.evidence)}</div>` : ''}</td><td>${val}</td><td>${to}</td><td class="xuse">${use}</td></tr>`;
  }
  function html(st, b) {
    const c = C[key(st, b)], rs = c && c.rows, open = !!OPEN[st.id];
    fetchIfStale(st, b);
    const n = rs ? rs.filter(r => r.kind !== 'analysis').length : null;
    const { t } = cur();
    const MAX = 400;
    const body = !rs ? '<div class="faint small" style="padding:8px 10px">loading&hellip;</div>' : !rs.length ? '<div class="faint small" style="padding:8px 10px">Nothing measured for ' + esc(b) + ' in this step yet. Run an action above.</div>'
      : `<div class="tblwrap"><table class="xtab"><thead><tr><th>Time</th><th>Action / run</th><th>Ch</th><th>Quantity</th><th>Value</th><th>Proposed to</th><th></th></tr></thead><tbody>${rs.slice(0, MAX).map(r => row(r, t && t.id)).join('')}</tbody></table></div>${rs.length > MAX ? `<div class="faint small" style="padding:6px 10px">first ${MAX} of ${rs.length} rows</div>` : ''}`;
    return `<details class="box xbox" id="expl" data-step="${esc(st.id)}" data-board="${esc(b)}" ${open ? 'open' : ''}><summary>Measurements (${n == null ? '&hellip;' : n}) <span class="faint small">${esc(b)}, ${esc(st.title)}</span></summary>${open ? body : ''}</details>`;
  }
  function fetchIfStale(st, b) {
    const k = key(st, b), c = C[k] || (C[k] = { ev: -1, rows: null, busy: false });
    if (c.busy || c.ev === S.events) return;
    c.busy = true;
    const ev = S.events;
    fetch(`/api/measurements?board=${encodeURIComponent(b)}&step=${encodeURIComponent(st.id)}`).then(r => r.json()).then(d => {
      c.rows = d.rows || []; c.ev = ev; c.busy = false;
      const el = document.getElementById('expl');
      if (el && el.dataset.step === st.id && el.dataset.board === b) { const t = document.createElement('div'); t.innerHTML = html(st, b); el.replaceWith(t.firstElementChild); }
    }).catch(() => { c.busy = false; c.ev = ev; c.rows = c.rows || []; });
  }
  function after() {}               // hook kept for the caller after a render
  document.addEventListener('toggle', e => {
    const d = e.target; if (!d || d.id !== 'expl') return;
    const was = !!OPEN[d.dataset.step]; if (was === d.open) return;
    OPEN[d.dataset.step] = d.open; save();
    if (d.open && S) { const st = S.steps.find(x => x.id === d.dataset.step); if (st) { const t = document.createElement('div'); t.innerHTML = html(st, d.dataset.board).replace('<details', '<details open'); d.replaceWith(t.firstElementChild); } }
  }, true);
  function use(i) {
    const st = curStep(), rs = rows(st, ui.board), r = rs && rs.find(x => x.i === +i);
    if (!r || !r.test) return;
    const info = testInfo(r.test);
    if (!info || info.st.id !== st.id) { toast('That test is not in this step', true); return; }
    ui.gate = null; ui.retired = null; ui.test = r.test; ui.pv = null; ui.armed = null; ui.draft = String(r.value); ui.useVal = String(r.value).trim(); ui.ctxKey = ui.board + '|' + r.test;       // useVal: a deliberate pick, so replacing a typed value needs one Enter, not two
    render();
    const iv = document.getElementById('val');
    if (iv) { iv.value = String(r.value); iv.dataset.ctx = ui.board + '|' + r.test; onInput(); iv.focus(); iv.select(); }
    toast('Value from ' + (r.run_label || 'the run') + ' is in the box for ' + r.test + '. Press Enter to accept it.');
  }
  return { html, after, use, rows };
})();
