'use strict';
/* Automated step (kind: automated): the main window shows the board's way through the whole sequence as a stage list, one Run for the
   board, the bench check as stage 1, an operator question as a large prompt, then one verdict. Presentation only: every verdict, the
   journal and the order of the stages come from the server (sequence.py); nothing here decides pass or fail. docs/AUTOMATED_STEP.md */
const AUTO = (() => {
  const U = { skips: {}, ticks: {}, bench: {}, img: {}, open: {} };
  try { U.skips = JSON.parse(localStorage.getItem('pb.auto.skips') || '{}') || {}; } catch (e) { U.skips = {}; }   // 19 Oct: a skip survives a reload
  const saveSkips = () => { try { localStorage.setItem('pb.auto.skips', JSON.stringify(U.skips)); } catch (e) {} };         // per step id: skips {equipment: reason}, ticked manual items, standalone check, final image choice, opened <details>
  const per = (o, id) => (o[id] = o[id] || {});
  const live = () => (S && S.sequence) || null;
  const recOf = (st, b) => { const r = (S.sequences || {})[b + '|' + st.id] || null; const x = r && sb(st, b);   // 19 Oct: a retest opens a new attempt; an older run's record is not shown
    return r && x && (r.attempt || 1) !== (x.attempt || 1) ? null : r; };
  const fmtS = s => { s = Math.max(0, Math.round(s)); return s < 60 ? s + ' s' : Math.floor(s / 60) + ' min ' + String(s % 60).padStart(2, '0') + ' s'; };
  const fmtAge = s => s < 90 ? 'a moment' : s < 5400 ? Math.round(s / 60) + ' min' : Math.round(s / 360) / 10 + ' h';
  const benchKept = st => { const k = (S.bench_cache || {})[st.id]; return k && !Object.keys(per(U.skips, st.id)).length ? k : null; };   // a kept pass, unless the operator chose to skip something
  const short = h => h ? h.slice(0, 12) + '…' : '';
  const titleOf = tid => { const i = testInfo(tid); return i ? i.t.title : tid; };

  /* ---- what the stages are */
  function planFor(st) {
    const hid = (st.sections || []).find(s => s.hidden) || { actions: [] };
    const acts = Object.fromEntries((hid.actions || []).map(a => [a.id, a]));
    const out = [];
    if ((st.equipment || []).length) out.push({ id: '_bench', kind: 'bench', title: 'Equipment and connections', state: 'pending', proposes: [] });
    for (const e of st.sequence || []) {
      if (typeof e === 'string') { const a = acts[e] || {}; out.push({ id: e, kind: 'action', title: a.title || e, final: !!a.final, needs: a.needs || [], proposes: a.proposes || [], state: 'pending' }); }
      else if (e && e.operator_check) { const o = e.operator_check; out.push({ id: o.id, kind: 'check', title: o.title || o.question, question: o.question, needs: o.needs || [], test: o.test, proposes: [o.test], state: 'pending' }); }
    }
    return out;
  }
  function stages(st, b) {
    const lv = live(), rec = recOf(st, b);
    const mine = lv && lv.step === st.id && lv.board === b;
    let items, mode;
    if (mine && lv.phase !== 'final') { items = lv.items.map(x => ({ ...x })); mode = 'live'; }
    else if (rec) {
      mode = 'done';
      const ended = Object.fromEntries((rec.items || []).map(i => [i.id, i]));
      items = (rec.plan || []).map(p => ({ ...p, ...(ended[p.id] || {}), state: (ended[p.id] || {}).state || (rec.outcome === 'running' ? 'pending' : p.final ? 'pending' : 'not_run') }));
      if (mine) { const f = items.find(i => i.final); if (f) Object.assign(f, { state: 'running', t0: lv.items[0].t0, dur_s: null, text: '' }); }   // UIR-01-R1: a final flash (or its retry) keeps the list on screen
      else if (rec.final) { const f = items.find(i => i.final); if (f) Object.assign(f, { state: rec.final.state === 'ok' ? 'ok' : rec.final.state === 'cancelled' ? 'cancelled' : 'error', dur_s: rec.final.dur_s, text: rec.final.text }); }
      else if (rec.defects) items.forEach(i => { if (i.final && i.state === 'pending') i.state = 'not_run'; });
      if (rec.outcome !== 'complete' && rec.outcome !== 'running') items.forEach(i => { if (i.final && i.state === 'pending') i.state = 'not_run'; });
    } else { items = planFor(st); mode = 'pre'; }
    // 19 Oct (maintainer): a run that stopped on an action error (probe not answering, adapter missing) is set-up, not a defect: no verdict, Run again
    if (mode === 'done' && rec && rec.outcome === 'error' && !rec.proceed && !rec.defects) mode = 'notready';
    // the pseudo-stage between the checks and the final flash
    const fi = items.findIndex(i => i.final);
    if (fi >= 0) {
      let state = 'pending';
      if (rec && rec.proceed) state = 'ok'; else if (rec && rec.defects) state = 'not_run';
      else if (mode === 'done' && rec && rec.outcome === 'complete') state = 'asking';
      else if (mode === 'done' && rec && rec.outcome !== 'running') state = 'not_run';
      items.splice(fi, 0, { id: '_ready', kind: 'ready', title: 'Ready for final image', state, proposes: [], text: state === 'ok' ? 'verdict accepted' : '' });
    }
    if (mode === 'pre' && items[0] && items[0].kind === 'bench') {
      const kept = benchKept(st);
      if (kept) Object.assign(items[0], { state: 'ok', text: `${kept.for_board === b ? '' : 'reused: '}checked ${fmtAge(kept.age_s)} ago`, reused: kept.for_board !== b }); else items[0].state = 'asking';   // UIR-01-R2
    }
    return { items, mode, lv: mine ? lv : null, rec };
  }

  /* ---- values of a test for this board */
  function cellInfo(b, tid) {
    const c = cellOf(b, tid);
    if (!c) return { value: '', verdict: '', status: 'empty' };
    if (c.status === 'accepted') return { value: RES.valueText(c), verdict: c.verdict, status: 'accepted' };
    if (c.status === 'skipped') return { value: 'skipped', verdict: 'skipped', status: 'skipped', why: c.skip_reason };
    const q = c.proposals[c.proposals.length - 1];
    return q ? { value: `${esc(q.value)}${q.unit ? ' ' + esc(q.unit) : ''}`, verdict: q.verdict, status: 'proposal' } : { value: '', verdict: '', status: 'empty' };
  }
  const chip = v => !v ? '' : v === 'skipped' ? '<span class="vd record">SKIPPED</span>' : RES.chip(v === 'invalid' ? 'invalid' : v);
  function shortErr(t) {                       // 19 Oct: one short line; the full text stays in the stage details and the journal
    const x = String(t).replace(/^\w*Error:\s*/, ''); const m = x.match(/^[^.]*\./); return (m ? m[0].slice(0, -1) : x).slice(0, 90);
  }
  function stageResult(it, b) {
    if (it.state === 'skipped') return it.text || 'skipped';
    if (it.state === 'error' || it.state === 'cancelled') return (it.state === 'cancelled' ? 'Aborted' : 'FAIL') + (it.text ? ': ' + shortErr(it.text) : '');
    if (it.state !== 'ok') return '';
    const cs = (it.proposes || []).map(t => ({ t, ...cellInfo(b, t) })).filter(x => x.status !== 'empty');
    if (!cs.length) return it.text || '';
    const bad = cs.filter(x => ['fail', 'review', 'invalid'].includes(x.verdict));
    if (bad.length) return 'FAIL: ' + bad.slice(0, 2).map(x => `${esc(titleOf(x.t))} ${x.value}`).join('; ') + (bad.length > 2 ? ` and ${bad.length - 2} more` : '');
    if (cs.every(x => x.verdict === 'none')) return cs.slice(0, 3).map(x => x.value).join(', ');
    const pass = cs.filter(x => x.verdict === 'pass' || x.verdict === 'none').length;
    return `${pass} of ${cs.length} checks passed`;
  }
  function detailRows(it, b) {
    const ts = it.proposes || [];
    if (!ts.length) return '';
    return `<table class="adet">` + ts.map(t => { const c = cellInfo(b, t); return `<tr><td class="mono">${esc(t)}</td><td>${esc(titleOf(t))}</td><td class="mono">${c.value}</td><td>${chip(c.verdict)}</td></tr>`; }).join('') + `</table>`;
  }

  /* ---- equipment */
  function equipRows(st, b, s) {
    const lv = s.lv, rec = s.rec;
    const results = (lv && lv.bench && lv.bench.results) || (rec && rec.bench && rec.bench.results) || ((S.bench_cache || {})[st.id] || {}).results || U.bench[st.id] || null;
    const resBy = Object.fromEntries((results || []).map(r => [r.id, r]));
    const frozen = s.mode !== 'pre' && !(lv && lv.ask && lv.ask.kind === 'bench');          // the run has started: no more skipping
    const skips = frozen ? ((lv && lv.skips) || (rec && rec.bench && rec.bench.skips) || {}) : per(U.skips, st.id);
    const ticks = per(U.ticks, st.id);
    const rows = (st.equipment || []).map(e => {
      const r = resBy[e.id] || null, sk = skips[e.id];
      const state = sk ? 'skipped' : !r ? 'unknown' : r.state === 'manual' ? (ticks[e.id] ? 'ok' : 'manual') : r.state;
      const blocking = !sk && r && (r.required !== false) && (r.state === 'missing' || (r.state === 'manual' && !ticks[e.id]));
      return { e, r, state, sk, blocking };
    });
    return { rows, results, frozen, blocked: rows.filter(x => x.blocking), checking: !!(lv && lv.bench && lv.bench.checking) };
  }
  const ICON = { ok: '✓', missing: '✕', skipped: '–', unknown: '?', manual: '☐', warn: '!' };
  function equipHtml(st, b, s) {
    const q = equipRows(st, b, s), open = per(U.open, st.id);
    return `<div class="equip">` + q.rows.map(({ e, r, state, sk }) => {
      const needs = (st.sections.find(x => x.hidden) || { actions: [] }).actions.filter(a => (a.needs || []).includes(e.id)).map(a => a.title);
      const conn = (e.connections || []).length ? `<table class="conn"><tr><th>From</th><th>To</th><th>Note</th></tr>${e.connections.map(c => `<tr><td>${esc(c.from)}</td><td>${esc(c.to)}</td><td class="muted">${esc(c.note || '')}</td></tr>`).join('')}</table>` : '';
      const det = conn || e.notes || e.image ? `<details class="edet" data-eq="${esc(e.id)}" ${open[e.id] ? 'open' : ''}><summary>How to connect</summary><div class="epur muted">${esc(e.purpose || (r && r.purpose) || '')}${needs.length ? ` &middot; used by ${esc(needs.join(', '))}` : ''}</div>${conn}${e.notes ? `<div class="enotes">${md(e.notes)}</div>` : ''}${e.image ? `<img class="eimg" alt="${esc(e.title || e.id)}" src="/api/sequence/asset?step=${encodeURIComponent(st.id)}&equipment=${encodeURIComponent(e.id)}">` : ''}</details>` : '';
      const res = sk ? `<span class="skipnote">Skipped: ${esc(sk)}</span>` : !r ? `<span class="faint">${q.checking ? 'checking…' : 'not checked'}</span>` : `<span class="${state === 'missing' ? 'bad' : ''}">${state === 'missing' ? 'MISSING: ' : ''}${esc(r.detail)}</span>${state === 'missing' && r.hint ? `<div class="faint small">${esc(r.hint)}</div>` : ''}`;
      const tick = r && r.state === 'manual' && !sk && !q.frozen ? `<label class="sw"><input type="checkbox" data-act="autotick" data-eq="${esc(e.id)}" ${state === 'ok' ? 'checked' : ''}> connected</label>` : '';
      const skipBtn = q.frozen ? '' : sk ? `<button class="sm" data-act="autounskip" data-eq="${esc(e.id)}">Un-skip</button>` : `<button class="sm" data-act="autoskip" data-eq="${esc(e.id)}">Skip</button>`;
      return `<div class="erow st-${state}"><span class="eic" aria-hidden="true">${ICON[state] || '?'}</span><div class="ebody"><div class="eh"><b>${esc(e.title || e.id)}</b>${e.required === false ? '<span class="faint small"> optional</span>' : ''}<span class="sp"></span>${tick}${skipBtn}</div>
        <div class="eres">${res}</div>${det}</div></div>`;
    }).join('') + `</div>`;
  }

  /* ---- the centre */
  const STATE_TXT = { pending: 'waiting', running: 'running', asking: 'needs you', ok: 'done', error: 'failed', cancelled: 'aborted', skipped: 'skipped', not_run: 'not run' };
  function stageHtml(st, b, s, it) {
    const lv = s.lv;
    const act = S.action && S.action.action === it.id && S.action.board === b ? S.action : null;
    let desc = '';
    if (it.state === 'running' && act) desc = esc(act.text || 'running') + (act.fraction > 0 && act.fraction < 1 ? ` &middot; ${Math.round(act.fraction * 100)} %` : '');
    else if (it.state === 'running' && it.kind === 'bench') desc = 'Checking the equipment, read only…';
    else if (it.state === 'asking' && it.kind === 'check') desc = 'Waiting for your answer';
    else if (it.state === 'asking' && it.kind === 'bench') desc = s.mode === 'pre' ? '' : 'Confirm the bench below';
    else if (it.state === 'asking' && it.kind === 'ready') desc = 'Your verdict decides: program the final image, or record defects';
    else if (it.state === 'pending') desc = '';
    else if (it.state === 'not_run') desc = '';
    const result = it.kind === 'bench' && it.state === 'ok' ? esc(it.text || '') : stageResult(it, b);
    const resCls = /^FAIL|^Aborted/.test(result) ? ' bad' : it.state === 'ok' ? ' good' : '';
    const t = it.state === 'running' && it.t0 ? `<span class="el" data-t0="${it.t0}"></span>` : it.dur_s != null && it.state !== 'pending' ? `<span class="el faint">${fmtS(it.dur_s)}</span>` : '';
    const det = it.kind === 'bench' ? (s.mode === 'pre' || it.state === 'asking' || it.state === 'running' || (lv && lv.ask && lv.ask.kind === 'bench') ? '' : `<details class="sdet" data-st="_bench"><summary>details</summary>${equipHtml(st, b, s)}</details>`)
      : (it.proposes || []).length && it.state !== 'pending' && it.state !== 'not_run' ? `<details class="sdet" data-st="${esc(it.id)}" ${per(U.open, st.id)['s:' + it.id] ? 'open' : ''}><summary>details</summary>${detailRows(it, b)}</details>` : '';
    const showEq = it.kind === 'bench' && (s.mode === 'pre' || it.state === 'asking' || it.state === 'running');
    const benchAsk = it.kind === 'bench' && lv && lv.ask && lv.ask.kind === 'bench';
    const q = showEq ? equipRows(st, b, s) : null;
    const bar = benchAsk ? `<div class="benchbar">${q.blocked.length ? `<span class="bad">Not ready: ${esc(q.blocked.map(x => x.e.title || x.e.id).join(', '))}. Fix it and check again, or skip it with a reason.</span>` : '<span class="good">Every item is ready or skipped.</span>'}<span class="sp"></span>${q.blocked.length ? `<button class="primary big" data-act="autoskipstart">Skip ${esc(q.blocked.map(x => x.e.title || x.e.id).join(', '))} and start</button>` : ''}<button data-act="autobench" data-do="recheck">Check again</button><button class="primary big" data-act="autobench" data-do="confirm" ${q.blocked.length ? 'disabled' : ''}>Bench ready, start the protocol</button></div>`
      : it.kind === 'bench' && s.mode === 'pre' ? `<div class="benchbar">${benchKept(st) ? `<span class="good small">Passed ${fmtAge(benchKept(st).age_s)} ago, ${benchKept(st).for_board === b ? 'used' : 'reused'} for this run. Checked again if a device is plugged or unplugged, an action reports an error, or after ${fmtAge(benchKept(st).window_s)}.</span>` : ''}<span class="sp"></span><button data-act="autocheck" title="Read only: nothing is attached, reset or transmitted">${benchKept(st) ? 'Check again' : 'Check equipment'}</button></div>` : '';
    const cls = ['stage', 'st-' + it.state, it.kind, s.cur === it.id ? 'cur' : ''].join(' ');
    return `<li class="${cls}" data-stage="${esc(it.id)}"><span class="sic" title="${STATE_TXT[it.state] || ''}"><i></i></span><div class="sbody">
      <div class="sh"><b>${esc(it.title)}</b><span class="sst">${STATE_TXT[it.state] || ''}</span><span class="sp"></span>${t}</div>
      ${desc ? `<div class="sdesc">${desc}</div>` : ''}${result ? `<div class="sres${resCls}">${result}</div>` : ''}
      ${showEq ? equipHtml(st, b, s) : ''}${bar}${det}</div></li>`;
  }
  function header(st, b, s) {
    const board = S.boards.find(x => x.label === b) || {};
    const ident = (st.identity || []).map(t => cellInfo(b, t)).filter(x => x.status !== 'empty').map(x => x.value).join(' &middot; ');
    const lv = s.lv, rec = s.rec;
    let state = 'Not started', cls = '';
    if (lv && lv.ask) { state = lv.ask.kind === 'check' ? 'Needs you: question' : 'Needs you: bench'; cls = 'warn'; }
    else if (lv) { state = lv.phase === 'final' ? 'Programming the final image' : 'Running'; cls = 'run'; }
    else if (rec) {
      if (rec.proceed) state = rec.final && rec.final.state !== 'ok' ? 'Final image failed' : 'Passed and accepted', cls = rec.final && rec.final.state !== 'ok' ? 'bad' : 'good';
      else if (rec.defects) state = 'Defects recorded', cls = 'bad';
      else if (rec.outcome === 'complete') { const v = (S.verdicts || {})[b + '|' + st.id]; state = v && v.summary.ok ? 'Verdict ready: all passed' : 'Verdict ready: failures'; cls = v && v.summary.ok ? 'good' : 'bad'; }
      else if (rec.outcome !== 'running') state = rec.outcome === 'error' ? 'Not ready' : 'Stopped: ' + shortErr((rec.stop || {}).reason || rec.outcome), cls = 'bad';
    }
    const t0 = lv ? lv.t0 : null;
    const total = !lv && rec && rec.items && rec.items.length ? fmtS(rec.items.reduce((a, i) => a + (i.dur_s || 0), 0)) : '';
    return `<div class="ahead"><div class="abd"><span class="ab">${esc(b)}</span>${board.serial ? `<span class="muted">serial ${esc(board.serial)}</span>` : ''}${ident ? `<span class="mono muted">${ident}</span>` : ''}</div>
      <span class="astate ${cls}">${esc(state)}</span><span class="sp"></span>${t0 ? `<span class="muted">elapsed <b class="el" data-t0="${t0}"></b></span>` : total ? `<span class="muted">total ${total}</span>` : ''}</div>`;
  }

  /* ---- operator question: the centre */
  function askBig(lv) {
    const a = lv.ask;
    return `<div class="askbig" data-asking="1"><div class="aq">${esc(a.question)}</div>
      <div class="abtns"><button class="primary huge" data-act="autoans" data-v="yes"><kbd>Y</kbd> Yes</button><button class="danger huge" data-act="autoans" data-v="no"><kbd>N</kbd> No</button></div>
      <div class="atype"><input id="autoans" placeholder="Enter = yes, or type n and Enter" autocomplete="off"><input id="autonote" placeholder="Note, if the answer is no (optional)" autocomplete="off"></div>
      <div class="faint small">A no is recorded as a FAIL. Nothing is accepted for you. You can also say yes or no.</div></div>`;
  }

  /* ---- verdict */
  function imageChoice(st) {
    const imgs = st.final_images || [], ch = per(U.img, st.id);
    if (!ch.source) { const di = Math.max(0, imgs.findIndex(i => i.default)); Object.assign(ch, { source: 'protocol', index: di }); }
    const opts = imgs.map((i, k) => `<option value="${k}" ${ch.source === 'protocol' && ch.index === k ? 'selected' : ''}>${esc(i.label)}${i.default && !/\(default\)/i.test(i.label) ? ' (default)' : ''}</option>`).join('');
    const pv = ch.source === 'file' && ch.preview;
    const filebit = pv ? `<div class="imgfile"><b>${esc(pv.name)}</b> <span class="mono muted">${short(pv.sha256)}</span> ${pv.match ? `<span class="good">matches manifest ${esc(pv.manifest_label)}</span>` : `<span class="bad">not in manifest</span>`}
      ${pv.warning ? `<div class="bad small">${esc(pv.warning)}</div>` : ''}${pv.match ? '' : `<input id="imgreason" placeholder="Reason for using a file that is not in the manifest" value="${esc(ch.reason || '')}" autocomplete="off">`}</div>` : '';
    return `<div class="imgpick"><label class="muted">Final image</label><select id="imgsel" ${ch.source === 'file' ? 'class="off"' : ''}>${opts}${ch.source === 'file' ? '<option selected>file chosen below</option>' : ''}</select><button class="sm" data-act="autoopenimg">Open image…</button>${filebit}</div>`;
  }
  function verdictHtml(st, b, s) {
    const v = (S.verdicts || {})[b + '|' + st.id], rec = s.rec;
    if (!v) return '';
    const sm = v.summary, complete = rec.outcome === 'complete';
    const dis = S.action ? ' disabled title="Something else is running: wait for it or abort it"' : '';
    const order = { fail: 0, invalid: 0, review: 1 };
    const rows = [...v.rows].sort((x, y) => (x.clean ? 1 : 0) - (y.clean ? 1 : 0) || (order[x.verdict] ?? 3) - (order[y.verdict] ?? 3));
    const lab = ((st.final_images || []).find(i => i.default) || {}).label || 'final image';
    const failNames = sm.failed_names.map(n => `<li>${esc(n)}</li>`).join('');
    const stopped = !complete ? `<div class="stoptxt">The run stopped: ${esc((rec.stop || {}).reason || rec.outcome)}. The remaining stages did not run.</div>` : '';
    const main = complete && sm.ok && v.has_final ? `${imageChoice(st)}<button class="primary huge wide" data-act="autoproceed"${dis}>Program final image: ${esc(lab)}</button>`
      : complete && sm.ok ? `<button class="primary huge wide" data-act="autoproceed"${dis}>Accept the results</button>` : '';
    const sec = [];
    if (complete && !sm.ok) sec.push(`<button data-act="autoproceed" data-confirm="1"${dis}>Accept and proceed anyway…</button>`);
    sec.push(`<button class="${sm.ok && complete ? '' : 'primary big'}" data-act="autodefects"${dis}>Record defects…</button>`);
    if ((st.sections || []).some(x => x.diagnose)) sec.push(`<button data-act="autodiag">Diagnose this board</button>`);
    sec.unshift(`<button class="${sm.ok && complete ? '' : 'primary big'}" data-act="autoretest">Retest ${esc(b)}</button>`);
    const tbl = rows.map(r => `<tr class="${r.clean || r.final || r.src === 'skipped' ? '' : 'badrow'}"><td class="mono">${esc(r.test)}</td><td>${esc(r.title)}</td><td class="mono">${r.src === 'none' ? '<span class="faint">no result</span>' : r.src === 'skipped' ? '<span class="faint">skipped</span>' : (r.value == null ? '' : esc(r.value) + (r.unit ? ' ' + esc(r.unit) : ''))}</td><td>${r.src === 'skipped' ? chip('skipped') : r.src === 'none' ? (r.final ? '<span class="faint">after the final flash</span>' : chip('invalid')) : chip(r.verdict)}</td></tr>`).join('');
    return `<div class="verdict ${sm.ok && complete ? 'allgood' : 'hasbad'}"><div class="vsum">${esc(sm.text)}</div>${failNames ? `<ul class="vfail">${failNames}</ul>` : ''}${stopped}
      ${st.verdict_prompt ? `<div class="muted vprompt">${esc(st.verdict_prompt)}</div>` : ''}
      <div class="vact">${main}<div class="vsec">${sec.join('')}</div></div></div>
      <table class="vtab"><thead><tr><th>Test</th><th></th><th>Value</th><th>Verdict</th></tr></thead><tbody>${tbl}</tbody></table>`;
  }
  function recordedHtml(st, b, s) {
    const rec = s.rec; if (!rec || !(rec.proceed || rec.defects)) return '';
    const fin = (st.sections || []).some(x => x.hidden && (x.actions || []).some(a => a.final));
    const im = rec.image;
    const finFailed = !!(rec.proceed && rec.final && rec.final.state !== 'ok');
    let h = `<div class="verdict ${rec.defects || finFailed ? 'hasbad' : 'allgood'}">`;
    if (rec.defects) h += `<div class="vsum">Defects recorded</div><div>${esc(rec.defects.note)}</div><div class="muted small">${(rec.defects.tests || []).length ? 'Failing: ' + esc(rec.defects.tests.join(', ')) + '. ' : ''}${rec.defects.not_run ? rec.defects.not_run + ' tests not run. ' : ''}Nothing was flashed.</div>`;
    else {
      h += `<div class="vsum">${rec.final && rec.final.state !== 'ok' ? 'Accepted. The final image was not programmed' : 'Accepted' + (im ? ' and programmed' : '')}</div>`;
      if (rec.final && rec.final.state !== 'ok') h += `<div class="bad">The final flash failed: ${esc(shortErr(rec.final.text || rec.final.state))}.</div><details class="sdet"><summary>details</summary><pre class="mono small" style="white-space:pre-wrap">${esc(rec.final.text || rec.final.state)}</pre></details>`;
    }
    if (im) h += `<div class="imgrec"><b>${esc(im.label)}</b> <span class="mono muted">${short(im.sha256)}</span> <span class="muted small">${im.source === 'file' ? 'opened from a file' : 'from the protocol'}${im.in_manifest ? `, matches manifest ${esc(im.manifest)}` : im.source === 'file' ? ', not in the manifest: ' + esc(im.reason) : ''}</span></div>`;
    const failed = !!(rec.proceed && rec.final && rec.final.state !== 'ok' && fin);
    if (failed && !s.lv) {
      const busy = !!S.action;
      h += `<div class="vact">${imageChoice(st)}<button class="primary huge wide" data-act="autoretryfinal" ${busy ? 'disabled title="Something else is running"' : ''}>Retry final flash on ${esc(b)}</button>
        <div class="muted small">Only the final flash runs again. The accepted results and the attempt number stay. Fix the cause first (probe plugged in, no other program using it).</div>
        <div class="vsec"><button data-act="autoretest" title="Start a whole new attempt: equipment, identify, self-test and every result again">Retest ${esc(b)} from the start</button></div></div></div>`;
    } else if (!s.lv) h += `<div class="vact"><button class="primary huge wide" data-act="autoretest">Retest ${esc(b)}</button></div></div>`;
    else h += `</div>`;
    return h;
  }
  function centre(el, st) {
    const b = ui.board, s = stages(st, b);
    const cur = s.items.find(i => ['running', 'asking'].includes(i.state));
    s.cur = cur ? cur.id : null;
    if (s.mode === 'pre' && s.items[0] && s.items[0].kind === 'bench') s.cur = '_bench';
    const chips = S.boards.map(x => { const sb_ = sb(st, x.label); if (!sb_ || sb_.state === 'na') return ''; const l = live(); const run = l && l.step === st.id && l.board === x.label;
      const c = run ? 'proposed' : sb_.state === 'done' ? (sb_.fails ? 'fail' : 'pass') : sb_.state === 'attention' ? 'fail' : sb_.state === 'partial' ? 'proposed' : 'empty';
      return `<button class="bchip${x.label === b ? ' cur' : ''}" data-act="board" data-id="${x.label}"><i class="dot s-${c}"></i>${x.label}${(sb_.attempt || 1) > 1 ? `<span class="rr">R${sb_.attempt}</span>` : ''}</button>`; }).join('');
    const lv = s.lv, busy = !!S.action || !!live();
    const canRun = !busy && !(s.rec && s.mode === 'done' && !(s.rec.proceed || s.rec.defects) && s.rec.outcome !== 'running') && !(s.rec && (s.rec.proceed || s.rec.defects)) && sb(st, b) && sb(st, b).gate.ok;
    const nr = s.mode === 'notready' ? `<div class="notready"><b>Not ready:</b> ${esc(shortErr((s.rec.stop || {}).reason || 'a stage could not run'))}. Fix it and press Run again. Nothing was recorded as a failure.</div>` : '';
    const runBtn = s.mode === 'pre' || s.mode === 'notready' || (s.rec && s.rec.outcome !== 'running' && !s.rec.proceed && !s.rec.defects && false)
      ? `<div class="runbar"><button class="primary huge" data-act="autorun" ${canRun ? '' : 'disabled'}>${s.mode === 'notready' ? 'Run again on ' + esc(b) : 'Run ' + esc(st.title.toLowerCase()) + ' on ' + esc(b)}</button>${!sb(st, b).gate.ok ? `<span class="bad small">${esc(sb(st, b).gate.reasons.join('; '))}</span>` : busy ? `<span class="muted small">${live() ? 'A sequence is running.' : 'Another action is running: wait for it or abort it.'}</span>` : ''}<span class="sp"></span>${(st.sections || []).some(x => x.diagnose) ? '<button data-act="autodiag">Diagnose…</button>' : ''}</div>`
      : lv ? `<div class="runbar"><span class="muted">${lv.phase === 'final' ? 'Programming the final image' : 'Running the sequence'}…</span><span class="sp"></span><button class="danger" data-act="abort">Abort</button></div>` : '';
    const stagesHtml = s.items.map(it => stageHtml(st, b, s, it)).join('');
    el.innerHTML = `<div class="bar2"><span class="lbl">Board</span>${chips}</div><div class="pane auto">${header(st, b, s)}
      ${lv && lv.ask && lv.ask.kind === 'check' ? askBig(lv) : ''}${nr}${runBtn}
      ${s.mode === 'done' && s.rec && !(s.rec.proceed || s.rec.defects) && s.rec.outcome !== 'running' ? verdictHtml(st, b, s) : ''}${recordedHtml(st, b, s)}
      <ol class="stages${lv && lv.ask && lv.ask.kind === 'check' ? ' dim' : ''}">${stagesHtml}</ol></div>`;
    if (s.mode === 'done' && s.rec && !(s.rec.proceed || s.rec.defects)) {          // retest is offered once the run has ended
      const pane = $('.pane.auto', el); if (pane && s.rec.outcome !== 'running') pane.insertAdjacentHTML('afterbegin', '');
    }
    // app.js routes only some data-act names to AUTO.act ('autoskipstart' was never routed: the button did nothing, UIR-01-R4). These are wired here.
    el.querySelectorAll('[data-act=autoskipstart],[data-act=autoretryfinal]').forEach(btn => btn.addEventListener('click', e => { e.stopPropagation(); if (!btn.disabled) act({ act: btn.dataset.act }, st); }));
    const sel = $('#imgsel'); sel && sel.addEventListener('change', () => { const c = per(U.img, st.id); c.source = 'protocol'; c.index = +sel.value; render(); });
    const ir = $('#imgreason'); ir && ir.addEventListener('input', () => { per(U.img, st.id).reason = ir.value; });
    const ai = $('#autoans');
    if (ai) { ai.focus(); ai.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); const v = ai.value.trim().toLowerCase(); answer(/^(n|no)$/.test(v) ? 'no' : 'yes'); } }); }
    el.querySelectorAll('details.edet').forEach(d => d.addEventListener('toggle', () => { per(U.open, st.id)[d.dataset.eq] = d.open; }));
    el.querySelectorAll('details.sdet').forEach(d => d.addEventListener('toggle', () => { per(U.open, st.id)['s:' + d.dataset.st] = d.open; }));
    const c = $('.stage.cur', el); if (c && !$('.verdict', el)) c.scrollIntoView({ block: 'nearest' });
    tick();
  }
  function tick() {
    document.querySelectorAll('.el[data-t0]').forEach(n => { n.textContent = fmtS(Date.now() / 1000 - +n.dataset.t0); });
  }
  setInterval(tick, 1000);

  function left(el, hdr, st) {
    let html = hdr(st.title, `${st.settled}/${st.applicable}`);
    html += S.boards.map(b => { const x = sb(st, b.label); if (!x || x.state === 'na') return '';
      const l = live(), run = l && l.step === st.id && l.board === b.label, rec = recOf(st, b.label);
      const t = run ? 'running' : x.state === 'done' ? (x.fails ? 'accepted with FAIL' : 'passed') : rec && rec.defects ? 'defects recorded' : rec && rec.outcome && rec.outcome !== 'running' ? 'verdict waiting' : x.state === 'empty' ? 'not run' : x.state;
      return `<div class="trow${b.label === ui.board ? ' cur' : ''}" data-act="board" data-id="${b.label}"><span class="id">${b.label}</span><span class="ti">${esc(b.serial) || '<span class="faint">no serial</span>'}</span><span class="dots faint">${esc(t)}</span></div>`; }).join('');
    el.innerHTML = html;
  }

  /* ---- actions */
  async function answer(v) {
    const note = ($('#autonote') || {}).value || '';
    const r = await api('/api/sequence/answer', { value: v, note, via: 'keyboard' });
    if (!r.ok) toast(r.data.error, true);
    await refresh();
  }
  function image(st) {
    const c = per(U.img, st.id);
    if (c.source === 'file') return { source: 'file', path: c.path, reason: c.reason || '' };
    return { source: 'protocol', index: c.index };
  }
  async function act(d, st) {
    const b = ui.board;
    switch (d.act) {
      case 'autoskip': {
        const e = (st.equipment || []).find(x => x.id === d.eq);
        const v = await dialog({ title: 'Skip ' + (e.title || e.id), text: 'Every action and test that needs it is skipped and recorded as skipped with your reason. You can un-skip it until the run starts.', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Skip' });
        if (v) { per(U.skips, st.id)[d.eq] = v.reason; saveSkips(); render(); }
        break; }
      case 'autounskip': delete per(U.skips, st.id)[d.eq]; saveSkips(); render(); break;
      case 'autotick': per(U.ticks, st.id)[d.eq] = !!(document.querySelector(`[data-act=autotick][data-eq="${d.eq}"]`) || {}).checked; render(); break;
      case 'autocheck': { toast('Checking the equipment…'); const r = await api('/api/sequence/check', { step: st.id, board: b, ticked: Object.keys(per(U.ticks, st.id)).filter(k => per(U.ticks, st.id)[k]) }); if (r.ok) { U.bench[st.id] = r.data.results; await refresh(); } else toast(r.data.error, true); break; }
      case 'autorun': {
        let r = await api('/api/sequence/run', { step: st.id, board: b, confirmed: false, fresh_bench: Object.keys(per(U.skips, st.id)).length > 0 });
        if (r.ok && r.data.needs_prompt) {
          const v = await dialog({ title: 'Before you start', text: r.data.needs_prompt.join('  /  '), ok: 'Done, start' });
          if (!v) return; r = await api('/api/sequence/run', { step: st.id, board: b, confirmed: true, fresh_bench: Object.keys(per(U.skips, st.id)).length > 0 });
        }
        if (!r.ok) toast(r.data.error, true);
        await refresh(); break; }
      case 'autoskipstart': {                    // 19 Oct: one click; the missing items are skipped for this run and the protocol starts
        const lv0 = live(); const miss = (lv0 && lv0.ask && lv0.ask.missing) || [];
        const names = miss.map(id => ((st.equipment || []).find(x => x.id === id) || { title: id }).title || id).join(', ');
        const need = miss.filter(id => !per(U.skips, st.id)[id]);
        let why = '';
        if (need.length) {                       // skipping equipment is journaled: it needs a reason
          const v = await dialog({ title: 'Skip ' + names + ' and start', text: 'Every action and test that needs it is skipped and recorded as skipped with your reason.', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Skip and start' });
          if (!v) return; why = v.reason;
        }
        const sk = per(U.skips, st.id); miss.forEach(id => { sk[id] = sk[id] || why; }); saveSkips();
        const r = await api('/api/sequence/bench', { action: 'confirm', skips: { ...sk }, ticked: Object.keys(per(U.ticks, st.id)).filter(k => per(U.ticks, st.id)[k]) });
        if (!r.ok) toast(r.data.error, true);
        await refresh(); break; }
      case 'autobench': {
        const r = await api('/api/sequence/bench', { action: d.do, skips: { ...per(U.skips, st.id) }, ticked: Object.keys(per(U.ticks, st.id)).filter(k => per(U.ticks, st.id)[k]) });
        if (!r.ok) toast(r.data.error, true);
        await refresh(); break; }
      case 'autoans': answer(d.v); break;
      case 'autoopenimg': {
        const im = (st.final_images || [])[0];
        fsbOpen('image', im ? im.path.replace(/\/[^/]*$/, '') : '', 'Choose the .hex, .bin or .elf to program. Its SHA-256 is checked against the manifest.'); break; }
      case 'autoproceed': {
        let r = await api('/api/sequence/proceed', { step: st.id, board: b, confirm: !!d.confirm, image: image(st) });
        if (!r.ok && r.data.needs_confirm) {
          const names = (r.data.tests || []).map(t => `${t} ${titleOf(t)}`).join('; ');
          const v = await dialog({ title: 'Accept the failures?', text: `${names}. Accepting records them as FAIL or REVIEW and then programs the final image if one is set.`, ok: 'Accept and proceed', danger: true });
          if (!v) return; r = await api('/api/sequence/proceed', { step: st.id, board: b, confirm: true, image: image(st) });
        }
        if (!r.ok) toast(r.data.error, true);
        await refresh(); break; }
      case 'autoretryfinal': {                   // UIR-01-R1: only the final flash again; accepted results and the attempt stay
        const r = await api('/api/sequence/retry_final', { step: st.id, board: b, image: image(st) });
        if (!r.ok) toast(r.data.error, true);
        await refresh(); break; }
      case 'autodefects': {
        const v0 = (S.verdicts || {})[b + '|' + st.id]; const bad = v0 ? v0.rows.filter(r => !r.final && (r.src === 'none' || !r.clean)).map(r => `${r.test} ${r.title}${r.value != null ? ' = ' + r.value : ''}`) : [];
        const v = await dialog({ title: 'Record defects on ' + b, text: (bad.length ? 'Failing: ' + bad.join('; ') + '. ' : '') + 'The results are recorded, with your note. The final image is not programmed.', fields: [{ name: 'note', label: 'What is wrong', area: true, required: true }], ok: 'Record defects' });
        if (!v) return;
        const r = await api('/api/sequence/defects', { step: st.id, board: b, note: v.note });
        if (!r.ok) toast(r.data.error, true);
        await refresh(); break; }
      case 'autoretest': {                       // 19 Oct: one click, no dialog
        const r = await cmd('retest', { board: b, step: st.id, cause: 'operator retest' });
        if (r && r.ok) { ui.test = null; await refresh(); } break; }
      case 'autodiag': ui.diag = ui.diag || {}; ui.diag[st.id] = true; ui.test = null; render(); break;
      case 'autoback': ui.diag = ui.diag || {}; ui.diag[st.id] = false; ui.test = null; render(); break;
    }
  }
  async function imagePicked(path) {
    const st = curStep(); const r = await api('/api/sequence/image?step=' + encodeURIComponent(st.id) + '&path=' + encodeURIComponent(path));
    if (!r.ok) { toast(r.data.error, true); return; }
    Object.assign(per(U.img, st.id), { source: 'file', path: r.data.path, preview: r.data, reason: '' });
    render();
  }
  return { centre, left, act, imagePicked, U };
})();
