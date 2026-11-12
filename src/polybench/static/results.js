'use strict';
/* Results and anomalies: how a recorded value, its verdict and its source are shown, the Anomalies strip, the per-board results table,
   the table-valued cell with its small plot, and the flash that shows what an action just recorded. Presentation only: every verdict
   comes from the server (engine.evaluate); nothing here decides pass or fail. */
const RES = (() => {
  const LABEL = { pass: 'PASS', none: 'RECORD', review: 'REVIEW', fail: 'FAIL', stop: 'STOP', proposed: 'PROPOSED', invalid: 'INVALID' };
  const num = v => typeof v === 'number' ? String(+v.toPrecision(6)) : String(v == null ? '' : v);
  const chip = (v, title) => `<span class="vd ${v === 'none' ? 'record' : v}"${title ? ` title="${esc(title)}"` : ''}>${LABEL[v] || String(v || '').toUpperCase()}</span>`;
  const isTable = c => !!c && c.value && typeof c.value === 'object' && Array.isArray(c.value.rows);
  const clock = ts => ts ? esc(String(ts).replace('T', ' ').slice(11, 19)) : '';
  const ICON = '<svg class="insticon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M2 12h12M3 12V6m3 6V3m3 9V7m3 5V4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

  /* where a value came from: instrument (action plugin), voice, assistant, keyboard */
  function source(c) {
    const s = (c && c.source) || {};
    const t = clock(c && c.ts);
    if (s.kind === 'action') return `<span class="rsrc inst" title="Recorded from the instrument by ${esc(s.component)}, run ${esc(s.run_ref)}${c.auto ? ' (recorded automatically)' : ''}">${ICON}${esc(s.component || 'action')} &middot; ${t}</span>`;
    if (s.kind === 'voice' || s.input_method === 'voice') return `<span class="rsrc" title="Spoken by the operator">voice &middot; ${t}</span>`;
    if (s.kind === 'assistant') return `<span class="rsrc" title="Offered by the AI assistant, accepted by the operator">assistant &middot; ${t}</span>`;
    return `<span class="rsrc">${esc(s.kind || 'keyboard')} &middot; ${t}</span>`;
  }

  function valueText(c) {
    if (!c) return '';
    if (inRange(c)) return '✓ in range';
    if (isTable(c)) return `table &middot; ${c.value.rows.length} rows`;
    if (c.value == null) return c.status === 'skipped' ? 'skipped' : '';
    return `${esc(c.value)}${c.unit ? ' ' + esc(c.unit) : ''}`;
  }

  /* ---- table cell: compact table (out-of-band rows and cells marked) and a small line plot of the declared x/y pair */
  function plot(t, v, rv) {
    const p = t.plot; if (!p) return '';
    const xi = v.columns.indexOf(p.x), yi = v.columns.indexOf(p.y); if (xi < 0 || yi < 0 || v.rows.length < 2) return '';
    const pts = v.rows.map(r => [+r[xi], +r[yi]]).filter(q => isFinite(q[0]) && isFinite(q[1]));
    if (pts.length < 2) return '';
    const lim = (t.limits || {})[p.y] || {};
    const xs = pts.map(q => q[0]), ys = pts.map(q => q[1]).concat([lim.min, lim.max].filter(z => z != null));
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const W = 300, H = 90, L = 34, B = 14, T = 6, R = 6;
    const X = x => L + (x1 === x0 ? 0.5 : (x - x0) / (x1 - x0)) * (W - L - R), Y = y => T + (1 - (y1 === y0 ? 0.5 : (y - y0) / (y1 - y0))) * (H - T - B);
    const band = lim.min != null || lim.max != null ? `<rect class="pband" x="${L}" y="${Y(lim.max != null ? lim.max : y1)}" width="${W - L - R}" height="${Math.max(1, Y(lim.min != null ? lim.min : y0) - Y(lim.max != null ? lim.max : y1))}"/>` : '';
    const col = k => (v.columns.indexOf(k) >= 0 && (t.columns || []).find(c => c.id === k)) || { id: k };
    const cx = col(p.x), cy = col(p.y);
    return `<svg class="tplot" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(p.y)} against ${esc(p.x)}">${band}
      <polyline class="pline" fill="none" points="${pts.map(q => X(q[0]).toFixed(1) + ',' + Y(q[1]).toFixed(1)).join(' ')}"/>
      ${pts.map((q, i) => `<circle class="pdot${rv && rv[i] && rv[i].out && rv[i].out.length ? ' out' : ''}" cx="${X(q[0]).toFixed(1)}" cy="${Y(q[1]).toFixed(1)}" r="2.6"/>`).join('')}
      <text class="plab" x="${L - 3}" y="${T + 7}" text-anchor="end">${num(y1)}</text><text class="plab" x="${L - 3}" y="${H - B}" text-anchor="end">${num(y0)}</text>
      <text class="plab" x="${L}" y="${H - 2}">${num(x0)}</text><text class="plab" x="${W - R}" y="${H - 2}" text-anchor="end">${num(x1)} ${esc(cx.unit || '')}</text>
      <text class="plab" x="${L + 3}" y="${T + 7}">${esc(cy.title || p.y)}${cy.unit ? ' (' + esc(cy.unit) + ')' : ''}</text></svg>`;
  }
  function tableHtml(t, v, rv, tag) {
    if (!v || !Array.isArray(v.rows)) return '';
    const cols = v.columns.map(id => (t.columns || []).find(c => c.id === id) || { id });
    const head = cols.map(c => `<th>${esc(c.title || c.id)}${c.unit ? ` <span class="faint">${esc(c.unit)}</span>` : ''}</th>`).join('') + (rv ? '<th></th>' : '');
    const body = v.rows.map((r, i) => {
      const e = rv && rv[i] ? rv[i] : null;
      const cls = e && (e.verdict === 'fail' || e.verdict === 'review') ? ' ' + e.verdict : '';
      return `<tr class="${cls}">${r.map((x, j) => `<td class="${e && e.out && e.out.includes(v.columns[j]) ? 'out' : ''}">${esc(num(x))}</td>`).join('')}${rv ? `<td>${e && e.verdict !== 'none' ? chip(e.verdict) : ''}</td>` : ''}</tr>`;
    }).join('');
    return `<div class="tblwrap"><table class="rtable"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>${plot(t, v, rv)}${v.attachment ? `<div class="faint small">attachment: ${esc(v.attachment)}</div>` : ''}${tag || ''}`;
  }
  const cellTable = (t, c) => tableHtml(t, c.value, c.row_verdicts);

  /* ---- Anomalies strip (current board, current step) */
  const anomsFor = (st, b) => (S.anomalies || []).filter(a => a.step === st.id && a.board === b);
  function anomRow(a) {
    const bad = a.kind === 'fail' || a.kind === 'stop';
    let acts;
    if (a.kind === 'stop') acts = `<button class="sm" data-act="ackstop" data-id="${a.stop_id}">Acknowledge stop&hellip;</button>`;
    else acts = `<button class="sm" data-act="an-retest" data-b="${a.board}" data-step="${a.step}" data-id="${esc(a.test_id)}" title="${a.redo ? 'Run ' + esc(a.redo.title) + ' again on ' + a.board + '; the new reading replaces this one in the same attempt (the earlier value stays in the history)' : 'Clear the entry box and type this test again; the new value replaces this one in the same attempt (the earlier value stays in the history)'}">Redo</button>` +
      `<button class="sm" data-act="an-note" data-b="${a.board}" data-id="${esc(a.test_id)}" title="Add a note to this test (optional, never blocks)">Add note&hellip;</button>`;
    const label = a.kind === 'outside_guess' ? 'REVIEW' : a.kind === 'outside_tbr' ? 'TBR' : (LABEL[a.kind] || a.kind.toUpperCase());
    return `<div class="anrow ${bad ? 'bad' : 'warn'}" data-flash="${a.board}|${esc(a.test_id)}"><button class="anjump" data-act="test" data-id="${esc(a.test_id)}" title="Go to ${esc(a.test_id)}"><span class="vd ${a.kind === 'stop' || a.kind === 'fail' ? a.kind : 'review'}">${label}</span> <b class="mono">${esc(a.test_id)}</b> ${esc(a.title)}</button>
      <span class="anwhy">${esc(a.reason || '')}</span><span class="anacts">${acts}</span></div>`;
  }
  function strip(st, b) {
    const mine = anomsFor(st, b);
    const others = {};
    for (const a of S.anomalies || []) if (a.step === st.id && a.board !== b) others[a.board] = (others[a.board] || 0) + 1;
    const oth = Object.entries(others).map(([bb, n]) => `<button class="sm" data-act="board" data-id="${bb}">${bb} (${n})</button>`).join(' ');
    if (!mine.length && !oth) return '';
    return `<section class="anoms${mine.length ? '' : ' none'}" id="anoms" aria-label="Anomalies"><h3><span class="no ${mine.length ? 'bad' : ''}">${mine.length}</span>Anomalies on ${b}<span class="sp"></span>${oth ? `<span class="faint small">also on:</span> ${oth}` : ''}</h3>${mine.map(anomRow).join('') || '<div class="anrow ok">None on this board in this step.</div>'}</section>`;
  }

  /* ---- results of one board in one step: value, unit, limit, verdict chip, source */
  function results(st, b) {
    const rows = [];
    for (const sec of st.sections) {
      if (!secAp(st, sec, b)) continue;
      for (const t of sec.tests) {
        const c = cellOf(b, t.id);
        const prop = c && c.proposals && c.proposals[0];
        const settledC = c && (c.status === 'accepted' || c.status === 'skipped');
        let val, ver, src;
        if (settledC) {
          val = valueText(c); src = c.status === 'skipped' ? `<span class="faint">${esc(c.skip_reason || '')}</span>` : source(c);
          ver = c.status === 'skipped' ? '<span class="vd record">SKIPPED</span>' : chip(c.verdict, c.reason);
        } else if (prop) {
          val = isTable({ value: prop.value }) ? `table &middot; ${prop.value.rows.length} rows` : `${esc(prop.value)}${prop.unit ? ' ' + esc(prop.unit) : ''}`;
          ver = chip('proposed'); src = `<span class="rsrc">${esc((prop.source || {}).component || (prop.source || {}).kind || '')}</span>`;
        } else continue;
        rows.push(`<tr class="rrow${t.id === ui.test ? ' cur' : ''}" data-act="test" data-id="${esc(t.id)}" data-flash="${b}|${esc(t.id)}"><td class="mono">${esc(t.id)}</td><td class="rti">${esc(t.title)}</td>
          <td class="rv mono">${val}<div class="rsub">${src}</div></td><td class="rlim">${esc(t.expected || '')}</td><td>${ver}</td></tr>`);
      }
    }
    if (!rows.length) return '';
    return `<details class="box resbox" id="resbox" open><summary>Results for ${b}${(sb(st, b).attempt || 1) > 1 ? ' &middot; attempt ' + sb(st, b).attempt : ''} <span class="faint small">(${rows.length} recorded or proposed)</span></summary>
      <div class="tblwrap"><table class="restab"><thead><tr><th>ID</th><th>Test</th><th>Result</th><th class="rlim">Limit</th><th>Verdict</th></tr></thead><tbody>${rows.join('')}</tbody></table></div></details>`;
  }

  /* ---- the cell currently in focus: one large result */
  function recorded(t, c, b) {
    const how = c.auto ? ' <span class="autotag" title="An instrument reading is a result: it was recorded as soon as the action delivered it">recorded automatically</span>' : '';
    const bad = ['fail', 'review'].includes(c.verdict) || (c.flags || []).some(f => f.startsWith('outside_'));
    return `<div class="box recbox ${c.verdict === 'fail' ? 'bad' : bad ? 'warn' : ''}" data-flash="${b}|${t.id}"><h3>Recorded${c.attempt > 1 ? ' (attempt ' + c.attempt + ')' : ''}${how}<span class="sp"></span>${chip(c.verdict)}</h3>
      <div class="acc">${isTable(c) ? cellTable(t, c) : `<span class="v">${valueText(c)}</span>`}<span class="meta">${source(c)}${c.origin === 'simulated' ? ' &middot; SIMULATED' : ''}${c.reason && !isTable(c) ? ' &middot; ' + esc(c.reason) : ''}${isTable(c) && c.reason ? '<div>' + esc(c.reason) + '</div>' : ''}</span>
      </div></div>`;
  }

  /* ---- pending proposals: voice, assistant, or an action the protocol set to record: propose */
  function proposals(t, c, b) {
    const ps = c.proposals;
    return `<div class="box propbox"><h3><span class="no">${ps.length}</span>Proposed, not yet a result</h3>` + ps.map(p => {
      const bad = p.verdict === 'fail' || p.verdict === 'review';
      const who = p.source.kind === 'assistant' ? `<b>AI assistant</b> (${esc(p.source.client || '')}): ${esc(p.source.reason || '')}` : `${esc(p.source.component || p.source.kind)}${p.source.run_ref ? ' &middot; run ' + esc(p.source.run_ref) : ''}`;
      return `<div class="prop2"><div class="pbig">${p.origin === 'operator_confirmed' ? inRangeHtml() : (isTable({ value: p.value }) ? tableHtml(t, p.value, p.row_verdicts) : `<span class="v">${esc(p.value)} ${esc(p.unit || '')}</span>`)} ${chip(p.verdict)}</div>
        <div class="src">${who}${p.origin === 'simulated' ? ' &middot; SIMULATED' : ''}${p.reason ? ' &middot; ' + esc(p.reason) : ''}${p.raw && p.source.kind !== 'assistant' ? ' &middot; ' + esc(p.raw) : ''}</div>
        <div class="acts">${bad ? `<button class="outdanger" data-act="acceptprop" data-id="${p.id}" data-confirm="1">${p.verdict === 'fail' ? 'Record FAIL' : 'Accept for review'}</button>` : `<button class="primary" data-act="acceptprop" data-id="${p.id}">Accept <kbd style="color:inherit">Enter</kbd></button>`}
        <button data-act="rejectprop" data-id="${p.id}">Reject</button></div></div>`;
    }).join('') + `</div>`;
  }

  /* ---- flash: what an action just recorded. Compare event ids between two state snapshots. */
  let prev = null, flashKeys = [], flashT = null;
  function detect(next) {
    const now = {};
    for (const [k, c] of Object.entries(next.cells || {})) if (c.status === 'accepted') now[k] = c.event;
    if (prev && next.meta && prev.batch === next.meta.batch) {
      const hit = Object.entries(now).filter(([k, ev]) => prev.ev[k] !== ev && next.cells[k].auto).map(([k]) => k);
      if (hit.length) flashKeys = hit;
    }
    prev = { batch: next.meta && next.meta.batch, ev: now };
  }
  function applyFlash() {
    if (!flashKeys.length) return;
    const keys = flashKeys; flashKeys = [];
    let first = null;
    for (const k of keys) {
      const [b, tid] = [k.split('|')[0], k.split('|')[1]];
      for (const el of document.querySelectorAll('[data-flash]')) {
        if (el.dataset.flash === k || (el.classList.contains('trow') && el.dataset.id === tid)) { el.classList.add('flash'); first = first || el; }
      }
    }
    const c = document.querySelector('#centre'), r = document.querySelector('#resbox .rrow.flash'), anchor = r || first;
    if (document.querySelector('#anoms .anrow:not(.ok)') && c) c.scrollTop = 0;     // the Anomalies strip stays in view; it lists what was recorded
    else if (anchor && anchor.scrollIntoView) { try { anchor.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { anchor.scrollIntoView(); } }
    clearTimeout(flashT); flashT = setTimeout(() => document.querySelectorAll('.flash').forEach(x => x.classList.remove('flash')), 2600);
  }
  const anomSet = () => { const s = new Set(); for (const a of S.anomalies || []) s.add(a.board + '|' + a.test_id); return s; };

  return { chip, source, valueText, isTable, cellTable, tableHtml, strip, results, recorded, proposals, detect, applyFlash, anomSet, anomsFor };
})();
