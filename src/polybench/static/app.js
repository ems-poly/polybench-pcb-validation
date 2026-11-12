'use strict';
/* Polybench DV front end. Plain JS, no build step. The server is the source of truth: every action is a
   command that is written to the journal first; the screen is redrawn from /api/state afterwards. */

const KINDS = ['project', 'setup', 'assembly', 'visual', 'unpowered', 'powered', 'function', 'automated', 'characterisation', 'report'];
const DESIGN = ['Schematic', 'BOM review', 'Test plan'];
let S = null;                       // last server snapshot; null while no project is open
let NP = null;                      // the no-project state ({no_project, workspace, plugins}) while S is null: the welcome page
/* Operator-facing name of the active ECAD program (KC-43): the session state, else the scanner state, else Fusion. */
function ecadName() { try { return (S && S.ecad_program) || (sc && sc.st && sc.st.program) || 'Fusion'; } catch { return (S && S.ecad_program) || 'Fusion'; } }
const consoleLines = [];
let journalRows = [];
const ui = {
  step: null, board: null, test: null, line: null, pv: null, armed: null, draft: '', scanMsg: null,
  bottomTab: 'console', bottomOpen: false, secOpen: {}, hlTest: null, setupFocused: null, leftOpen: true, rightOpen: true, advanced: false, advance: 'test',
  gate: null, retired: null, theme: 'light', gridOpen: false, askKey: null, reportHtml: null, reportMsg: '', connected: true,
};
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const lsGet = (k, d) => { try { const v = localStorage.getItem('pbdv.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem('pbdv.' + k, JSON.stringify(v)); } catch { /* private mode */ } };

function md(s) {
  if (!s) return '';
  return String(s).split(/\n\s*\n/).map(p => {
    const lines = p.split('\n');
    if (lines.every(l => /^\s*[-*] /.test(l))) return '<ul>' + lines.map(l => '<li>' + inl(l.replace(/^\s*[-*] /, '')) + '</li>').join('') + '</ul>';
    return '<p>' + inl(p) + '</p>';
  }).join('');
  function inl(t) { return esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`(.+?)`/g, '<code>$1</code>'); }
}

/* With no project open only these endpoints answer; every other call is refused here without a request (the server also says 409). */
const NP_OK = /^\/api\/(state|console|session|events|workspace|fs\/list|project\/(inspect|protocol_design|new|open)|recent\/remove|plugins|demo\/)/;
let stateReady = null;
const stateFirst = new Promise(res => { stateReady = res; });           // settles when the first /api/state answer says whether a project is open
async function api(path, body, retried) {
  if (!S && !NP_OK.test(path)) { await stateFirst; if (!S) return { ok: false, status: 409, data: { error: 'no project is open', no_project: true } }; }
  try {
    const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let data = {}; try { data = await r.json(); } catch { /* empty */ }
    if (r.status === 403 && data.operator_only && /operator session/.test(data.error || '') && !retried && body !== undefined) {   // stale cookie after a restart: refused, nothing ran
      try { await fetch('/api/session', { cache: 'no-store' }); } catch { /* offline */ }
      return api(path, body, true);
    }
    return { ok: r.ok, status: r.status, data };
  } catch (e) { return { ok: false, status: 0, data: { error: 'server not reachable' } }; }
}
async function cmd(name, body, quiet) {
  const r = await api('/api/cmd/' + name, body);
  if (!r.ok && !quiet && !r.data.needs_confirm && !r.data.needs_replace) toast(r.data.error || 'command failed', true);
  if (r.ok) await refresh();
  return r;
}
let toastT;
/* A toast covers as little as possible. It takes the spot (bottom centre, bottom left, bottom right, top centre) with the least overlap
   on buttons and inputs, and an overlap on the control a message names (Accept for review, Record FAIL, Abort, a dialog's buttons)
   weighs 1000 times more, so those stay visible and clickable. */
const TOAST_KEEP_CLEAR = 'button[data-act=entryconfirm],button[data-act=batchconfirm],button[data-act=acceptprop],[data-act=abort],.dlg button,.dlg input';
function placeToast(t) {
  const box = e => e.getBoundingClientRect();
  const rects = (sel, w) => [...document.querySelectorAll(sel)].map(box).filter(r => r.width && r.height).map(r => ({ r, w }));
  const keep = rects(TOAST_KEEP_CLEAR, 1000).concat(rects('button,input,textarea,select', 1));
  let best = null;
  for (const spot of ['bc', 'bl', 'br', 'tc']) {
    t.dataset.spot = spot;
    const r = box(t);
    const cost = keep.reduce((sum, k) => {
      const w = Math.min(r.right, k.r.right) - Math.max(r.left, k.r.left), h = Math.min(r.bottom, k.r.bottom) - Math.max(r.top, k.r.top);
      return w > 0 && h > 0 ? sum + k.w * w * h : sum;
    }, 0);
    if (!best || cost < best.cost) best = { spot, cost };
    if (cost === 0) break;
  }
  t.dataset.spot = best.spot;
}
let toastStep = null;
function clearToast() { clearTimeout(toastT); const t = $('#toast'); if (t) t.style.display = 'none'; toastStep = null; }
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = err === 'warn' ? 'warn' : err ? 'err' : ''; t.style.display = 'block'; toastStep = ui.step; placeToast(t);
  clearTimeout(toastT); toastT = setTimeout(() => t.style.display = 'none', err ? 5000 : 2200);
}

/* ------------------------------------------------------------------ model helpers */
/* design header text: NAME \u00b7 VARIANT rev R (variant and revision are optional, separate fields) */
const designLabel = d => d.name ? [d.name + (d.variant ? ' \u00b7 ' + d.variant : ''), d.revision ? 'rev ' + d.revision : ''].filter(Boolean).join(' ') : [d.variant ? '\u00b7 ' + d.variant : '', d.revision ? 'rev ' + d.revision : ''].filter(Boolean).join(' ');
const stepOf = id => id === 'testplan' ? TP_STEP : S.steps.find(s => s.id === id);
const curStep = () => stepOf(ui.step);
const asmSplit = st => { const all = st.sections || []; return [all.filter(x => x.position === 'before_placement'), all.filter(x => x.position !== 'before_placement')]; };
const orderedSecs = st => st.kind === 'assembly' ? asmSplit(st).flat() : (st.sections || []);       // the order the left list draws them in
const testsOf = st => orderedSecs(st).filter(sec => st.kind !== 'automated' || (!sec.hidden && !!sec.diagnose === !!(ui.diag || {})[st.id])).flatMap(sec => sec.tests.map(t => ({ t, sec })));
const cellOf = (b, tid) => S.cells[b + '|' + tid] || null;
const sb = (st, b) => st.per_board[b];
function cstate(c) {
  if (!c || c.status === 'empty') return c && c.proposals.length ? 'proposed' : 'empty';
  if (c.status === 'skipped') return 'skip';
  return ({ pass: 'pass', fail: 'fail', review: 'review' })[c.verdict] || 'rec';
}
const inRange = c => !!c && c.origin === 'operator_confirmed';
const inRangeHtml = () => '<span class="inrange">\u2713 in range</span> <span class="muted">(no value)</span>';
const settled = c => c && (c.status === 'accepted' || c.status === 'skipped');
const applicable = (st, b) => sb(st, b) && sb(st, b).state !== 'na';
/* Tier 2 sections (applies_to: operator) apply to a board only when the operator or the decision gate sent it there, for the current attempt. */
const secOfTest = (st, tid) => st.sections.find(s => s.tests.some(t => t.id === tid));
function secAp(st, sec, b) {
  if (!applicable(st, b)) return false;
  if (!sec || sec.applies_to !== 'operator') return true;
  const r = S.tier2 && S.tier2[b + '|' + st.id];
  return !!(r && r.sections.includes(sec.id) && r.attempt === (sb(st, b).attempt || 1));
}
/* Where Enter takes you: row mode (same test, next board) for steps the plan marks `row`, board by board otherwise; the switch is remembered per step. */
function advMode(st) {
  const v = lsGet('advance:' + st.id, null);
  if (v) return v;
  return st.advance === 'row' ? 'board' : st.advance === 'board' ? 'test' : ui.advance;
}
function testInfo(tid) { for (const st of S.steps) for (const sec of st.sections) for (const t of sec.tests) if (t.id === tid) return { t, sec, st }; return null; }
function cur() { const st = curStep(); if (!st) return {}; const i = testInfo(ui.test); return i && i.st.id === st.id ? { st, t: i.t, sec: i.sec, c: cellOf(ui.board, ui.test) } : { st }; }

function ensureSelection() {
  if (!S) return;
  if (!ui.step || !stepOf(ui.step)) {
    const first = S.steps.find(s => s.kind !== 'project' && s.kind !== 'setup' && s.kind !== 'report' && s.state !== 'done') || S.steps[1];
    ui.step = lsGet('step:' + S.meta.batch, null);
    if (S.project && S.project.state !== 'done') ui.step = 'project';          // not set up for this batch yet: open on step 1
    else if (!stepOf(ui.step) || ui.step === 'project') ui.step = (S.steps.find(s => s.kind === 'setup' && s.state !== 'done') || first).id;
    ui.board = lsGet('board:' + S.meta.batch, null);
  }
  if (!ui.board || !S.boards.some(b => b.label === ui.board)) ui.board = S.boards[0].label;
  const st = curStep();
  if (st.kind !== 'testplan' && st.kind !== 'project' && st.kind !== 'setup' && st.kind !== 'report' && !applicable(st, ui.board)) {
    const b = S.boards.find(b => applicable(st, b.label)); if (b) ui.board = b.label;
  }
  const all = testsOf(st);
  if (st.kind !== 'assembly' && all.length && !all.some(x => x.t.id === ui.test)) {
    const un = all.find(x => secAp(st, x.sec, ui.board) && !settled(cellOf(ui.board, x.t.id)));
    ui.test = (un || all[0]).t.id;
  }
  if (st.kind === 'assembly' && !ui.noSel && (ui.line == null || !S.bom.some(l => l.line === ui.line))) asmEmpty();
}
/* Assembly opens with nothing selected: the same waiting state as after a placement, with a first-use message.
   No select_line is sent to Fusion until a scan, a typed scan or a click on a list line. */
function asmEmpty() { clearTimeout(selTimer); ui.line = null; ui.noSel = true; ui.waitFresh = true; ui.tick = null; }
function persist() { if (S) { lsSet('step:' + S.meta.batch, ui.step); lsSet('board:' + S.meta.batch, ui.board); } }

/* ------------------------------------------------------------------ rendering */
function render() {
  if (!S) { if (NP) renderWelcome(); return; }
  document.body.classList.remove('welcome');
  if (toastStep !== null && toastStep !== ui.step) clearToast();      // a toast belongs to the step it was raised on
  ensureSelection(); persist();
  document.documentElement.dataset.theme = ui.theme;
  renderAppbar(); renderTimeline(); renderLeft(); renderCentre(); renderRight(); renderBottom();
  $('#work').className = (ui.leftOpen ? '' : 'noleft ') + (ui.rightOpen ? '' : 'noright');
  checkAsk();
  scannerSync();
  NOTES.afterRender();
  VOICE.afterRender();
}

function renderAppbar() {
  const d = S.design, m = S.meta, pname = ((S.workspace || {}).project || {}).name || d.name;
  $('#appbar').innerHTML = `
    <span class="brand"><b>Poly</b>bench <span style="font-weight:400;color:var(--bar-ink2)">DV</span></span>
    <span class="ctx proj">Project <b>${esc(pname)}</b>${S.demo ? '' : pname !== d.name ? ` <span class="faint">design ${esc(designLabel(d))}</span>` : (designLabel(d).slice(d.name.length).trim() ? ` <b>${esc(designLabel(d).slice(d.name.length).trim())}</b>` : '')}</span><span class="ctx">Batch <b>${esc(m.batch)}</b> &middot; ${S.boards.length} boards</span>
    ${S.origin === 'simulated' ? '<span class="badge sim">SIMULATED</span>' : ''}
    ${S.demo ? `<span class="badge demo" title="The packaged demo, in its own folder (${esc(S.demo.dir)}). Nothing here is written to your projects and no instrument or ECAD program is driven.">DEMO PROJECT</span><button class="sm" data-act="democlose" title="${S.demo.back && S.demo.back.kind === 'project' ? 'Reopen ' + esc(S.demo.back.name || S.demo.back.dir) : 'Back to the welcome page'}">${S.demo.back && S.demo.back.kind === 'project' ? 'Back to my project' : 'Back to welcome'}</button>` : ''}
    ${m.protocol_changed ? '<button class="badge bad" data-act="protoaccept" title="The protocol file changed since this batch started or was last accepted. Click to see the hashes and accept it.">PROTOCOL CHANGED</button>' : ''}
    ${(m.journal_problems || []).length ? '<span class="badge bad" title="' + esc(m.journal_problems.join('; ')) + '">JOURNAL WARNING</span>' : ''}
    <span class="sp"></span>
    ${S.demo ? '' : `<span class="ctx" id="saved" title="Every accepted value is fsynced to the journal before it is shown">saved: <b>${S.events}</b> events</span>`}
    ${ui.connected ? '' : '<span class="badge bad">SERVER NOT REACHABLE</span>'}
    <label class="sw"><input type="checkbox" id="adv" ${ui.advanced ? 'checked' : ''}> Advanced</label>
    <button data-nt="add" title="Add a note linked to what you are looking at (c, or Alt+C from anywhere)">Note <kbd>c</kbd></button>
    <button data-act="theme" title="Light or dark">${ui.theme === 'dark' ? 'Light' : 'Dark'}</button>
    <button data-act="help" title="Keyboard help">?</button>`;
}

function nodeNo(i) { return 5 + i; }
const tlWords = t => String(t).split(/\s+/).filter(Boolean).map(w => `<span class="w">${esc(w)}</span>`).join(' ');   // a word never breaks: it wraps between words or ends in an ellipsis (title holds the full name)
function renderTimeline() {
  const dn = DESIGN.map((t, i) => i === 2 ? `<button class="tl tp-tile${ui.step === 'testplan' ? ' sel' : ''}" data-act="step" data-id="testplan" title="Test plan: the board's test plan document, read-only"><div class="l1"><span class="t">${tlWords(t)}</span></div><div class="l2"><span class="n">${i + 2}</span><span>${TP.files().length ? (TP.files().every(f => f.exists) ? 'document' : 'missing') : 'none'}</span></div></button>` : `<div class="tl later" title="${t}: design phase, not part of this build"><div class="l1"><span class="t">${tlWords(t)}</span></div><div class="l2"><span class="n">${i + 2}</span><span>later</span></div></div>`).join('');
  const pjs = S.steps.find(s => s.kind === 'project');
  const tlBtn = (s, no, prog, pct) => {
    const sel = s.id === ui.step ? ' sel' : '';
    return `<button class="tl st-${s.state}${sel}" data-act="step" data-id="${esc(s.id)}" title="${esc(s.title)}: ${prog}${s.fails ? ', ' + s.fails + ' FAIL' : ''}">
      <div class="l1"><span class="t">${tlWords(s.title.replace(/\s*\(.*\)\s*$/, ''))}</span>${s.retests ? `<span class="rt">R${s.retests}</span>` : ''}${s.fails ? `<span class="fl">${s.fails}F</span>` : ''}</div>
      <div class="l2"><span class="n">${no}</span><i class="bar"><b style="width:${pct}%"></b></i><span class="mono">${prog}</span></div></button>`;
  };
  const proj = pjs ? tlBtn(pjs, 1, `${pjs.settled}/${pjs.applicable}`, Math.round(100 * pjs.settled / pjs.applicable)) : '';
  const ordered = S.steps.filter(s => s.kind !== 'project').sort((a, b) => KINDS.indexOf(a.kind === 'automated' ? 'function' : a.kind) - KINDS.indexOf(b.kind === 'automated' ? 'function' : b.kind));
  const bn = ordered.map((s, i) => {
    let prog = '', pct = 0;
    if (s.kind === 'report') prog = 'preview';
    else if (s.applicable) { prog = `${s.settled}/${s.applicable}`; pct = Math.round(100 * s.settled / s.applicable); }
    else { prog = '0/0'; if (s.kind === 'visual' && S.aoi && s.state === 'done') pct = 100; }
    return tlBtn(s, nodeNo(i), prog, pct);
  }).join('');
  $('#timeline').innerHTML = `<div class="tl-group">Design</div>${proj}${dn}<div class="tl-sep"></div><div class="tl-group">Bench</div>${bn}`;
}

/* "Not similar to the batch": display only, from S.similarity (server). Never a verdict. */
const simOf = (b, tid) => { const x = S.similarity && S.similarity[tid]; return x && x.flagged && x.flagged[b] ? x.flagged[b] : null; };
function dotHtml(b, c, st, tid) {
  const s = applicable(st, b) ? cstate(c) : 'na';
  return `<i class="dot s-${s}${b === ui.board ? ' curb' : ''}${tid && simOf(b, tid) ? ' sim' : ''}${tid && c && (S.anomalies || []).some(a => a.board === b && a.test_id === tid) ? ' anom' : ''}"></i>`;
}
/* ---- UIR-17: collapsible group headings in the left lists
   A group heading folds its rows to the name, the count and one square per board. A group whose every board passed starts folded;
   the group holding the current test is always open. Above BAR_MIN_BOARDS boards the squares become one bar of pass, fail and
   open counts (review counts as open). BAR_MIN_BOARDS is TBR (QUESTIONS Q8, proposed 12); it closes at the bench with the maintainer. */
const BAR_MIN_BOARDS = 12;
const secKey = (st, sec) => st.id + '|' + sec.id;
const holdsCurrent = sec => ui.gate == null && ui.test != null && sec.tests.some(t => t.id === ui.test);
function secBoardState(st, sec, b) {
  if (!secAp(st, sec, b)) return 'na';
  const ss = sec.tests.map(t => cstate(cellOf(b, t.id)));
  if (!ss.length) return 'empty';
  if (ss.includes('fail')) return 'fail';
  if (ss.includes('review')) return 'review';
  if (ss.every(x => x === 'pass' || x === 'rec')) return 'pass';
  if (ss.every(x => x === 'pass' || x === 'rec' || x === 'skip')) return 'skip';
  return ss.some(x => x !== 'empty') ? 'proposed' : 'empty';
}
const secAllPass = (st, sec) => { const bs = S.boards.filter(b => secAp(st, sec, b.label)); return !!bs.length && sec.tests.length > 0 && bs.every(b => secBoardState(st, sec, b.label) === 'pass'); };
function secOpen(st, sec) {
  if (holdsCurrent(sec)) return true;
  const o = ui.secOpen[secKey(st, sec)];
  return o !== undefined ? o : !secAllPass(st, sec);
}
const bucket = s => s === 'pass' || s === 'rec' || s === 'skip' ? 'pass' : s === 'fail' ? 'fail' : s === 'na' ? 'na' : 'open';
function countBar(states, label) {
  const n = { pass: 0, fail: 0, open: 0 }; states.forEach(s => { const k = bucket(s); if (k !== 'na') n[k]++; });
  const tot = n.pass + n.fail + n.open || 1;
  const txt = `${n.pass} pass, ${n.fail} fail, ${n.open} open`;
  return `<span class="cbar" title="${esc((label ? label + ': ' : '') + txt + ' (review counts as open)')}" data-n="${n.pass}/${n.fail}/${n.open}"><span class="cb-bar"><i class="p" style="flex:${n.pass}"></i><i class="f" style="flex:${n.fail}"></i><i class="o" style="flex:${n.open}"></i></span><span class="cb-t"><b class="p">${n.pass}</b>/<b class="f">${n.fail}</b>/<b class="o">${n.open}</b></span></span>`;
}
const manyBoards = () => S.boards.length > BAR_MIN_BOARDS;
const squares = (states, label) => manyBoards() ? countBar(states, label)
  : `<span class="dots">${S.boards.map((b, i) => `<i class="dot s-${states[i]}${b.label === ui.board ? ' curb' : ''}" title="${b.label}: ${states[i]}"></i>`).join('')}</span>`;
function secHead(key, open, forced, inner, tip) {
  return `<div class="sec-h${open ? '' : ' folded'}" data-act="sectoggle" data-k="${esc(key)}" role="button" tabindex="0" aria-expanded="${open}" title="${esc(tip || '')}${forced ? ' (holds the current test, stays open)' : ' (click to ' + (open ? 'fold' : 'open') + ')'}"><span class="chev" aria-hidden="true">${open ? '&#9662;' : '&#9656;'}</span>${inner}</div>`;
}
function rowDots(st, sec, t) {
  if (manyBoards()) return countBar(S.boards.map(b => secAp(st, sec, b.label) ? cstate(cellOf(b.label, t.id)) : 'na'), t.id);
  return `<span class="dots">${S.boards.map(b => {
    const c = cellOf(b.label, t.id); const ap = secAp(st, sec, b.label);
    return (ap ? dotHtml(b.label, c, st, t.id) : `<i class="dot s-na${b.label === ui.board ? ' curb' : ''}"></i>`).replace('<i ', `<i title="${b.label}: ${ap ? esc(inRange(c) ? 'in range (no value)' : c && c.value != null ? (RES.isTable(c) ? 'table' : c.value) : (c ? c.status : 'empty')) : 'n/a, not in Tier 2'}${ap && simOf(b.label, t.id) ? ' (not similar to the batch)' : ''}" `);
  }).join('')}</span>`;
}
/* "To do" of the Assembly aid: the BOM lines in the chosen order. Folds like any group heading (UIR-17); open while it holds the current line. */
const bomHoldsCurrent = () => !ui.noSel && ui.line != null && S.bom.some(l => l.line === ui.line);
function bomBlock() {
  const tot = asmTotals(), allDone = tot.nlines > 0 && tot.lines === tot.nlines;
  const o = ui.secOpen['assembly|todo'], forced = bomHoldsCurrent(), open = forced || (o !== undefined ? o : !allDone);
  const boardStates = S.boards.map(b => { const fit = S.bom.filter(l => !l.dnf); const n = fit.filter(l => asmOf(l).done.includes(b.label)).length; return !fit.length ? 'empty' : n === fit.length ? 'pass' : n ? 'proposed' : 'empty'; });
  let h = secHead('assembly|todo', open, forced, `<span class="sh-t">To do</span><span class="c">${tot.lines}/${tot.nlines} lines done</span>${open ? '' : squares(boardStates, 'To do')}`, 'To do');
  if (open) h += bomList().map(l => {
      const a = asmOf(l), cur = l.line === ui.line, nb = S.boards.length;
      const ic = cur ? '&#9654;' : ({ done: '&#10003;', partial: '&#9680;', todo: '&#9675;', dnf: '&ndash;' })[a.state];
      const sub = a.state === 'done' ? 'done on all boards' : a.state === 'dnf' ? 'not fitted' : a.state === 'todo' ? `${l.designators.length} per board` : `${a.done.length} of ${nb} boards, missing ${a.missing.join(' ')}`;
      return `<div class="bomrow st-${a.state}${cur ? ' cur' : ''}" data-act="line" data-id="${l.line}" title="${esc(l.mpn)}: ${esc(sub)}${esc(rankTip(l))}">
        <span class="ic">${ic}</span><span class="ds"><b class="no">${l.order}</b> ${esc(l.designators.join(' '))}${l.polarity && !/^none/i.test(l.polarity) ? '<span class="tag pol">POL</span>' : ''}${l.dnf ? '<span class="tag dnf">DNF</span>' : ''}${!l.dnf && listBy() === 'hand' && RANK[l.line] && RANK[l.line].size_unknown ? '<span class="tag szu" title="No package or size known for this line, so it is listed last among the tall parts">size unknown</span>' : ''}</span>
        <span class="pc">${NOTES.mk('line', l.line)}${l.dnf ? '' : a.done.length + '/' + nb}</span><span class="sub">${esc(sub)}</span></div>`;
    }).join('');
  return `<div class="sec bomsec">${h}</div>`;
}
function renderLeft() {
  const st = curStep(), el = $('#left');
  const hdr = (title, sub) => `<div class="ph"><span>${esc(title)}</span><span class="muted" style="font-weight:400">${sub}</span><span class="sp"></span><button data-act="toggleLeft" title="Collapse (Ctrl+B)">&laquo;</button></div>`;
  if (st.kind === 'testplan') return TP.left(el, hdr);
  if (st.kind === 'project') return leftProject(el, hdr);
  if (st.kind === 'setup') {
    el.innerHTML = hdr('Boards', S.boards.filter(b => b.serial).length + '/' + S.boards.length + ' serials') +
      S.boards.map(b => `<div class="trow" data-act="board" data-id="${b.label}"><span class="id">${b.label}</span><span class="ti">${esc(b.serial) || '<span class="faint">no serial</span>'}</span></div>`).join('');
    return;
  }
  if (st.kind === 'report') {
    el.innerHTML = hdr('Report', '') + ['Header and summary', 'Results by step', 'Retests', 'Notes and comments', 'Open items', 'Actions and evidence'].map(x => `<div class="trow"><span class="id"></span><span class="ti">${x}</span></div>`).join('');
    return;
  }
  if (st.kind === 'automated' && !(ui.diag || {})[st.id]) return AUTO.left(el, hdr, st);
  let html = hdr(st.title.replace(/\s*\(.*\)\s*$/, ''), `${st.settled}/${st.applicable}`);
  if (st.kind === 'automated') html += `<div class="sec"><div class="sec-h"><button class="sm" data-act="autoback">&larr; Back to the run</button></div></div>`;
  if (st.kind === 'visual' && S.aoi && typeof AOI !== 'undefined') { el.innerHTML = html + AOI.left(st); return; }
  if (st.kind === 'assembly' && asmMode() !== 'assembling') {           // UIR-20: no list until Assemble is pressed
    el.innerHTML = html + `<div class="sec"><div class="asmnote faint small">${asmMode() === 'skipped' ? 'Assembly skipped: the boards are already built.' : 'The to-do list appears when you press Assemble.'}</div></div>`;
    return;
  }
  let lastTier = null, t2first = true;
  const gatesHere = (S.gates || []).filter(g => (g.after.mode || 'powered') === st.id);
  let gateShown = false;
  const gateRow = () => gatesHere.map(g => {
    const n = g.boards.filter(x => x.verdict === 'tier2' && !x.assigned && !x.rejected).length;
    const w = g.boards.filter(x => x.verdict === 'waiting').length;
    return `<div class="trow gate${ui.gate === g.index && ui.step === st.id ? ' cur' : ''}" data-act="gate" data-i="${g.index}"><span class="id">GATE</span><span class="ti" title="${esc(g.title)}">${esc(g.title.replace('Decision gate ', 'Gate '))}</span><span class="dots">${n ? `<span class="tag pol">${n} to Tier 2</span>` : w ? `<span class="faint">${w} waiting</span>` : '<span class="faint">clear</span>'}</span></div>`;
  }).join('');
  const drawSecs = list => { for (const sec of list) {
    if (st.kind === 'automated' && (sec.hidden || !sec.diagnose)) continue;
    const tier = sec.tier || null;
    const cond = sec.applies_to === 'operator';
    if (tier !== null && tier !== lastTier) {
      if (tier >= 2 && !gateShown) { html += gateRow(); gateShown = true; }
      html += `<div class="tier-h t${tier}"><span title="${tier === 1 ? 'Every board' : 'Deeper review: only the boards the Decision gate or the operator sent here'}">${tier === 1 ? 'Tier 1 &middot; every board' : 'Tier 2 &middot; deeper review'}</span><span class="sp"></span>${tier >= 2 ? `<button class="sm" data-act="t2send" title="Open the Tier 2 sections of this step for ${ui.board}">Send ${ui.board}&hellip;</button>` : ''}</div>`;
      lastTier = tier;
    }
    const bs = S.boards.filter(b => secAp(st, sec, b.label));
    const settledN = sec.tests.reduce((a, t) => a + bs.filter(b => settled(cellOf(b.label, t.id))).length, 0);
    const tot = sec.tests.length * bs.length;
    const open = secOpen(st, sec), forced = holdsCurrent(sec);
    const secTip = sec.title + (sec.designators && sec.designators.length ? ' (' + sec.designators.join(', ') + ')' : '');
    html += `<div class="sec${cond ? ' cond' : ''}">` + secHead(secKey(st, sec), open, forced, `<span class="sh-t">${esc(sec.title)}</span>${sec.designators && sec.designators.length ? `<span class="sh-d">${esc(sec.designators.slice(0, 6).join(' '))}${sec.designators.length > 6 ? ' +' + (sec.designators.length - 6) : ''}</span>` : ''}<span class="c">${cond && !bs.length ? 'n/a' : settledN + '/' + tot}</span>${(st.kind === 'visual' || st.kind === 'assembly') && (sec.actions || []).length + sec.tests.length ? `<button class="sm guidebtn" data-vo="guide" data-sec="${esc(sec.id)}" ${S.action ? 'disabled' : ''} title="Voice guide: walk this section step by step on ${ui.board}">Guide</button>` : ''}${open ? '' : squares(S.boards.map(b => secBoardState(st, sec, b.label)), sec.title)}`, secTip);   // the AOI and assembly views have no run bar of their own: their Guide stays here, every other step has it in the work area
    if (open) for (const t of sec.tests) {
      html += `<div class="trow${t.id === ui.test && ui.gate == null ? ' cur' : ''}" data-act="test" data-id="${t.id}" data-flash="${ui.board}|${t.id}"><span class="id">${t.id}${NOTES.mk('test', t.id)}</span><span class="ti" title="${esc(t.title)}">${esc(t.title)}</span>${rowDots(st, sec, t)}</div>`;
    }
    html += `</div>`;
  } };
  /* UIR-21: the Assembly aid lists the protocol's check groups in build order: groups declared `position: before_placement` above To do,
     every other group (after_placement, or no position) below it. Everywhere else the groups keep the protocol's order. */
  const [preSecs, postSecs] = st.kind === 'assembly' ? asmSplit(st) : [[], st.sections];
  drawSecs(preSecs);
  if (st.kind === 'assembly') html += bomBlock();
  drawSecs(postSecs);
  if (!gateShown) html += gateRow();
  const ret = (S.retired || []).filter(r => r.step === st.id || (!r.step && st.kind === 'unpowered'));
  if (ret.length) {
    html += `<div class="sec retired"><div class="sec-h" title="Tests an earlier plan had and the current plan no longer has. Their recorded results are kept.">Retired<span class="c">${ret.length}</span></div>` +
      ret.map(r => `<div class="trow retired" data-act="retired" data-id="${esc(r.id)}"><span class="id">${esc(r.id)}</span><span class="ti" title="${esc(r.title)}">${esc(r.title) || '<span class="faint">removed from the plan</span>'}</span><span class="dots faint">${Object.keys(r.results).length} kept</span></div>`).join('') + `</div>`;
  }
  el.innerHTML = html;
}

/* ---- centre */
function boardBar(st) {
  const chips = S.boards.map(b => {
    const x = sb(st, b.label); if (x.state === 'na') return '';
    const s = x.state === 'done' ? (x.fails ? 'fail' : 'pass') : x.state === 'attention' ? 'fail' : x.state === 'partial' ? 'proposed' : 'empty';
    return `<button class="bchip${b.label === ui.board ? ' cur' : ''}" data-act="board" data-id="${b.label}" title="${b.label} ${esc(b.serial)}: ${x.settled}/${x.applicable}"><i class="dot s-${s}"></i>${b.label}${(x.attempt || 1) > 1 ? `<span class="rr">R${x.attempt}</span>` : ''}</button>`;
  }).join('');
  return (st.kind === 'visual' && S.aoi && typeof AOI !== 'undefined' ? AOI.bar() : '') + `<div class="bar2"><span class="lbl">Board</span>${chips}<span class="sp"></span>
    <label class="sw" title="Where Enter takes you"><input type="checkbox" id="advmode" ${advMode(st) === 'board' ? 'checked' : ''}> next board, same test</label></div>`;
}
function renderCentre() {
  const st = curStep(), el = $('#centre');
  if (st.kind === 'testplan') return TP.centre(el);
  if (st.kind === 'project') return viewProject(el);
  if (st.kind === 'setup') return viewSetup(el);
  if (st.kind === 'report') return viewReport(el);
  if (st.kind === 'assembly' && ui.test === null) return viewAssembly(el, st);
  if (st.kind === 'automated' && !(ui.diag || {})[st.id]) return AUTO.centre(el, st);
  if (ui.gate != null && (S.gates || []).some(g => g.index === ui.gate && (g.after.mode || 'powered') === st.id)) return viewGate(el, st);
  if (ui.retired && (S.retired || []).some(r => r.id === ui.retired)) return viewRetired(el, st);
  if (st.kind === 'visual' && S.aoi && typeof AOI !== 'undefined') return AOI.centre(el, st);
  if (!st.sections.length) { el.innerHTML = `<div class="empty">This step has no tests in the protocol.</div>`; return; }
  viewTest(el, st);
}

/* ---- the single value box (PW-2) */
const srcLabel = src => { src = src || {}; if (src.kind === 'action') return String(src.component || 'instrument').split('.')[0].toUpperCase() + (src.run_ref ? ' run ' + src.run_ref : ''); return src.kind === 'assistant' ? 'assistant' : src.kind === 'voice' || src.input_method === 'voice' ? 'voice' : (src.kind || 'keyboard'); };
/* what the box holds when the operator has typed nothing: the accepted value, else the first plain proposal */
function boxBase(c, t) {
  if (!c || t.kind === 'table') return '';
  if (c.status === 'accepted') return c.value != null && !inRange(c) && !RES.isTable(c) ? String(c.value) : '';
  const p = (c.proposals || [])[0];
  return p && p.value != null && typeof p.value !== 'object' && p.origin !== 'operator_confirmed' ? String(p.value) : '';
}
const boxCls = c => !c ? '' : c.status === 'accepted' ? (c.verdict === 'fail' ? 'bad' : ['review'].includes(c.verdict) || (c.flags || []).some(f => f.startsWith('outside_')) ? 'warn' : '') : '';
/* the instrument value an edit replaced: the last history entry whose source was an action, when the cell no longer says so */
const instrumentSaid = c => { if (!c || c.status !== 'accepted' || (c.source || {}).kind === 'action') return null; const h = (c.edits || []).filter(e => e.source && e.source.kind === 'action'); return h.length ? h[h.length - 1] : null; };

/* Board-level actions on pending proposals (they used to sit in the proposal box, which a plain value no longer has) */
function propBar(st, b) {
  const n = testsOf(st).reduce((a, x) => { const c = cellOf(b, x.t.id); return a + (c && c.status !== 'accepted' && (c.proposals || []).length ? 1 : 0); }, 0);
  const pi = (S.pending_instrument || {})[b] || 0;
  if (!n && !pi) return '';
  return `<div class="propbar" id="propbar"><span><b>${n}</b> proposed value${n === 1 ? '' : 's'} on ${b} in this step, not yet results</span><span class="sp"></span>${pi ? `<button class="sm primary" data-act="recordpending" data-b="${b}" title="These readings came from an action in an earlier version, which left them as proposals. Instrument readings are results: record them as such.">Record ${pi} instrument reading${pi === 1 ? '' : 's'}</button>` : ''}${n ? `<button class="sm" data-act="acceptall" title="Takes only clean PASS proposals for ${b} in this step">Accept all clean for ${b}</button>` : ''}</div>`;
}

/* The instrument action's Run button lives here, at the top of the work area, like the automated step's Run (PW-1). */
function runBarHtml(st, sec, t, b, x) {
  const acts = sec.actions || [];
  const guide = (acts.length + sec.tests.length) ? `<button class="sm guidebtn" data-vo="guide" data-sec="${esc(sec.id)}" ${S.action ? 'disabled' : ''} title="Voice guide: walk this section step by step on ${b}">Guide</button>` : '';
  if (!acts.length) return guide ? `<div class="runbar guideonly" id="runbar"><span class="sp"></span>${guide}</div>` : '';
  const a = S.action;
  if (a) return `<div class="runbar running" id="runbar"><span class="muted">${esc(a.title)} on ${esc(a.board)}${a.text ? ': ' + esc(a.text) : ''}…</span><span class="prog"><b id="progb" style="width:${Math.round(100 * (a.fraction || 0))}%"></b></span><span class="sp"></span><button class="danger" data-act="abort">Abort</button></div>`;
  const stops = S.stops.filter(z => z.board === b && !z.ack), t2off = !secAp(st, sec, b);
  const off = !x.gate.ok || stops.length || t2off;
  return `<div class="runbar" id="runbar">${acts.map(z => `<button class="primary huge" data-act="run" data-sec="${esc(sec.id)}" data-action="${esc(z.id)}" ${off ? 'disabled' : ''} title="Run on ${b}: ${esc(z.plugin)}">Run ${esc(z.title)} on ${b}</button>`).join('')}${!x.gate.ok ? `<span class="bad small">${esc(x.gate.reasons.join('; '))}</span>` : ''}<span class="sp"></span>${guide}</div>`;
}

function viewTest(el, st) {
  if (!ui.test) { const a = testsOf(st); ui.test = a[0] && a[0].t.id; }
  const { t, sec } = cur(); if (!t) { el.innerHTML = '<div class="empty">No test selected.</div>'; return; }
  const b = ui.board, c = cellOf(b, t.id), x = sb(st, b), gate = x.gate;
  const old = $('#val');
  const base = boxBase(c, t);
  const keep = old && old.dataset.ctx === b + '|' + t.id ? (old.value === (old.dataset.base || '') ? base : old.value) : (ui.ctxKey === b + '|' + t.id && ui.draft ? ui.draft : base);
  const hadFocus = old && document.activeElement === old;
  const stops = S.stops.filter(s => s.board === b && !s.ack);
  const probe = (t.probe || []).map(p => p.pole ? `<span class="pole ${p.pole === '+' ? 'red' : 'blk'}" title="${p.pole === '+' ? 'red lead' : 'black lead'}">${p.pole === '+' ? '+' : '&minus;'}</span> <span class="mono">${esc(p.point)}</span>` : `${esc(p.net || '')}${p.point ? ' <span class="mono">(' + esc(p.point) + ')</span>' : ''}`).join(' &rarr; ');
  const props = c ? c.proposals : [];
  let h = boardBar(st) + `<div class="pane">${runBarHtml(st, sec, t, b, x)}${propBar(st, b)}${typeof METER !== 'undefined' ? METER.html() : ''}${RES.strip(st, b)}
    <div class="crumb"><span>${esc(st.title)}</span><span class="sep">/</span><span>${esc(sec.title)}</span><span class="sep">/</span><span>${b}${x.attempt > 1 ? ' &middot; attempt ' + x.attempt : ''}</span></div>
    <h1 class="tt"><span class="id">${t.id}</span>${esc(t.title)}${NOTES.mk('test', t.id)}</h1>
    ${probeCard(t, probe)}
    <dl class="facts">
      ${probe && !(t.highlight || []).length && !(t.nets || []).length ? `<dt>Where</dt><dd>${probe}</dd>` : ''}
      ${t.outlier ? `<dt>Outlier</dt><dd>${esc(outlierText(t.outlier))} <span class="muted">of the batch median, read by the Decision gate</span></dd>` : ''}
      <dt>Expected</dt><dd><span class="exp">${esc(t.expected) || (t.kind === 'yn' ? 'yes' : 'record only')}</span>${t.class ? `<span class="cls ${t.class}">${t.class.toUpperCase()}</span>` : ''}${t.source_text ? ` <span class="muted">source: ${esc(t.source_text)}</span>` : ''}</dd>
      ${t.stop_if ? `<dt>Stop rule</dt><dd class="stoprule">${esc(t.stop_if.text || 'stop')} <span class="muted" style="font-weight:400">(${t.stop_if.below != null ? 'below ' + t.stop_if.below : t.stop_if.above != null ? 'above ' + t.stop_if.above : t.stop_if.outside ? 'outside ' + t.stop_if.outside[0] + '..' + t.stop_if.outside[1] : 'on ' + t.stop_if.equals})</span></dd>` : ''}
      ${t.class === 'guess' || t.class === 'tbr' ? `<dt>Closes on</dt><dd>${esc(t.limit.closes_on)}</dd>` : ''}
    </dl>
    ${sec.instructions || t.instructions ? `<div class="instr">${md(sec.instructions)}${md(t.instructions)}</div>` : ''}`;
  if (!gate.ok) h += `<div class="callout"><b>Gated.</b> ${gate.reasons.map(esc).join('; ')}. <div class="row"><button data-act="override" data-step="${st.id}">Override with a reason&hellip;</button></div></div>`;
  else if (gate.override) h += `<div class="callout"><b>Gate overridden:</b> ${esc(gate.override)}</div>`;
  for (const s of stops) h += `<div class="callout stop"><b>STOP on ${b}:</b> ${esc(s.text)} (${esc(s.test_id)} = ${esc(s.value)}) <div class="row"><button data-act="ackstop" data-id="${s.id}">Acknowledge stop with a reason&hellip;</button></div></div>`;
  const pr = ui.pendingRun;
  if (pr) h += `<div class="callout" style="border-color:var(--accent);background:var(--accent-bg)"><b>Before '${esc(pr.title)}' on ${esc(pr.board)}:</b><div style="font-size:15px;margin:6px 0">${esc(pr.prompt)}</div><div class="row"><button class="primary" id="runok" data-act="runok">Continue</button><button data-act="runno">Cancel</button></div></div>`;
  const t2off = !secAp(st, sec, b);
  if (t2off && applicable(st, b)) h += `<div class="callout"><b>${b} is not in Tier 2 for this section.</b> Tier 2 is deeper review for the boards the Decision gate sent there.<div class="row"><button data-act="t2send" class="primary">Send ${b} to Tier 2&hellip;</button></div></div>`;
  const blocked = !gate.ok || stops.length || t2off;
  if (t.kind === 'table') h += `<div class="entry tableonly"><span class="faint">A table result comes from an instrument action: use its Run button at the top of this panel. It is recorded as the result when the action delivers it.</span></div>`;
  else h += `<div class="entry one ${boxCls(c)}" data-flash="${b}|${t.id}"><div class="row"><input id="val" class="big" data-ctx="${b}|${t.id}" data-base="${esc(base)}" autocomplete="off" spellcheck="false" ${blocked ? 'disabled' : ''}
      placeholder="${inRange(c) ? 'in range (no value)' : t.kind === 'yn' ? 'y / n' : t.kind === 'choice' ? '1 ... ' + (t.options || []).length : t.kind === 'hex' ? '0x...' : t.kind === 'number' ? 'type the reading' : 'type'}">
      ${t.unit ? `<span class="unit">${esc(t.unit)}</span>` : ''}<span class="srctag" id="srctag"></span></div>
    <div id="boxnote"></div>
    ${t.kind === 'choice' || t.kind === 'yn' ? `<div class="opts">` + (t.kind === 'yn' ? ['yes', 'no'] : t.options).map((o, i) => `<button data-act="quick" data-v="${esc(o)}" class="${c && c.value === o ? 'on' : ''}" ${blocked ? 'disabled' : ''}><kbd>${t.kind === 'yn' ? o[0] : i + 1}</kbd>${esc(o)}</button>`).join('') + `</div>` : ''}
    <div id="entryctl"></div></div>`;
  // result / state: a plain value lives in the single box above; a table result, a skip and any further proposals keep their blocks
  if (c && c.status === 'accepted') {
    if (RES.isTable(c)) h += RES.recorded(t, c, b);
    if (simOf(b, t.id)) h += `<div class="simnote" id="simresult"><i class="simmark"></i><b>Not similar to the batch.</b> ${esc(simOf(b, t.id).text.replace(/^≠ batch: /, ''))}</div>`;
    if ((c.edits || []).length) h += `<div class="hist">${c.edits.map(e => `<div>earlier value: ${RES.isTable({ value: e.value }) ? 'table' : esc(e.value)} (${esc(e.verdict)})${e.source && e.source.kind === 'action' ? ' from ' + esc(srcLabel(e.source)) : ''}</div>`).join('')}</div>`;
  } else if (c && c.status === 'skipped') {
    h += `<div class="box"><h3>Skipped</h3><div class="acc"><span class="meta">${esc(c.skip_reason)}</span></div></div>`;
  }
  { const rest = RES.isTable({ value: props[0] && props[0].value }) || (c && c.status === 'accepted') ? props : props.slice(1);
    if (rest.length) h += RES.proposals(t, { ...c, proposals: rest }, b); }
  h += st.sections.some(z => (z.actions || []).length) && typeof EXP !== 'undefined' ? EXP.html(st, b) : RES.results(st, b);
  if (c && c.prev_attempts && c.prev_attempts.length) h += `<div class="box"><h3>Earlier attempts</h3><div class="hist">${c.prev_attempts.map(a => `<div>attempt ${a.attempt}: ${a.status === 'skipped' ? 'skipped, ' + esc(a.skip_reason) : (RES.isTable({ value: a.value }) ? 'table' : esc(a.value) + ' ' + esc(a.unit || '')) + ' <span class="vd ' + a.verdict + '">' + (a.verdict || '').toUpperCase() + '</span>'}</div>`).join('')}</div>`;
  h += `<div class="navrow"><button data-act="prev">&uarr; Previous test</button><button data-act="next">&darr; Next test</button><span class="sp"></span>
    <button data-act="skip" ${blocked ? 'disabled' : ''}>Skip with reason&hellip;</button><button data-act="note">Note&hellip;</button><button data-act="retest" title="Open attempt ${x.attempt + 1} of this step for ${b}">Send ${b} back for retest&hellip;</button></div>`;
  // across the batch
  h += `<div class="box" style="margin-top:14px"><h3>${t.id} across the batch</h3><table class="across">` + S.boards.filter(bb => applicable(st, bb.label)).map(bb => {
    const cc = cellOf(bb.label, t.id), s = cstate(cc);
    return `<tr data-act="board" data-id="${bb.label}" class="${bb.label === b ? 'me' : ''}"><td class="mono">${bb.label}</td><td><i class="dot s-${s}"></i></td><td class="mono">${cc && cc.value != null ? RES.valueText(cc) : cc && cc.status === 'skipped' ? 'skipped' : ''}</td><td class="muted">${cc && cc.attempt > 1 ? 'attempt ' + cc.attempt : ''}${cc && cc.proposals.length ? ' proposal pending' : ''}${simOf(bb.label, t.id) ? `<span class="simnote" data-sim="${bb.label}"><i class="simmark"></i>${esc(simOf(bb.label, t.id).text)}</span>` : ''}</td></tr>`;
  }).join('') + `</table></div>`;
  if (ui.advanced) h += gridHtml(st);
  h += `</div>`;
  el.innerHTML = h;
  const inp = $('#val');
  if (typeof EXP !== 'undefined') EXP.after(st, b);
  if (inp) {
    inp.value = keep || ''; ui.ctxKey = b + '|' + t.id;
    if (!blocked && (hadFocus || !old || old.dataset.ctx !== inp.dataset.ctx) && !document.querySelector('.veil')) inp.focus();
  }
  updateEntryCtl();
  autoHighlight(t);
}

const outlierText = o => o.ratio ? 'outside x' + o.ratio[0] + '..x' + o.ratio[1] : '\u00b1' + o.pct + ' %' + (o.floor != null ? ' (min ' + o.floor + (o.floor_unit ? ' ' + o.floor_unit : '') + ')' : '');
/* Where to probe, as one compact card. Every test with `highlight` designators gets the button, not only Assembly aid. */
function ecadStatusText(t) {
  if (S.project && S.project.ecad_skipped) return { cls: 'bad', text: 'ECAD is off: this batch continues without ECAD (' + S.project.ecad_skipped.reason + '). Nothing is sent', lines: [] };
  const last = S.ecad_last, res = last && last.result;
  if (!res || last.test_id !== t.id) return { cls: '', text: 'not highlighted yet', lines: [] };
  if (res.disabled) return { cls: 'bad', text: res.error, lines: [] };
  if (res.error) return { cls: 'bad', text: res.error, lines: [], pick: res.pick || null };
  const sim = res.simulated || S.origin === 'simulated' ? ' (simulated: nothing is drawn)' : '';
  if (res.ok === false) return { cls: 'bad', text: (res.message || 'ECAD refused the highlight'), lines: [] };
  const lines = [];
  const miss = [];
  if ((res.missing || []).length) miss.push('parts ' + res.missing.join(' '));
  if ((res.pins_missing || []).length) miss.push('pins ' + res.pins_missing.join(' '));
  if ((res.nets_missing || []).length) miss.push('nets ' + res.nets_missing.join(' '));
  if ((res.pins || []).length) lines.push('pins ' + res.pins.join(' '));
  if ((res.nets || []).length) lines.push('nets ' + res.nets.join(' ') + ({ nets: ' (selected as nets)', pads: ' (selected as pads)', pinrefs: ' (nets cannot be selected here: their pins are selected)', parts: ' (nets cannot be selected here: the parts with a pin on them are selected)' }[res.net_mode] || ''));
  if (res.note && !res.simulated) lines.push(res.note);
  const hl = (res.highlighted || []).join(' ');
  return { cls: miss.length ? 'warn' : 'ok', text: (hl ? 'selected ' + hl : 'nothing to select') + sim + (miss.length ? '; not found in the ECAD document: ' + miss.join('; ') : ''), lines };
}
/* Several open Fusion documents and no safe automatic choice: one button per document. A click pins it for the session
   (a manual pin, never changed automatically) and repeats the selection. */
function pickHtml(pick, testId) {
  if (!pick || !(pick.options || []).length) return '';
  return `<div class="pc-pick"><span class="muted small">${pick.kind === 'board_doc' ? 'The board document is not the active tab.' : `Several ${ecadName()} documents are open.`}</span> ` +
    pick.options.map(o => `<button class="sm" data-act="ecadpin" data-key="${esc(o.key)}" data-test="${esc(testId)}" title="Pins this document for the session. This brings its tab forward in ${ecadName()}.">${pick.kind === 'board_doc' ? 'Use board document' : 'Use'}: ${esc(o.label)}</button>`).join(' ') + `</div>`;
}
function probeCard(t, probe) {
  const ds = t.highlight || [], pins = t.highlight_pins || [], nets = t.nets || [];
  if (!ds.length && !nets.length) return '';
  const st = ecadStatusText(t);
  return `<div class="probecard" data-test="${esc(t.id)}"><div class="pc-row"><span class="pc-lbl">Probe</span>
    ${probe ? `<span class="pc-nets">${probe}</span>` : ''}
    <span class="pc-ds">${ds.map(d => `<span class="dchip sm">${esc(d)}</span>`).join('')}${pins.map(d => `<span class="dchip sm pin">${esc(d)}</span>`).join('')}${nets.map(d => `<span class="dchip sm net">${esc(d)}</span>`).join('')}</span>
    <button class="sm" data-act="hl" title="Select these parts, pins and nets in the open ECAD document again">Highlight in ECAD</button></div>
    <div class="pc-status ${st.cls}"><i class="dotc ${st.cls}"></i>ECAD: ${esc(st.text)}${st.lines.map(l => `<div class="pc-more">${esc(l)}</div>`).join('')}${pickHtml(st.pick, t.id)}</div></div>`;
}

function gridHtml(st) {
  const bs = S.boards.filter(b => applicable(st, b.label));
  let h = `<div class="box" style="margin-top:14px"><h3>Full grid (advanced)</h3><div style="overflow:auto"><table class="grid"><tr><th>Test</th>${bs.map(b => `<th>${b.label}</th>`).join('')}</tr>`;
  for (const sec of st.sections) {
    h += `<tr class="sechead"><td colspan="${bs.length + 1}">${esc(sec.title)}</td></tr>`;
    for (const t of sec.tests) h += `<tr><td>${t.id} ${esc(t.title)}</td>` + bs.map(b => {
      const c = cellOf(b.label, t.id), s = cstate(c);
      return `<td class="c ${['pass', 'fail', 'review'].includes(s) ? s : ''}${b.label === ui.board && t.id === ui.test ? ' curc' : ''}" data-act="cell" data-b="${b.label}" data-id="${t.id}">${inRange(c) ? '\u2713 in range' : RES.isTable(c) ? 'table' : c && c.value != null ? esc(c.value) : c && c.status === 'skipped' ? 'skip' : c && c.proposals.length ? '...' : ''}</td>`;
    }).join('') + `</tr>`;
  }
  return h + `</table></div></div>`;
}

function updateEntryCtl() {
  const el = $('#entryctl'); if (!el) return;
  const { t, c } = cur(); const inp = $('#val'); const v = inp ? inp.value.trim() : '';
  const base = inp ? (inp.dataset.base || '') : '';
  const pend = c && c.status !== 'accepted' && props(c) ? c.proposals[0] : null;
  const fromInst = !!base && (c && c.status === 'accepted' ? (c.source || {}).kind === 'action' : !!pend && (pend.source || {}).kind === 'action');
  const clean = !!base && v === base;                       // the box still holds what the instrument (or the record) said
  const said = instrumentSaid(c);
  { const tag = $('#srctag'), note = $('#boxnote'); const src = c && c.status === 'accepted' ? c.source : pend && pend.source;
    if (tag) { tag.textContent = clean && src ? srcLabel(src) : v ? 'keyboard' : (src && base ? srcLabel(src) : 'keyboard'); tag.classList.toggle('inst', !!(clean && fromInst)); tag.title = clean && fromInst ? 'Value from the instrument. Type to override it; the instrument value stays in the history.' : ''; }
    if (note) {
      const ver = clean ? (c && c.status === 'accepted' ? c.verdict : pend && pend.verdict) : null;
      const reason = clean ? (c && c.status === 'accepted' ? c.reason : pend && pend.reason) : '';
      const instv = c && c.status === 'accepted' && fromInst && !clean ? c.value : null;
      const inr = c && c.status === 'accepted' && inRange(c) && !v;     // confirmed in range by the operator: there is no value to put in the box
      note.innerHTML = (inr ? `${RES.chip(c.verdict)} ${inRangeHtml()} <span class="faint small">${esc(srcLabel(c.source))}</span>` : '') + (clean && ver ? `${RES.chip(ver)} <span class="muted">${esc(reason || '')}</span>${c && c.status === 'accepted' ? ` <span class="faint small">${c.auto ? 'recorded automatically' : 'recorded'}</span>` : pend ? ' <span class="faint small">proposed, not yet a result: Enter accepts it</span>' : ''}` : '')
        + (said ? `<span class="editnote">edited (instrument said ${esc(said.value)}${said.unit ? ' ' + esc(said.unit) : ''})</span>` : '')
        + (instv != null && !said ? `<span class="editnote">typing overrides the instrument value ${esc(instv)}${c.unit ? ' ' + esc(c.unit) : ''}; it stays in the history</span>` : '')
        + (clean && pend ? ` <button class="sm" data-act="rejectprop" data-id="${pend.id}" title="Throw this proposal away; nothing is recorded">Reject</button>` : '')
        + (!clean && pend && base ? `<span class="editnote">typing overrides the instrument value ${esc(base)}${pend.unit ? ' ' + esc(pend.unit) : ''}</span>` : '');
    } }
  let pv = '', btns = '';
  const r = ui.pv && ui.pv.value_in === v ? ui.pv : null;
  if (clean) pv = '';
  else if (v && r) {
    if (!r.ok) pv = `<span class="vd invalid">INVALID</span> <span>${esc(r.error)}</span>`;
    else pv = `<span class="vd ${r.verdict}">${r.verdict === 'none' ? 'NO VERDICT' : r.verdict.toUpperCase()}</span> <span class="muted">${esc(r.reason || (r.verdict === 'pass' ? 'inside the limit' : ''))}</span>${r.stop ? ` <b class="stoprule">STOP: ${esc(r.stop)}</b>` : ''}`;
  } else if (!v) pv = `<span class="faint">${pend ? 'Enter accepts the proposal. ' : ''}Type a value, then Enter.</span>`;
  const bad = r && r.ok && (r.verdict === 'fail' || r.verdict === 'review');
  const armed = c && c.status === 'accepted' && v && ui.armed === v;
  if (armed) pv += ` <b>Press Enter again to replace ${esc(c.value)}.</b>`;
  const pbad = clean && pend && (pend.verdict === 'fail' || pend.verdict === 'review');
  const accepted = c && c.status === 'accepted';
  if (bad) btns = `<button class="outdanger" data-act="entryconfirm">${r.verdict === 'fail' ? 'Record FAIL' : 'Accept for review'}</button>`;
  else if (pbad) btns = `<button class="outdanger" data-act="entryconfirm">${pend.verdict === 'fail' ? 'Record FAIL' : 'Accept for review'}</button>`;
  else btns = `<button class="primary" data-act="entrygo" ${v || pend ? '' : 'disabled'}>${clean && accepted ? 'Next test' : 'Accept'} <kbd style="color:inherit">Enter</kbd></button>`;
  const bt = ui.batch && ui.batch.test === ui.test && ui.batch.value === v ? ui.batch : null;
  let panel = '';
  if (bt) {
    const n = bt.boards.length;
    panel = `<div class="batchpanel callout stop" id="batchpanel"><b>${esc(bt.verdict === 'fail' || bt.verdict === 'review' ? bt.verdict.toUpperCase() : 'STOP RULE')}: ${esc(v)}${bt.unit ? ' ' + esc(bt.unit) : ''} on ${n} board${n === 1 ? '' : 's'}.</b> Nothing is recorded until you confirm.
      <ul>${bt.boards.map(x => `<li>${esc(x.board)}: <span class="vd ${x.verdict}">${esc(x.verdict === 'none' ? 'NO VERDICT' : x.verdict.toUpperCase())}</span>${x.stop ? ` <b class="stoprule">STOP: ${esc(x.stop)}</b>` : ''}</li>`).join('')}</ul>
      <div class="row"><button class="outdanger" data-act="batchconfirm">Accept ${bt.verdict === 'fail' ? 'FAIL' : bt.verdict === 'review' ? 'review' : 'and stop'} on all ${n} board${n === 1 ? '' : 's'}</button><button data-act="batchcancel">Cancel</button></div></div>`;
  }
  const allBtn = `<button data-act="entryall" ${v ? '' : 'disabled'} title="Apply the typed value to every applicable board that is still empty for this test (Shift+Enter)">Apply to all empty boards <kbd style="color:inherit">Shift+Enter</kbd></button>`;
  el.innerHTML = `<div class="pv">${pv}</div>${panel}<div class="btns">${btns}${allBtn}<span class="hint">Esc clears &middot; Up/Down tests &middot; Alt+Left/Right boards</span></div>
    <div class="hint batchhint" id="batchhint">Enter: this board &middot; Shift+Enter: all empty boards</div>`;
}
const props = c => c && c.proposals && c.proposals.length;

let pvTimer;
function onInput() {
  const inp = $('#val'); ui.draft = inp.value; ui.armed = ui.armed === inp.value.trim() ? ui.armed : null; if (ui.useVal !== inp.value.trim()) ui.useVal = null;
  const v = inp.value.trim(); updateEntryCtl();
  clearTimeout(pvTimer);
  if (!v) { ui.pv = null; return updateEntryCtl(); }
  pvTimer = setTimeout(async () => {
    const r = await api('/api/preview', { test_id: ui.test, value: v });
    if (r.ok && $('#val') && $('#val').value.trim() === v) { ui.pv = { ...r.data, value_in: v }; updateEntryCtl(); }
  }, 50);
}

async function submitEntry(confirm) {
  const { t, c, st } = cur(); const inp = $('#val'); if (!t || inp.disabled) return;
  const v = inp.value.trim(), b = ui.board, base = inp.dataset.base || '';
  const pend = c && c.status !== 'accepted' && props(c) ? c.proposals[0] : null;
  if (!v || (v === base && pend)) {                       // the box holds the instrument's proposal (or nothing): accept the proposal
    if (pend) return acceptProp(pend.id, confirm || pend.verdict === 'pass' || pend.verdict === 'none' ? confirm : false, pend);
    if (c && settled(c)) return advance();
    return;
  }
  if (v === base && c && c.status === 'accepted') return advance();     // unchanged: nothing to record
  let replace = false;
  const redo = ui.redo === b + '|' + t.id;
  if (redo) replace = true;
  else if (c && c.status === 'accepted' && c.value !== v) {
    if (ui.armed !== v && (c.source || {}).kind !== 'action' && ui.useVal !== v) { ui.armed = v; updateEntryCtl(); return; }   // an override of an instrument value is deliberate: Enter once; a typed value asks twice
    replace = true;
  }
  const r = await api('/api/cmd/entry', { board: b, test_id: t.id, value: v, confirm: !!confirm, replace, redo });
  if (r.ok) {
    ui.redo = null; ui.armed = null; ui.draft = ''; ui.pv = null; if (inp) { inp.value = ''; inp.dataset.ctx = ''; }
    await refresh();
    if (r.data.stop) { toast('STOP: ' + r.data.stop, true); return; }
    const sm = simOf(b, t.id);
    if (sm) toast('Not similar to the batch: ' + sm.text.replace(/^\u2260 batch: /, ''), 'warn');
    return advance();
  }
  if (r.data.needs_confirm) { toast(r.data.error + '. Use the ' + (r.data.verdict === 'fail' ? 'Record FAIL' : 'Accept for review') + ' button to record it.', true); const rr = await api('/api/preview', { test_id: t.id, value: v }); ui.pv = { ...rr.data, value_in: v }; updateEntryCtl(); placeToast($('#toast')); return; }
  if (r.data.needs_replace) { ui.armed = v; updateEntryCtl(); return; }
  toast(r.data.error || 'rejected', true);
}
async function submitBatch(confirm) {
  const { t } = cur(); const inp = $('#val'); if (!t || !inp || inp.disabled) return;
  const v = inp.value.trim(); if (!v) { toast('Type a value first, then Shift+Enter applies it to all empty boards.', true); return; }
  const r = await api('/api/cmd/entry_batch', { test_id: t.id, value: v, confirm: !!confirm });
  if (r.ok) {
    ui.batch = null; ui.armed = null; ui.draft = ''; ui.pv = null; inp.value = '';
    await refresh();
    const d = r.data, u = d.unit ? ' ' + d.unit : '';
    let m = `${d.test_id}: ${d.value}${u} on ${d.recorded.join(' ')}`;
    const extra = [];
    if (d.have.length) extra.push(`${d.have.join(' ')} already had a value`);
    if (d.blocked.length) extra.push(`${d.blocked.join(' ')} gated or stopped`);
    if (extra.length) m += ` (${extra.join('; ')})`;
    if (d.stops.length) { toast('STOP on ' + d.stops.map(x => x.board).join(' ') + ': ' + d.stops[0].stop, true); return; }
    toast(m); return advance();
  }
  if (r.data.needs_confirm_batch) { ui.batch = { test: t.id, value: v, verdict: r.data.verdict, unit: r.data.unit, boards: r.data.boards }; updateEntryCtl(); const b = $('#batchpanel button'); b && b.focus(); return; }
  toast(r.data.error || 'rejected', true);
}
async function acceptProp(id, confirm, p) {
  const { t } = cur();
  const needs = p && (p.verdict === 'fail' || p.verdict === 'review');
  if (needs && !confirm) { toast('A ' + p.verdict.toUpperCase() + ' is recorded only with its own button.', true); return; }
  const c = cellOf(ui.board, t.id);
  const r = await cmd('accept', { board: ui.board, test_id: t.id, proposal_id: id, confirm: !!confirm, replace: !!(c && c.status === 'accepted') }, true);
  if (r.ok) { if (r.data.stop) toast('STOP: ' + r.data.stop, true); else advance(); } else toast(r.data.error, true);
}

function advance() {
  const st = curStep(); const all = testsOf(st).map(x => x.t.id); const bs = S.boards.filter(b => applicable(st, b.label)).map(b => b.label);
  const order = [], open = [];
  if (advMode(st) === 'board') { for (let ti = 0; ti < all.length; ti++) for (let bi = 0; bi < bs.length; bi++) order.push([bs[bi], all[ti]]); }
  else { for (let bi = 0; bi < bs.length; bi++) for (let ti = 0; ti < all.length; ti++) order.push([bs[bi], all[ti]]); }
  const start = order.findIndex(([b, t]) => b === ui.board && t === ui.test);
  for (let k = 1; k <= order.length; k++) {
    const [b, t] = order[(start + k) % order.length];
    const c = cellOf(b, t);
    if (settled(c)) continue;
    if (!secAp(st, secOfTest(st, t), b)) continue;                       // a Tier 2 test for a board that was not sent there is not an applicable cell
    if (sb(st, b).gate && !sb(st, b).gate.ok) { open.push({ b, why: 'gate' }); continue; }
    ui.board = b; ui.test = t; ui.pv = null; ui.draft = ''; ui.armed = null; render(); return;
  }
  render(); toast(advanceSummary(open), open.length ? 'warn' : false);
}
function moveTest(d) {
  /* UIR-17-R3: Up/Down walk the rows that are showing and skip folded groups. When nothing is showing in that direction, the
     adjacent test is taken and its group opens, so the focus is never on a row nobody can see. */
  const st = curStep(), items = testsOf(st), all = items.map(x => x.t.id), i = all.indexOf(ui.test);
  let j = -1;
  for (let k = i + d; k >= 0 && k < all.length; k += d) if (secOpen(st, items[k].sec)) { j = k; break; }
  if (j < 0) { j = Math.max(0, Math.min(all.length - 1, i + d)); if (j !== i && items[j]) ui.secOpen[secKey(st, items[j].sec)] = true; }
  ui.test = all[j]; ui.pv = null; ui.draft = ''; ui.armed = null; render();
}
function moveBoard(d) {
  const st = curStep(); const bs = S.boards.filter(b => applicable(st, b.label)).map(b => b.label);
  const i = bs.indexOf(ui.board); ui.board = bs[(i + d + bs.length) % bs.length]; ui.pv = null; ui.draft = ''; ui.armed = null; if (st.kind === 'assembly') asmEmpty(); render();
}

/* ---- Decision gate (Tier 1 -> Tier 2): the app proposes, the operator accepts or rejects */
function viewGate(el, st) {
  const g = (S.gates || []).find(x => x.index === ui.gate);
  const rows = g.boards.map(r => {
    let v, act = '';
    if (r.assigned) v = '<span class="vd review">SENT TO TIER 2</span>';
    else if (r.rejected) v = `<span class="vd none">REJECTED</span> <span class="muted">${esc(r.rejected)}</span>`;
    else if (r.verdict === 'tier2') { v = '<span class="vd fail">TIER 2</span>'; act = `<button class="primary sm" data-act="gateacc" data-i="${g.index}" data-b="${r.board}">Accept</button> <button class="sm" data-act="gaterej" data-i="${g.index}" data-b="${r.board}">Reject&hellip;</button>`; }
    else if (r.verdict === 'outliers') v = '<span class="vd review">OUTLIER, CONTINUE</span>';
    else if (r.verdict === 'clean') v = '<span class="vd pass">TIER 2 SKIPPED</span>';
    else v = '<span class="vd none">WAITING</span>';
    const why = r.reasons.length ? r.reasons.map(esc).join('<br>') : r.outliers.length ? r.outliers.map(esc).join('<br>') + '<br><span class="muted">the board is powered; the powered gate sends it to Tier 2</span>' : r.verdict === 'clean' ? 'every Tier 1 reading in band, no stop rule, no outlier' : '';
    return `<tr><td class="mono">${r.board}</td><td class="mono">${r.settled}/${r.total}</td><td>${v}</td><td class="why">${why}</td><td class="acts">${act}</td></tr>`;
  }).join('');
  const prop = g.boards.filter(r => r.verdict === 'tier2' && !r.assigned && !r.rejected).map(r => r.board);
  el.innerHTML = `<div class="pane"><div class="crumb"><span>${esc(st.title)}</span><span class="sep">/</span><span>Decision gate</span></div>
    <h1 class="tt">${esc(g.title)}</h1>
    ${(g.halt || []).map(h => `<div class="callout stop"><b>Stop the batch.</b> ${esc(h)}</div>`).join('')}
    <div class="instr"><p>The gate reads the recorded Tier 1 values against the limits, the stop rules and the batch-median Outlier bands of the plan, and proposes which boards go to Tier 2. Nothing moves until you accept.
      ${g.hold_power ? ' A board with an out-of-band unpowered reading cannot be powered until its Tier 2 is settled and Tier 1 unpowered has been repeated (power interlock).' : ''}</p></div>
    <table class="gatet"><tr><th>Board</th><th>Tier 1 settled</th><th>Verdict</th><th>Reason</th><th></th></tr>${rows}</table>
    <div class="navrow">${prop.length ? `<button class="primary" data-act="gateacc" data-i="${g.index}" data-b="${prop.join(',')}">Accept all proposals (${prop.join(' ')})</button>` : '<span class="muted">No proposal waiting.</span>'}</div></div>`;
}
function viewRetired(el, st) {
  const r = S.retired.find(x => x.id === ui.retired);
  const rows = Object.entries(r.results).map(([b, x]) => `<tr><td class="mono">${b}</td><td class="mono">${x.status === 'skipped' ? 'skipped' : esc(x.value) + ' ' + esc(x.unit || '')}</td><td><span class="vd ${x.verdict || 'none'}">${(x.verdict || '').toUpperCase()}</span></td><td class="muted">attempt ${x.attempt}</td></tr>`).join('');
  el.innerHTML = `<div class="pane"><div class="crumb"><span>${esc(st.title)}</span><span class="sep">/</span><span>Retired</span></div>
    <h1 class="tt"><span class="id">${esc(r.id)}</span>${esc(r.title) || 'removed from the plan'} <span class="badge">RETIRED</span></h1>
    <div class="instr"><p>The current plan no longer has this test. Its recorded results stay in the journal and in the report.</p></div>
    <table class="across">${rows || '<tr><td class="muted">No result was recorded.</td></tr>'}</table></div>`;
}

/* ---- assembly */
const asmOf = l => (S.asm && S.asm[String(l.line)]) || { state: 'todo', done: [], missing: [], partial: [], per_board: l.designators.length, boards: S.boards.length };
const boardsTxt = bs => bs.length === S.boards.length ? `all ${bs.length} boards` : bs.length > 6 ? `${bs.slice(0, 5).join(' ')} ... (${bs.length} of ${S.boards.length})` : bs.join(' ');
/* ---- Assembly aid list order ("List by"). Only the to-do list is re-sorted: placements, the log and the protocol are untouched.
   hand = small and low first, from the server's partsort rank (RANK); DNF lines stay at the bottom in every mode. */
const LISTBY = [['hand', 'Hand assembly: small and low first'], ['protocol', 'Protocol order'], ['designator', 'Designator'], ['remaining', 'Most remaining']];
let RANK = {};
const listBy = () => { const v = lsGet('listby', 'hand'); return LISTBY.some(o => o[0] === v) ? v : 'hand'; };
const natCmp = (a, b) => { const x = String(a).split(/(\d+)/).filter(Boolean), y = String(b).split(/(\d+)/).filter(Boolean);
  for (let i = 0; i < Math.min(x.length, y.length); i++) { const dx = /^\d+$/.test(x[i]), dy = /^\d+$/.test(y[i]);
    const c = dx && dy ? Number(x[i]) - Number(y[i]) : x[i].toLowerCase() < y[i].toLowerCase() ? -1 : x[i].toLowerCase() > y[i].toLowerCase() ? 1 : 0; if (c) return c; }
  return x.length - y.length; };
const firstDes = l => [...l.designators].sort(natCmp)[0] || '';
const handRank = l => (RANK[l.line] ? RANK[l.line].hand_rank : 1e6 + l.order);
const remaining = l => l.dnf ? 0 : l.designators.length * (S.boards.length - asmOf(l).done.length);
function bomList() {
  const mode = listBy(), by = {
    hand: (a, b) => handRank(a) - handRank(b),
    protocol: (a, b) => a.order - b.order || a.line - b.line,
    designator: (a, b) => natCmp(firstDes(a), firstDes(b)) || a.line - b.line,
    remaining: (a, b) => remaining(b) - remaining(a) || handRank(a) - handRank(b),
  }[mode];
  return [...S.bom].sort((a, b) => (a.dnf ? 1 : 0) - (b.dnf ? 1 : 0) || by(a, b));
}
async function loadRank() { const r = await api('/api/assembly/rank'); if (r.ok) RANK = r.data; }
const rankTip = l => { const r = RANK[l.line]; return r ? '\n' + r.why + (r.size_unknown ? ' (size unknown)' : '') : ''; };
const firstOpenLine = () => (bomList().find(l => !l.dnf && asmOf(l).state !== 'done') || S.bom[0] || {}).line;
function tickSet(line) {          // boards ticked for this line: every board that does not yet have it, until the operator changes it
  if (!ui.tick || ui.tick.line !== line.line) ui.tick = { line: line.line, set: new Set(S.boards.map(b => b.label).filter(b => !asmOf(line).done.includes(b))), touched: false };
  return ui.tick.set;
}
function asmTotals() {
  const fit = S.bom.filter(l => !l.dnf); const nb = S.boards.length;
  const lines = fit.filter(l => asmOf(l).state === 'done').length;
  const parts = fit.reduce((a, l) => a + asmOf(l).done.length * l.designators.length, 0);
  const all = fit.reduce((a, l) => a + l.designators.length * nb, 0);
  return { lines, nlines: fit.length, parts, all };
}
function logHtml(rows, n) {
  rows = rows.slice(-n).reverse();
  if (!rows.length) return '<div class="faint">Placements and scans appear here, with who and when.</div>';
  return rows.map(r => {
    const who = r.operator ? esc(r.operator) : '<span class="faint">no operator</span>';
    let what;
    if (r.kind === 'place' || r.kind === 'clear') what = `${r.kind === 'place' ? 'placed' : 'cleared'} <b>${esc((r.designators || []).join(' '))}</b>${r.line ? ' (line ' + r.line + ')' : ''} on ${esc(boardsTxt(r.boards))} <span class="faint">${r.auto ? 'auto-place, ' : ''}${esc(r.via || '')}${r.code ? ', scan ' + esc(r.code) : ''}</span>`;
    else if (r.kind === 'begin' || r.kind === 'skip' || r.kind === 'unskip') what = `<b>${({ begin: 'Assemble pressed', skip: 'Assembly skipped (boards already built)', unskip: 'Skip reversed: Assemble instead' })[r.kind]}</b>`;
    else if (r.kind === 'auto_cancel') what = `<b>auto-place cancelled</b> for line ${r.line} <span class="faint">(${esc(r.reason || '')}), nothing placed</span>`;
    else if (r.kind === 'scan') what = `scan <span class="mono">${esc(r.code)}</span> &rarr; line ${r.line} <span class="faint">${esc(r.via || '')}, matched ${esc(r.matched_by || '')}${r.qty != null ? ', label qty ' + r.qty : ''}</span>`;
    else what = `scan <span class="mono">${esc(r.code)}</span> &rarr; <span style="color:var(--fail)">no BOM line</span> <span class="faint">${esc(r.via || '')}</span>`;
    return `<div class="lg"><span class="faint mono">${esc((r.ts || '').slice(11, 19))}</span> <b>${who}</b> ${what}</div>`;
  }).join('');
}
/* UIR-20: the start page of the Assembly aid, and the page shown once it was skipped. Neither builds the scanner or opens the camera. */
const ASM_ICON = `<svg class="asmicon" viewBox="0 0 64 64" width="56" height="56" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22h28l4 8v26H8V30z"/><path d="M12 22l4-8h20l4 8"/><path d="M16 36v14M21 36v14M25 36v14M31 36v14M36 36v14" stroke-width="2"/><path d="M52 8a8 8 0 0 1 8 8c0 6-8 14-8 14s-8-8-8-14a8 8 0 0 1 8-8z"/><circle cx="52" cy="16" r="2.6"/></svg>`;
function viewAssemblyStart(el, st, mode) {
  const nobom = !S.bom.length;
  el.innerHTML = `<div class="pane asm"><div class="crumb"><span>${esc(st.title)}</span></div>
    <div class="asmstart ${mode}" id="asmstart"><div class="asmhead">${ASM_ICON}<div><h2>${mode === 'skipped' ? 'Assembly skipped' : 'Assemble the boards here?'}</h2>
      ${mode === 'skipped'
        ? `<p class="muted">Skipped (boards already built). You can still change your mind and assemble.</p>`
        : `<p class="muted">Scan a component bag and see where it goes on your board in ${esc(ecadName())}. Needs a matched BOM.</p>`}</div></div>
      ${nobom ? `<div class="callout stop" style="margin:10px 0">This batch has no BOM, so there is nothing to place. Add one on the Project page.</div>` : ''}
      <div class="asmbtns">${mode === 'skipped'
        ? `<button class="primary big" data-act="asmunskip" id="asmunskip" ${nobom ? 'disabled' : ''}>Assemble instead</button>`
        : `<button class="primary big" data-act="asmbegin" id="asmbegin" ${nobom ? 'disabled' : ''}>Assemble</button><button class="big" data-act="asmskip" id="asmskip">Boards are already built, skip</button>`}</div>
      <p class="faint small">${mode === 'skipped' ? 'Both the skip and reversing it are written to the journal.' : 'The camera stays off until you press Assemble. Skipping is written to the journal and can be reversed here.'}</p></div>
    <div class="box" style="margin-top:12px"><h3>Assembly log <span class="sp"></span><button class="sm" data-act="asmjournal" title="Opens the Journal with the Assembly filter">Open in Journal</button></h3><div class="lgbox" id="lgbox">${logHtml(S.alog || [], 6)}</div></div></div>`;
}
function viewAssembly(el, st) {
  const am = asmMode(); if (am !== 'assembling') return viewAssemblyStart(el, st, am);
  const line = ui.noSel ? null : (S.bom.find(l => l.line === ui.line) || S.bom[0]);
  const tot = asmTotals();
  const oldScan = $('#scan'), keepScan = oldScan ? oldScan.value : '';
  let h = `<div class="pane asm"><div class="crumb"><span>${esc(st.title)}</span><span class="sep">/</span><span><b>${tot.lines}</b> of ${tot.nlines} lines done</span><span class="sep">/</span><span>${tot.parts} of ${tot.all} parts placed on ${S.boards.length} boards</span></div>
    <div class="scanbox"><div class="scancol"><div class="scan"><input id="scan" placeholder="Scan or type an MPN, part number or designator, Enter" autocomplete="off" spellcheck="false"><button data-act="scango" class="primary">Find</button></div>
        <div class="scanmsg" id="scanmsg">${ui.scanMsg ? `<span style="color:var(--${ui.scanMsg.ok ? 'pass' : 'fail'})">${esc(ui.scanMsg.text)}</span>` : `<span class="faint">A scanned bag or a picked line selects its designators in ${ecadName()}. Enter places the line on the ticked boards.</span>`}</div>
        <div id="autoslot">${autoBannerHtml()}</div>
        <div id="scslot"></div>
        <div class="faint small">Ctrl+K scan box &middot; Up/Down lines &middot; Enter places the ticked boards, then waits for the next scan</div></div></div>`;
  if (ui.noSel) h += `<div class="bomcard waitcard" id="waitcard">${ui.waitFresh ? `<div class="waitbig fresh">Scan a part to begin, or pick a line from the list</div>` : `<div class="waitbig">${esc(ui.waitTitle || 'Waiting for next scan')}</div>
      ${ui.waitMsg ? `<div class="muted">${esc(ui.waitMsg)}</div>` : ''}<div class="muted small">Selection in ${ecadName()} cleared. Scan the next bag, or take the next line on the list.</div>`}
      <div class="navrow" style="margin:12px 0 4px"><button class="primary big" data-act="nextlist" id="nextlist">Next on list <kbd style="color:inherit">Enter</kbd></button></div></div>`;
  else if (!line) h += `<div class="empty">The protocol has no BOM.</div>`;
  else {
    const a = asmOf(line), ticks = tickSet(line), nb = S.boards.length, k = line.designators.length, nt = ticks.size;
    const idx = bomList().findIndex(l => l.line === line.line) + 1;
    h += `<div class="bomcard"><div class="crumb">Placement step <b>${idx}</b> of ${S.bom.length} &middot; BOM line ${line.line} &middot; ${esc(line.side || '')}
        ${NOTES.mk('line', line.line)}<span class="linestate st-${a.state}">${({ done: 'done on all boards', partial: 'partly done', todo: 'to do', dnf: 'not fitted' })[a.state]}</span></div>
      <div class="cntrow"><div class="cntcol">${line.dnf ? `<div class="bigcount dnf">DO NOT FIT</div>` : `<div class="bigcount" id="bigcount"><span class="n">${k}</span><span class="u"> per board</span><span class="tot"> &middot; ${k * nt} for ${nt} board${nt === 1 ? '' : 's'}</span></div>`}
      <div class="chips big">${line.designators.map(d => `<span class="dchip big${line.dnf ? ' dnfc' : ''}">${esc(d)}</span>`).join('')}</div></div>
      ${!line.dnf && line.polarity && !/^none/i.test(line.polarity) ? `<div class="polslot" id="polslot" data-line="${line.line}">${polHtml(line)}</div>` : ''}</div>
      <div class="mpn">${esc(line.mpn)}</div><div class="desc">${esc([line.value, line.package].filter(Boolean).join('  |  '))}</div>
      <div class="scres" id="scres">${scResultHtml()}</div>
      ${line.dnf ? `<div class="flagbox dnf">DO NOT FIT. ${esc(line.note || '')} Leave ${esc(line.designators.join(', '))} empty.</div>` : ''}
      ${line.polarity && !/^none/i.test(line.polarity) ? `<div class="flagbox pol">POLARITY: ${esc(line.polarity)}</div>` : ''}
      ${line.note && !line.dnf ? `<div class="muted" style="margin-bottom:8px">${esc(line.note)}</div>` : ''}
      ${line.dnf ? '' : `<div class="bsel"><div class="blabel"><b>Boards</b> <span class="muted">tick the boards you placed this on</span><span class="sp"></span>
          <button class="sm" data-act="tickall">All</button><button class="sm" data-act="tickrest">Not done</button><button class="sm" data-act="tickno">None</button></div>
        <div class="bchecks" id="bchecks">${S.boards.map(b => { const dn = a.done.includes(b.label), pt = a.partial.includes(b.label), on = ticks.has(b.label);
          return `<button class="bcheck${on ? ' on' : ''}${dn ? ' isdone' : ''}" data-act="btick" data-b="${b.label}" aria-pressed="${on}" title="${b.label}${dn ? ': already has this line' : pt ? ': partly placed' : ''}"><i class="cb">${on ? '&#10003;' : ''}</i>${b.label}${dn ? '<small>done</small>' : pt ? '<small>part</small>' : ''}</button>`; }).join('')}</div>
        ${a.state === 'partial' ? `<div class="muted small" style="margin-top:4px">Missing on: ${esc(a.missing.join(' '))}</div>` : ''}</div>`}
      <div class="navrow" style="margin:10px 0 8px">
        ${line.dnf ? `<button class="primary big" data-act="placeall" id="placeall">Not fitted: wait for next scan <kbd style="color:inherit">Enter</kbd></button>`
          : nt ? `<button class="primary big" data-act="placeall" id="placeall">Placed on all selected boards (${nt}) <kbd style="color:inherit">Enter</kbd></button>`
            : `<button class="primary big" data-act="placeall" id="placeall">Nothing ticked: wait for next scan <kbd style="color:inherit">Enter</kbd></button>`}
        <span class="sp"></span><button data-act="prev">&uarr; Previous</button><button data-act="next">&darr; Skip to next</button></div>
      <div class="ecadline"><span class="dotc" id="ecdot"></span><span id="ectext">ECAD: checking</span><button class="sm" data-act="hl" id="hlagain">Highlight again</button></div>
      ${line.dnf ? '' : `<details class="exc" ${ui.excOpen ? 'open' : ''}><summary>Exceptions on a single board</summary>
        <div class="excb">${S.boards.map(b => `<button class="bchip${b.label === ui.board ? ' cur' : ''}" data-act="board" data-id="${b.label}">${b.label}</button>`).join('')}</div>
        <div class="chips">${line.designators.map(d => `<button class="dchip${(S.placed[ui.board] || []).includes(d) ? ' on' : ''}" data-act="place" data-d="${esc(d)}">${esc(d)}</button>`).join('')}</div>
        <div class="muted small">${ui.board}: click a designator to mark it placed or not placed on this board alone.</div></details>`}
    </div>`;
  }
  h += `<div class="box" style="margin-top:12px"><h3>Assembly log <span class="sp"></span><button class="sm" data-act="asmjournal" title="Opens the Journal with the Assembly filter">Open in Journal</button></h3><div class="lgbox" id="lgbox">${logHtml(S.alog || [], 6)}</div></div></div>`;
  el.innerHTML = h;
  const sbox = $('#scan');
  if (sbox) { sbox.value = keepScan; if (!document.querySelector('.veil')) sbox.focus(); }
  const slot = $('#scslot'); if (slot) slot.replaceWith(scNode());
  ecadLine();
}
async function ecadLine() {
  const d = $('#ecdot'), t = $('#ectext'); if (!d || !t) return;
  const st = sc.st; if (!st) { t.textContent = 'ECAD: checking'; return; }
  const hlb = $('#hlagain'); if (hlb) hlb.disabled = !!st.ecad_off;
  if (st.ecad_off) { d.className = 'dotc'; t.innerHTML = '<b>ECAD disabled in step 1</b> <span class="muted">(continued without ECAD: nothing is sent to ' + esc(ecadName()) + ')</span>'; return; }
  const b = st.board || {}, r = st.result;
  d.className = 'dotc' + (b.ok ? ' on' : '');
  let h = esc(ecadName()) + ': ' + (b.connected ? `connected &middot; board <b>${esc(b.name || 'none')}</b> (${esc(b.mode)})` : `<b style="color:var(--fail)">not connected</b>`);
  if (!b.ok && b.reason) h += ` | <span style="color:var(--fail)">${esc(b.reason)}</span>`;
  if (r) {
    if (r.pending) h += ` | selecting in ${ecadName()}...`;
    else if (r.highlighted && r.highlighted.length) h += ` | selected ${r.highlighted.length}: ${esc(r.highlighted.join(' '))}`;
    if (r.not_on_board && r.not_on_board.length) h += ` | <b style="color:var(--fail)">not on the board: ${esc(r.not_on_board.join(' '))}</b>`;
    if (r.fusion_error) h += ` | <span style="color:var(--fail)">${esc(r.fusion_error)}</span>`;
    if (r.blocked) h += ` | <span style="color:var(--fail)">not selected: ${esc(r.blocked)}</span>`;
  }
  t.innerHTML = h;
}
function ecadTextFor(s, lineNo) {
  const line = S.bom.find(l => l.line === lineNo);
  if (S.project && S.project.ecad_skipped) return 'ECAD: <b>off</b> (continued without ECAD: ' + esc(S.project.ecad_skipped.reason) + '). Highlighting is disabled.';
  const conn = s.connected ? `connected &middot; ${esc(s.program || '')}${s.version ? ' ' + esc(s.version) : ''} &middot; ${esc(s.document || 'no document')}${s.detail ? ' (' + esc(s.detail) + ')' : ''}` : `<b style="color:var(--fail)">not connected</b>${s.detail ? ' (' + esc(s.detail) + ')' : ''}`;
  const last = S.ecad_last || {}, res = last.result;
  let hl = '';
  if (line && last.designators && last.designators.join(' ') === line.designators.join(' ') && res) {
    if (res.error) hl = ` | <span style="color:var(--fail)">${esc(res.error)}</span>`;
    else if (res.ok === false) hl = ` | <span style="color:var(--fail)">${esc(res.message || 'ECAD refused the highlight')}</span>`;
    else { const n = (res.highlighted || last.designators).length; hl = ` | Highlighted ${n}: ${esc((res.highlighted || last.designators).join(' '))}${res.simulated ? ' (simulated)' : ''}`;
      if (res.missing && res.missing.length) hl += ` | <b style="color:var(--fail)">not found in the document: ${esc(res.missing.join(' '))}</b>`; }
  } else if (line && !line.dnf) hl = ' | not highlighted yet';
  return 'ECAD: ' + conn + hl;
}
/* ---- embedded Assembly Scanner: camera, decoder, resolver and Fusion selection run in the server; this is its page.
   The camera is released when the Assembly step is left (heartbeat + /api/scanner/leave) or when paused/stopped. */
const sc = { st: null, timer: null, node: null, fseq: undefined, optSig: '', streaming: false };
const asmMode = () => (S && S.assembly && S.assembly.mode) || 'assembling';          // start | assembling | skipped (UIR-20)
/* The scanner, its stream and its polling exist only while the Assembly aid is assembling: the start and skipped pages never touch /api/scanner (UIR-20-R3). */
const onAssemblyPage = () => { const st = S && curStep(); return !!st && st.kind === 'assembly' && ui.test === null && !document.hidden && asmMode() === 'assembling'; };
function scNode() {
  if (sc.node) return sc.node;
  const n = document.createElement('div'); n.id = 'scanner'; n.className = 'scanner';
  n.innerHTML = `<div class="scpv"><img id="scimg" alt="camera preview"><div class="scroi" id="scroi"></div><div class="scov" id="scov"></div><div class="scmsg" id="scmsg2"></div></div>
    <div class="scctl"><button class="sm" data-act="scpause" id="scpause">Pause</button><button class="sm" data-act="scstop" id="scstop">Stop</button>
      <label class="sw"><input type="checkbox" id="scmirror"> Mirror</label>
      <label class="sw autochk" title="Scanning the next part places the previous line after 3 s; Esc cancels"><input type="checkbox" id="autochk"> Auto-place on scan</label>
      <label class="scsel" title="Changes only the order of the to-do list">List by <select id="listby">${LISTBY.map(o => `<option value="${o[0]}">${o[1]}</option>`).join('')}</select></label>
      <label class="scsel">Camera <select id="sccam"></select></label><label class="scsel">Board <select id="scboard"></select></label>
      <button class="sm" data-act="screscan" title="Re-list the boards open in ${ecadName()} and pick again">Rescan boards</button></div>
    <div class="scstat" id="scstat"></div>`;
  n.querySelector('#listby').value = listBy();
  sc.node = n; return n;
}
function scStream(on) {
  const img = $('#scimg'); if (!img) return;
  if (on && !sc.streaming) { img.src = '/api/scanner/stream.mjpg?t=' + Date.now(); sc.streaming = true; }
  if (!on && sc.streaming) { img.removeAttribute('src'); sc.streaming = false; }
}
function scannerSync() {          // after every render: poll and stream only while the Assembly page is showing
  if (onAssemblyPage()) { if (!sc.timer) { sc.timer = setInterval(scannerPoll, 500); scannerPoll(); } scStream(true); }
  else if (sc.timer) { clearInterval(sc.timer); sc.timer = null; scStream(false); api('/api/scanner/leave', {}); }
}
/* Page Visibility (not focus): hidden or minimised pauses capture and decoding on the server at once; a visible but unfocused
   half-screen window keeps scanning. The 6 s no-heartbeat pause in the service stays as a backstop. */
document.addEventListener('visibilitychange', async () => {
  if (document.hidden) autoCancel('hidden');
  if (sc.timer || sc.st) {   // only once the Assembly aid has used the scanner (UIR-20-R3)
    let beaconed = false;      // a fetch started while the page unloads is cancelled and WebKit logs it as an access-control error (UIR-36); a beacon is not
    if (document.hidden) { try { beaconed = navigator.sendBeacon('/api/scanner/visibility', new Blob(['{"visible":false}'], { type: 'application/json' })); } catch { /* fall through to fetch */ } }
    if (!beaconed) await api('/api/scanner/visibility', { visible: !document.hidden });
  }
  if (S) scannerSync();
});
window.addEventListener('pagehide', () => { if (!(sc.timer || sc.st)) return; try { navigator.sendBeacon('/api/scanner/leave', new Blob(['{}'], { type: 'application/json' })); } catch { /* gone */ } });
async function scannerPoll() {
  if (!onAssemblyPage()) return;
  const r = await api('/api/scanner/state?hb=1'); if (!r.ok) return;
  sc.st = r.data; scannerPaint();
  const f = sc.st.fired || {};
  if (sc.fseq === undefined) sc.fseq = f.seq;
  else if (f.seq > sc.fseq) { sc.fseq = f.seq; onCameraScan(f); }
}
async function onCameraScan(f) {
  const ln = S.bom.find(l => l.line === f.line);
  if (f.ok && ln) {
    await scanTakes(ln, f.code); ui.lastScanLine = f.line; ui.lastScanCode = f.code;
    ui.scanMsg = { ok: true, text: `${ln.mpn} (matched ${f.code}, camera)` }; beep(true);
  } else { ui.scanMsg = { ok: false, text: `Label seen, no BOM line matches: ${f.code || ''}` }; beep(false); }
  refresh();
}
function scResultHtml() {
  const st = sc.st; if (!st || !st.result) return '<div class="faint small">Scan a part bag (camera, or the box above) to see the scanner result here.</div>';
  const items = (st.hud && st.hud.bottom) || [];
  return '<div class="sch">Scanner result</div>' + items.map(([t, lv, sz]) => `<div class="scr lv-${esc(lv)} sz-${esc(sz)}">${esc(t)}</div>`).join('');
}
function scannerPaint() {
  const st = sc.st, n = sc.node; if (!st || !n) return;
  const set = (id, fn) => { const e = $('#' + id, n); if (e) fn(e); };
  set('scpause', e => { e.textContent = st.paused ? 'Resume' : 'Pause'; e.disabled = !st.enabled; });
  set('scstop', e => { e.textContent = st.enabled ? 'Stop' : 'Start'; });
  set('scmirror', e => { if (document.activeElement !== e) e.checked = !!st.mirror; });
  set('autochk', e => { e.checked = !!ui.autoOn; });
  set('listby', e => { if (document.activeElement !== e) e.value = listBy(); });
  const cams = st.camera.options.map(o => `${o.index}:${o.name}`).join('|'), bsig = (st.board.options || []).map(o => o.key + o.name).join('|') + '|' + st.board.mode;
  if (sc.optSig !== cams + '#' + bsig) {
    sc.optSig = cams + '#' + bsig;
    set('sccam', e => { e.innerHTML = st.camera.options.map(o => `<option value="${o.index}">${esc(o.index + ': ' + o.name)}</option>`).join(''); });
    set('scboard', e => { e.innerHTML = '<option value="">Auto</option>' + (st.board.options || []).map(o => `<option value="${esc(o.key)}">${esc(o.name)}</option>`).join(''); });
  }
  set('sccam', e => { if (document.activeElement !== e) e.value = String(st.camera.index); });
  set('scboard', e => { if (document.activeElement !== e) e.value = st.board.mode === 'manual' ? (st.board.pinned || '') : ''; });
  set('scov', e => { e.innerHTML = ((st.hud && st.hud.top) || []).map(([t, lv]) => `<div class="lv-${esc(lv)}">${esc(t)}</div>`).join(''); });
  set('scroi', e => { const r = st.camera.roi; e.style.display = r && !st.idle ? 'block' : 'none'; if (r) Object.assign(e.style, { left: r[0] * 100 + '%', top: r[1] * 100 + '%', width: r[2] * 100 + '%', height: r[3] * 100 + '%' }); });
  set('scmsg2', e => {
    let m = '';
    if (!st.enabled) m = 'Scanner stopped: camera released. Typed and wedge scans still work.';
    else if (st.paused) m = 'Paused: camera released. Press Resume.';
    else if (st.background) m = 'Paused (page in background)';
    else if (st.camera.error) m = st.camera.error;
    else if (st.camera.state !== 'running') m = `Camera ${st.camera.index}: ${st.camera.state}`;
    e.textContent = m; e.style.display = m ? 'flex' : 'none';
  });
  if (st.camera.size) n.style.setProperty('--ar', (st.camera.size[0] / st.camera.size[1]).toFixed(4));
  set('scstat', e => { e.textContent = (st.hud && st.hud.status) || ''; });
  const res = $('#scres'); if (res) res.innerHTML = scResultHtml();
  ecadLine();
}
async function scControl(action, value) { const r = await api('/api/scanner/control', { action, value }); if (r.ok) { sc.st = r.data; scannerPaint(); } else toast(r.data.error || 'scanner command failed', true); }
document.addEventListener('change', e => {
  if (e.target.id === 'autochk') { ui.autoOn = e.target.checked; lsSet('autoplace', ui.autoOn); if (!ui.autoOn) autoCancel('unchecked'); }
  else if (e.target.id === 'scmirror') scControl('mirror', e.target.checked);
  else if (e.target.id === 'listby') { lsSet('listby', e.target.value); loadRank().then(render); }
  else if (e.target.id === 'sccam') scControl('camera', e.target.value);
  else if (e.target.id === 'scboard') scControl('board', e.target.value);
});
let selTimer;
function selectLine(n) { ui.line = n; clearTimeout(selTimer); selTimer = setTimeout(async () => { await cmd('select_line', { line: n }, true); scannerPoll(); }, 120); }
function lineChanged(keepAuto) { ui.tick = null; ui.test = null; ui.noSel = false; ui.waitFresh = false; if (!keepAuto) autoCancel('line_changed'); }
async function doScan(codeIn, via = 'typed', codes = null, fmt = '') {
  const sc = $('#scan'); const code = codeIn != null ? codeIn : (sc ? sc.value.trim() : ''); if (!code) return placeAll();
  const r = await cmd('scan', { code, via, codes, format: fmt }, true); if (sc) sc.value = '';
  if (r.ok && r.data.ok) {
    const ln = S.bom.find(l => l.line === r.data.line); await scanTakes(ln, r.data.code); ui.lastScanLine = r.data.line; ui.lastScanCode = r.data.code;
    ui.scanMsg = { ok: true, text: `${ln ? ln.mpn : 'line ' + r.data.line} (matched ${r.data.matched_by}${via === 'camera' ? ', camera' : ''}${r.data.qty != null ? ', label qty ' + r.data.qty : ''})` };
    beep(true);
  } else { ui.scanMsg = { ok: false, text: via === 'camera' ? `Label seen, no BOM line matches: ${(codes || [code]).slice(0, 3).join(' | ')}` : `No BOM line matches '${code}'` }; beep(false); }
  render(); scannerPoll();
}
let actx;
function beep(ok) { try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); const o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = ok ? 880 : 220; g.gain.value = 0.05; o.connect(g); g.connect(actx.destination); o.start(); o.stop(actx.currentTime + (ok ? 0.08 : 0.25)); } catch { /* no audio */ } }
/* After Enter the app does not advance: it clears the Fusion selection and waits for the next scan (scanning is the main input).
   Enter again with nothing selected, or the Next on list button, takes the next to-do line. */
async function placeAll() {
  if (ui.auto) await autoFlush();                        // Enter after a scan accepts the previous line at once
  const line = S.bom.find(l => l.line === ui.line);
  if (ui.noSel || !line) return nextOnList();
  let title = 'Waiting for next scan', msg = `${line.designators.join(' ')}: not fitted`;
  if (!line.dnf) {
    const boards = [...tickSet(line)];
    if (boards.length) {
      const scanned = ui.lastScanLine === line.line;
      const r = await cmd('place_batch', { line: line.line, boards, via: scanned ? 'scan' : 'button', code: scanned ? (ui.lastScanCode || '') : '' }, true);
      if (!r.ok) { toast(r.data.error || 'could not record', true); return; }
      title = 'Placed. Waiting for next scan'; msg = `${line.designators.join(' ')} on ${boardsTxt(boards)}`;
      toast(`${line.designators.join(' ')} placed on ${boardsTxt(boards)}`);
    } else msg = 'Nothing was ticked, nothing placed.';
  }
  enterWaiting(title, msg, line.line);
}
function enterWaiting(title, msg, fromLine) {
  clearTimeout(selTimer); autoStop();
  ui.lastPlacedLine = fromLine; ui.waitTitle = title; ui.waitMsg = msg; ui.line = null; ui.noSel = true; ui.waitFresh = false; ui.tick = null;
  api('/api/scanner/clear', {}); render(); scannerPoll();
}
function nextOnList() {
  const ls = bomList(), i = ls.findIndex(l => l.line === ui.lastPlacedLine);
  const order = [...ls.slice(i + 1), ...ls.slice(0, i + 1)];
  const nx = order.find(l => !l.dnf && asmOf(l).state !== 'done');
  if (!nx) { toast('Every fitted line is done on every board.'); render(); return; }
  ui.noSel = false; ui.line = nx.line; lineChanged(); render(); selectLine(ui.line);
}
/* Auto-place on scan: scanning the next part means "done with the previous one". The new line becomes current at once (selected in
   Fusion, not placed); the PREVIOUS line is placed on the boards that were ticked for it after a 3 s grace unless Esc / Cancel.
   A further resolved scan during the grace places the previous line at once. No grace when no line was current, the previous line
   is DNF or already done on its ticked boards, the scan is the same line, or the scan does not resolve (no call here at all). */
const AUTO_S = 3;
const autoBoards = (line, boards) => boards.filter(b => !asmOf(line).done.includes(b));
function autoEligible(line) { return !!line && !line.dnf && [...tickSet(line)].some(b => !asmOf(line).done.includes(b)); }
async function scanTakes(ln, code) {
  let prev = ui.noSel ? null : S.bom.find(l => l.line === ui.line);
  const same = !!prev && prev.line === ln.line;
  if (!same && ui.auto) { await autoFlush(); prev = ui.noSel ? null : S.bom.find(l => l.line === ui.line); }
  const grace = !same && ui.autoOn && autoEligible(prev) ? { line: prev.line, boards: [...tickSet(prev)], code: code || '' } : null;
  ui.line = ln.line; lineChanged(true);
  if (grace) autoStart(grace);
}
function autoStart(g) {
  autoStop();
  ui.auto = { ...g, t0: performance.now(), timer: setInterval(autoTick, 100) };
  const slot = $('#autoslot'); if (slot) slot.innerHTML = autoBannerHtml();
}
async function autoPlace(a) {
  const line = S.bom.find(l => l.line === a.line); if (!line) return;
  const boards = autoBoards(line, a.boards); if (line.dnf || !boards.length) return;
  const r = await cmd('place_batch', { line: a.line, boards, via: 'scan', code: a.code, auto: true }, true);
  if (!r.ok) { toast(r.data.error || 'could not record', true); return; }
  toast(`${line.designators.join(' ')} placed on ${boardsTxt(boards)} (auto-place)`); render();
}
async function autoFlush() { const a = ui.auto; autoStop(); if (a) await autoPlace(a); }
function autoLeft() { return ui.auto ? Math.max(0, AUTO_S - (performance.now() - ui.auto.t0) / 1000) : 0; }
function autoBannerHtml() {
  if (!ui.auto) return '';
  const line = S.bom.find(l => l.line === ui.auto.line), n = line ? autoBoards(line, ui.auto.boards).length : 0, left = autoLeft();
  return `<div class="autobanner" id="autobanner"><div class="autotxt">Placing <b>${esc(line ? line.designators.join(' ') : '')}</b> (previous) on <b>${n}</b> board${n === 1 ? '' : 's'} in <b id="autosec">${Math.ceil(left)}</b>&hellip; <span>Esc to cancel</span></div>
    <button data-act="autocancel" id="autocancel">Cancel</button><div class="autobar"><i id="autobar" style="width:${(left / AUTO_S * 100).toFixed(0)}%"></i></div></div>`;
}
function autoStop() { if (ui.auto) { clearInterval(ui.auto.timer); ui.auto = null; } const slot = $('#autoslot'); if (slot) slot.innerHTML = ''; }
function autoCancel(reason) {
  if (!ui.auto) return;
  const a = ui.auto; autoStop();
  cmd('auto_cancel', { line: a.line, reason, code: a.code }, true);
  ui.scanMsg = { ok: false, text: `Auto-place cancelled (${reason.replace('_', ' ')}); nothing placed.` };
  const m = $('#scanmsg'); if (m) m.innerHTML = `<span style="color:var(--fail)">${esc(ui.scanMsg.text)}</span>`;
}
function autoTick() {
  if (!ui.auto) return;
  if (document.hidden) { autoCancel('hidden'); return; }
  const left = autoLeft();
  if (left > 0) { const a = $('#autosec'), b = $('#autobar'); if (a) a.textContent = Math.ceil(left); if (b) b.style.width = (left / AUTO_S * 100).toFixed(0) + '%'; return; }
  const a = ui.auto, line = S.bom.find(l => l.line === a.line);
  if (!line || line.dnf || !autoBoards(line, a.boards).length) { autoCancel('ineligible'); return; }
  autoStop(); autoPlace(a);
}
function moveLine(d) {
  const ls = bomList(); const i = ls.findIndex(l => l.line === ui.line); const j = Math.max(0, Math.min(ls.length - 1, i + d));
  ui.line = ls[j].line; lineChanged(); render(); selectLine(ui.line);
}

/* ---- step 1: Project. One card per decision, in order, each with a done / not-done marker:
   0 project folder, 1 ECAD platform, 2 ECAD link check, 3 ECAD document, 4 BOM (with the cross-check), then the optional AI card.
   A later card is greyed until the earlier ones are done; "Continue without ECAD" skips 1-3. Nothing here talks to an instrument
   or the network: the server reads folders, the ECAD program (read-only) and the settings file. */
const designName = () => (S && S.design && S.design.name) || '';
const pj = () => (S && S.project) || { state: 'empty' };
const WS = () => (S || NP || {}).workspace || { project: {}, batch: {}, batches: [], protocol: {}, ecad: { platforms: [], requests: [] }, recent: [] };
Object.assign(ui, { pjForm: null, pjNew: {}, pjOpenF: {}, pjInsp: {}, cardOpen: {}, ecadCleared: null, ecadReqOpen: false, ecadReqMsg: '',
  ecadReqText: '', bomMatches: null, bomMsg: '', fsb: null, platBusy: false });
async function probe(search) {
  if (ui.probeBusy) return;
  ui.probeBusy = true; if (search !== undefined) ui.probeSearch = search; render();
  const gen = ui.probeGen = (ui.probeGen || 0) + 1;       // a platform switch or project change starts a new generation
  const q = ui.probeSearch ? '?search=' + encodeURIComponent(ui.probeSearch) : '';
  const r = await api('/api/project/probe' + q);
  if (gen !== ui.probeGen) return;                       // stale answer (asked of the previous platform): drop it
  if (r.ok && r.data.stale) { ui.probe = null; ui.probeBusy = false; render(); return; }   // the server saw a platform switch meanwhile: ask again
  ui.probe = r.ok ? r.data : { status: { connected: false, detail: (r.data && r.data.error) || 'server not reachable' } };
  ui.probeBusy = false; ui.probeAt = Date.now();
  if (r.ok && !ui.probeSearch) ui.probeSearch = r.data.search || '';
  render();
}
const chipList = (a, cls = '') => a.length ? `<div class="chips">${a.map(d => `<span class="dchip sm ${cls}">${esc(d)}</span>`).join('')}</div>` : '<span class="faint">none</span>';
function ccTable(cc, title) {
  const sec = (label, arr, cls, hint) => `<div class="ccrow ${arr.length ? cls : ''}"><div><b>${label}</b> <span class="mono">${arr.length}</span> <span class="muted small">${hint}</span></div>${arr.length ? chipList(arr) : ''}</div>`;
  return `<div class="cc"><div class="ccrow ok"><div><b>Matched</b> <span class="mono">${cc.matched}</span> <span class="muted small">of ${cc.bom_fitted} fitted BOM designators are in the document</span></div></div>
    ${sec('Missing in design', cc.missing_in_design, 'bad', 'in the BOM, not in the document: nothing to highlight for them')}
    ${sec('Missing in BOM', cc.missing_in_bom, 'warn', 'in the document, on no BOM line (test points, fiducials, or a forgotten part)')}
    <div class="ccrow"><div><b>DNF</b> <span class="mono">${cc.dnf.length}</span> <span class="muted small">do-not-fit in the BOM, ${cc.dnf_in_design.length} drawn in the document</span></div>${cc.dnf.length ? chipList(cc.dnf, 'dnfc') : ''}</div></div>`;
}
function setupHelp(st, url) {        // the connection help: only shown under "Help connecting" (UIR-14)
  if (ecadName() !== 'Fusion') {      // no URL or MCP text for any other program
    const n = esc(ecadName());
    return `<ol class="steps"><li>Start ${n} and open the board in the PCB editor.</li>
    <li>Press <b>Check again</b> once. Do not press it repeatedly while the same error stays: wait for a change in ${n} first.</li>
    <li>If ${n} cannot be used today, continue without it: the assembly steps then work without highlighting.</li></ol>`;
  }
  return `<ol class="steps"><li>Start Autodesk Fusion and open the board in Electronics (a board or schematic tab, not a 3D design tab).</li>
    <li>Check that Fusion's MCP integration is enabled. It is a local HTTP endpoint on <span class="mono">${esc(url)}</span>. Ports 9000 and 9229 are Fusion's debug ports, not MCP, and they do not collide with it.</li>
    <li>Press <b>Check again</b> once. Do not press it repeatedly while the same error stays: wait for a change in Fusion first.</li>
    <li>If it stays down, restart Fusion's MCP integration and press <b>Check again</b>. There is no separate reset command: reconnecting makes a fresh session, and Polybench renews a lost session by itself and retries once.</li>
    <li>If Assembly Scanner is running and also shows a Fusion error, quit and relaunch it so it starts a fresh session. To test the link without Polybench, run <span class="mono">.venv/bin/python -m app.fusion_client status</span> in the Assembly Scanner folder; if that fails too, the problem is in Fusion.</li>
    <li>If Fusion itself must be restarted, save your work in Fusion first. Polybench cannot recover a Fusion that has stopped responding.</li>
    <li>If Fusion cannot be used today, continue without it: the assembly steps then work without highlighting.</li></ol>`;
}
function ccSummary(cc) {             // standard view of the cross-check: one line, then only the problems (UIR-15)
  const prob = (label, arr, cls, hint) => arr.length ? `<div class="ccrow ${cls}"><div><b>${label}</b> <span class="mono">${arr.length}</span> <span class="muted small">${hint}</span></div>${chipList(arr)}</div>` : '';
  return `<div class="pjline" id="bomline"><b>${cc.matched} of ${cc.bom_fitted}</b> fitted parts match the board, ${cc.dnf.length} DNF</div>
    <div class="cc">${prob('Missing in design', cc.missing_in_design, 'bad', 'in the BOM, not in the document')}${prob('Missing in BOM', cc.missing_in_bom, 'warn', 'in the document, on no BOM line')}</div>`;
}

/* -- small pieces */
const fmtSize = n => n == null ? '' : n < 1024 ? n + ' B' : (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' kB';
const baseName = p => String(p || '').replace(/\/+$/, '').split('/').pop();
const tail2 = p => String(p || '').split('/').slice(-2).join('/');       // folder/file: two files with one name stay apart
const dirName = p => String(p || '').replace(/\/[^/]*$/, '') || '/';
/* A path that has to fit one line: the folder part is ellipsised in the middle, the file name stays whole; the full path is the title. */
function midPath(p, cls = 'mono') {
  p = String(p || ''); const i = p.lastIndexOf('/');
  const d = i >= 0 ? p.slice(0, i + 1) : '', n = i >= 0 ? p.slice(i + 1) : p;
  return `<span class="pth mid ${cls}" title="${esc(p)}"><span class="pthd">${esc(d)}</span><span class="pthn">${esc(n)}</span></span>`;
}
const wbrPath = p => esc(p).replace(/\//g, '/<wbr>');          // long paths break after a slash, not mid-name
const platLabel = id => ((WS().ecad.platforms || []).find(p => p.id === id) || {}).label || id || 'none';
function pathRow(label, path, sub = '') {
  if (!path) return `<dt>${esc(label)}</dt><dd class="faint">none</dd>`;
  return `<dt>${esc(label)}</dt><dd class="pjpath"><span class="pjpl">${midPath(path)}${sub ? `<span class="pjsub1">${sub}</span>` : ''}</span><span class="pjpb">
    <button class="sm" data-act="pjcopy" data-path="${esc(path)}" title="Copy the full path">Copy</button><button class="sm" data-act="pjreveal" data-path="${esc(path)}" title="Show it in Finder">Show in Finder</button></span></dd>`;
}
const PJST = { done: ['done', '&#10003; Done'], todo: ['todo', 'To do'], wait: ['wait', 'Waiting'], skip: ['skip', 'Skipped'] };
/* One card. st: done | todo | wait | skip | null (optional card, no marker). A closed card shows its one-line summary. */
function pjCard(id, no, title, st, { open = true, toggle = false, summary = '', body = '', extra = '', labels = ['Change', 'Hide'] } = {}) {
  const chip = st ? `<span class="pjst ${PJST[st][0]}">${PJST[st][1]}</span>` : '<span class="pjst opt">Optional</span>';
  return `<div class="box pjcard st-${st || 'opt'}${open ? '' : ' shut'}" id="pjc-${id}" data-card="${id}"><h3>${no != null ? `<span class="no">${no}</span>` : ''}<span class="pjtt">${title}</span>${chip}<span class="sp"></span>${extra}${toggle ? `<button class="sm" data-act="pjtoggle" data-id="${id}" data-open="${open ? 1 : 0}">${open ? labels[1] : labels[0]}</button>` : ''}</h3>${open ? `<div class="pjb">${body}</div>` : summary ? `<div class="pjsum">${summary}</div>` : ''}</div>`;
}
const cardOpen = (id, dflt) => ui.cardOpen[id] != null ? ui.cardOpen[id] : dflt;
async function copyText(t) {
  let ok = false;
  try { await navigator.clipboard.writeText(t); ok = true; } catch { /* fall back */ }
  if (!ok) {
    const ta = document.createElement('textarea'); ta.value = t; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.appendChild(ta); ta.select(); try { ok = document.execCommand('copy'); } catch { ok = false; } ta.remove();
  }
  toast(ok ? 'Copied: ' + t : 'Could not copy; select the path and copy it by hand', !ok);
}

/* -- the design a typed protocol path names (New project hint) */
const protoD = {}, protoT = {};
function protoDesignTxt(path) {
  const d = protoD[path];
  if (!d) return '';
  if (d.design) return ` (design <b>${esc(d.design)}</b>${d.ok ? '' : ', ' + esc(d.problem)})`;
  return d.pending ? '' : ` (${esc(d.problem || 'not readable')})`;
}
function protoLookup(path) {
  clearTimeout(protoT.t);
  if (!path || protoD[path]) return;
  protoT.t = setTimeout(async () => {
    protoD[path] = { pending: true };
    const r = await api('/api/project/protocol_design?path=' + encodeURIComponent(path));
    protoD[path] = r.ok ? r.data : { problem: 'not readable' };
    if ((ui.pjNew.protocol ?? '') === path) render();
  }, 300);
}

/* -- folder inspection for the New / Open forms */
const inspT = {};
function pjInspect(kind, now) {
  clearTimeout(inspT[kind]);
  inspT[kind] = setTimeout(async () => {
    const path = ((kind === 'new' ? ui.pjNew.dir : ui.pjOpenF.dir) || '').trim();
    if (!path) { ui.pjInsp[kind] = null; render(); return; }
    const r = await api('/api/project/inspect?path=' + encodeURIComponent(path));
    if (((kind === 'new' ? ui.pjNew.dir : ui.pjOpenF.dir) || '').trim() !== path) return;      // typed on meanwhile
    ui.pjInsp[kind] = r.ok ? r.data : { kind: 'error', problem: (r.data && r.data.error) || 'could not inspect the folder' };
    if (kind === 'new') ui.pjNew.confirm = false;
    if (kind === 'open' && r.ok) {
      const s = r.data.settings || {}, bs = r.data.batches || [];
      ui.pjOpenF.batch = bs.some(b => b.name === s.current_batch) ? s.current_batch : bs[0] ? bs[0].name : '';
      ui.pjOpenF.useNew = false;
    }
    render();
  }, now ? 0 : 250);
}
const HOLDS = ['project', 'legacy'];
const warnHtml = i => (i.warnings || []).length ? `<div class="callout pjwarn"><b>Check this folder.</b> ${i.warnings.map(w => esc(w.text)).join(' ')}
  <label class="pjradio"><input type="checkbox" data-pjf="pjNew.confirm" ${ui.pjNew.confirm ? 'checked' : ''}><span>Create the project here anyway</span></label></div>` : '';
function inspNew(i) {
  if (!i) return { ok: false, html: '<span class="faint">Type or browse to the folder for the new project. It may not exist yet.</span>' };
  if (HOLDS.includes(i.kind)) return { ok: false, html: `<span class="pjbad">Already holds a project.</span> Choose an empty or new folder, or use <b>Open project&hellip;</b>.` };
  if (i.kind === 'file') return { ok: false, html: '<span class="pjbad">This is a file, not a folder.</span>' };
  if (i.kind === 'missing') return i.creatable ? { ok: true, html: `<span class="pjok">Will be created.</span> <span class="muted">${esc(i.path)}</span>` } : { ok: false, html: `<span class="pjbad">Cannot be created:</span> ${esc(i.problem || 'not writable')}` };
  if (i.kind === 'error' || i.kind === 'corrupt' || i.kind === 'unreadable') return { ok: false, html: `<span class="pjbad">${esc(i.problem)}</span>` };
  if (!i.writable) return { ok: false, html: '<span class="pjbad">Not writable.</span> Choose another folder.' };
  if ((i.warnings || []).length) return { ok: !!ui.pjNew.confirm, html: (i.kind === 'empty' ? '<span class="pjok">Writable.</span>' : '<span class="pjok">Writable, but check it.</span>') + warnHtml(i) };
  if (i.kind === 'empty') return { ok: true, html: '<span class="pjok">Empty folder:</span> the project is created here.' };
  return { ok: true, html: '<span class="pjok">Usable.</span> The folder holds other files; the settings file and the batch folders are added next to them.' };
}
function inspOpen(i) {
  if (!i) return { ok: false, html: '<span class="faint">Type or browse to a project folder, or pick a recent project.</span>' };
  if (i.kind === 'project') return { ok: true, html: `<span class="pjok">Polybench project</span> <b>${esc(i.name)}</b>, ${i.batches.length} batch${i.batches.length === 1 ? '' : 'es'}.` };
  if (i.kind === 'legacy') return { ok: true, html: `<span class="pjok">Adopt this folder:</span> a settings file is written next to the batch folders; nothing is moved. ${i.batches.length} batch${i.batches.length === 1 ? '' : 'es'} found. Say below which protocol they were recorded with.` };
  if (i.kind === 'batch') return { ok: false, html: `<span class="pjbad">This is a batch folder.</span> Open its parent, the project. <button class="sm" data-act="pjparent" data-dir="${esc(i.parent)}">Use ${esc(baseName(i.parent))}</button>` };
  if (i.kind === 'missing') return { ok: false, html: '<span class="pjbad">The folder does not exist.</span>' };
  if (i.kind === 'file') return { ok: false, html: '<span class="pjbad">This is a file, not a folder.</span>' };
  if (i.kind === 'error' || i.kind === 'corrupt' || i.kind === 'unreadable') return { ok: false, html: `<span class="pjbad">${esc(i.problem)}</span>` };
  return { ok: false, html: '<span class="pjbad">Holds no Polybench project</span> (no polybench-project.json and no batch folders).' };
}

/* -- card 0: project folder */
function pjFolderCard(W) {
  const p = W.project || {}, b = W.batch || {}, ov = p.overrides || {}, saved = p.settings || {};
  const ovTxt = [];
  if (ov.ecad) ovTxt.push(`this run: ${esc(platLabel(ov.ecad))} from --ecad; saved: ${esc(platLabel((saved.ecad || {}).program))}`);
  if (ov.protocol) ovTxt.push(`this run: protocol ${esc(tail2(ov.protocol))} from --protocol; saved: ${esc(tail2(saved.protocol_path) || 'none')}`);
  if (ov.batch) ovTxt.push(`this run: batch ${esc(ov.batch)} from --batch; saved: ${esc(saved.current_batch || 'none')}`);
  let h = `<dl class="facts pjfacts"><dt>Project</dt><dd><b>${esc(p.name || '')}</b>${p.adopted ? ' <span class="badge ok" title="This folder held batch folders without a settings file. The settings file was written next to them; nothing was moved.">adopted in place</span>' : ''}${W.simulate ? ' <span class="badge sim">SIMULATED</span>' : ''}</dd>
    ${pathRow('Project folder', p.dir)}${ui.advanced ? pathRow('Batch folder', b.dir, b.name ? `<span class="muted small">batch <b>${esc(b.name)}</b>, ${b.events} events</span>` : '') + pathRow('Journal', b.journal) + pathRow('Settings file', p.settings_file) + pathRow('Protocol', (W.protocol || {}).path) : ''}</dl>`;
  if (ovTxt.length && ui.advanced) h += `<div class="callout pjov"><b>Command-line overrides for this run only.</b> ${ovTxt.join('. ')}. The settings file keeps the saved values.</div>`;
  const bs = W.batches || [];
  if (bs.length && ui.advanced) {
    h += `<div class="pjsub">Batches in this project</div><table class="across pjbt"><tr><th>Batch</th><th>Events</th><th>Last change</th><th></th></tr>${bs.slice(0, 8).map(x =>
      `<tr class="${x.name === b.name ? 'me' : ''}"><td class="mono">${esc(x.name)}</td><td class="mono">${x.events}</td><td class="mono small">${esc((x.modified || '').replace('T', ' ').slice(0, 16))}</td>
      <td style="text-align:right">${x.name === b.name ? '<span class="vd pass">CURRENT</span>' : `<button class="sm" data-act="pjbatch" data-name="${esc(x.name)}">Open this batch</button>`}</td></tr>`).join('')}</table>
      ${bs.length > 8 ? `<div class="muted small">${bs.length - 8} more: use Open project&hellip; to pick one.</div>` : ''}`;
  }
  h += `<div class="navrow" style="margin-top:10px"><button class="${ui.pjForm === 'new' ? 'primary' : ''}" data-act="pjnew">New project&hellip;</button><button class="${ui.pjForm === 'open' ? 'primary' : ''}" data-act="pjopen">Open project&hellip;</button>
    <span class="muted small">These settings stay with this project until you create or open another one.</span><span class="sp"></span>${S && S.demo ? '' : '<button class="link small" data-act="demoopen" title="Opens the packaged demo, simulated, in its own folder. Back returns here.">Try the demo project</button>'}</div>`;
  if (ui.pjForm === 'new') h += pjNewForm(W);
  if (ui.pjForm === 'open') h += pjOpenForm(W);
  return h;
}
function pjNewForm(W) {
  const f = ui.pjNew, v = inspNew(ui.pjInsp.new);
  if ((f.protocol ?? '') !== '' && f.protocol !== (W.protocol || {}).path) protoLookup(f.protocol.trim());
  return `<div class="pjform" id="pjnewform"><h4>New project</h4>
    <label for="pjn-dir">Project folder</label><div class="pjrow"><input id="pjn-dir" data-pjf="pjNew.dir" value="${esc(f.dir || '')}" placeholder="a new or empty folder" autocomplete="off"><button data-act="pjbrowse" data-target="new.dir">Browse&hellip;</button></div>
    <div class="pjinsp" id="pjn-insp">${v.html}</div>
    <label for="pjn-name">Project name</label><input id="pjn-name" data-pjf="pjNew.name" value="${esc(f.name || '')}" placeholder="for example ${esc(designName() || 'BOARD')} prototype" autocomplete="off">
    <label for="pjn-proto">Protocol file</label><div class="pjrow"><input id="pjn-proto" data-pjf="pjNew.protocol" value="${esc(f.protocol ?? ((W.protocol || {}).path || ''))}" autocomplete="off"><button data-act="pjbrowse" data-target="new.protocol">Browse&hellip;</button></div>
    <div class="pjprotohint small" id="pjn-protohint">${!S ? ((f.protocol ?? '').trim() ? `This project will use <b>${esc(tail2(f.protocol))}</b>${protoDesignTxt(f.protocol)}.` : 'Choose the .pcbv.yaml protocol file that describes your board. It is not copied: the settings file names it.')
      : (f.protocol ?? '') === '' || f.protocol === (W.protocol || {}).path ? `This project will use the protocol of the open project, <b>${esc(baseName((W.protocol || {}).path))}</b> (design <b>${esc(designName())}</b>). Change the path or Browse to use another.` : `This project will use <b>${esc(tail2(f.protocol))}</b>${protoDesignTxt(f.protocol)}, not the protocol of the open project (${esc(tail2((W.protocol || {}).path))}, design <b>${esc(designName())}</b>).`}</div>
    <div class="pjgrid2"><div><label for="pjn-batch">First batch <span class="faint">(optional)</span></label><input id="pjn-batch" data-pjf="pjNew.batch" value="${esc(f.batch || '')}" placeholder="DESIGN_DATE" autocomplete="off"></div>
      <div><label for="pjn-boards">Boards <span class="faint">(optional)</span></label><input id="pjn-boards" data-pjf="pjNew.boards" value="${esc(f.boards || '')}" inputmode="numeric" autocomplete="off"></div></div>
    <div class="muted small" style="margin-top:6px">ECAD platform for the new project: ${esc(W.ecad.label || 'Fusion')} (change it in card 1 afterwards). The settings file names the protocol and BOM files; it never copies them.</div>
    ${f.err ? `<div class="callout stop" id="pjn-err">${esc(f.err)}</div>` : ''}
    <div class="row"><button class="primary" data-act="pjnewgo" ${v.ok && (S || (f.protocol ?? '').trim()) ? '' : 'disabled'}>Create project</button><button data-act="pjformclose">Cancel</button></div></div>`;
}
function pjOpenForm(W) {
  const f = ui.pjOpenF, i = ui.pjInsp.open, v = inspOpen(i);
  const rec = (W.recent || []).filter(r => r.dir !== (W.project || {}).dir);
  let h = `<div class="pjform" id="pjopenform"><h4>Open project</h4>
    <label for="pjo-dir">Project folder</label><div class="pjrow"><input id="pjo-dir" data-pjf="pjOpenF.dir" value="${esc(f.dir || '')}" placeholder="a folder with polybench-project.json or batch folders" autocomplete="off"><button data-act="pjbrowse" data-target="open.dir">Browse&hellip;</button></div>
    <div class="pjinsp" id="pjo-insp">${v.html}</div>`;
  if (rec.length) h += `<div class="pjsub">Recent projects</div><div class="pjrec">${rec.map(r => r.missing ? `<div class="pjrecm"><div class="pjrecb missing"><b>${esc(r.name || baseName(r.dir))}</b> <span class="pjbad small">missing</span><span class="mono small muted">${wbrPath(r.dir)}</span><span class="small faint">the folder no longer exists</span></div><button class="sm" data-act="pjrecrm" data-dir="${esc(r.dir)}">Remove from list</button></div>` : `<button class="pjrecb${r.dir === f.dir ? ' on' : ''}" data-act="pjrecent" data-dir="${esc(r.dir)}" title="${esc(r.dir)}"><b>${esc(r.name || baseName(r.dir))}</b><span class="mono small muted">${wbrPath(r.dir)}</span><span class="small faint">batch ${esc(r.batch || '?')}, ${esc((r.opened || '').replace('T', ' ').slice(0, 16))}</span></button>`).join('')}</div>`;
  if (v.ok) {
    const bs = i.batches || [];
    h += `<div class="pjsub">Batch</div><div class="pjbatches">${bs.map(b => `<label class="pjradio"><input type="radio" name="pjob" data-pjf="pjOpenF.batch" value="${esc(b.name)}" ${!f.useNew && f.batch === b.name ? 'checked' : ''}><span class="mono">${esc(b.name)}</span><span class="muted small">${b.events} events, ${esc((b.modified || '').replace('T', ' ').slice(0, 16))}</span></label>`).join('')}
      <label class="pjradio"><input type="radio" name="pjob" data-pjf="pjOpenF.useNew" value="1" ${f.useNew ? 'checked' : ''}><span>New batch</span><input id="pjo-newb" data-pjf="pjOpenF.newBatch" value="${esc(f.newBatch || '')}" placeholder="batch name" autocomplete="off"></label></div>`;
  }
  const needP = i && i.kind === 'legacy', protoOk = !needP || !!(f.protocol || '').trim();
  if (needP && v.ok) h += `<div class="pjsub">Protocol for this folder <span class="pjbad small">required</span></div>
    <div class="pjrow"><input id="pjo-proto" data-pjf="pjOpenF.protocol" value="${esc(f.protocol || '')}" placeholder="the .pcbv.yaml these batches were recorded with" autocomplete="off"><button data-act="pjbrowse" data-target="open.protocol">Browse&hellip;</button></div>
    <div class="small muted" style="margin-top:4px">Adopting writes this protocol into the project settings. Polybench checks that the batches were recorded for the same design and refuses otherwise.
      ${(W.protocol || {}).path ? `<button class="sm" data-act="pjuseproto">Use the open project's protocol (${esc(baseName(W.protocol.path))})</button>` : ''}</div>`;
  if (f.err) h += `<div class="callout stop" id="pjo-err">${esc(f.err)}</div>`;
  h += `<div class="row"><button class="primary" data-act="pjopengo" ${v.ok && protoOk ? '' : 'disabled'}>${i && i.kind === 'legacy' ? 'Adopt and open' : 'Open project'}</button><button data-act="pjformclose">Cancel</button></div></div>`;
  return h;
}
async function pjSwitched(msg) {
  ui.pjForm = null; ui.pjNew = {}; ui.pjOpenF = {}; ui.pjInsp = {}; ui.cardOpen = {}; ui.ecadCleared = null; ui.ecadRestored = null; ui.pjLanded = false; ui.ecadReqMsg = ''; ui.bomMatches = null; ui.bomMsg = '';
  ui.probe = null; ui.probeBusy = false; ui.probeGen = (ui.probeGen || 0) + 1; ui.probeSearch = ''; ui.ccFocus = null; refresh.rankDone = false;
  ui.step = 'project'; ui.test = null; ui.line = null;
  PLG.reset();
  await refresh();
  ui.step = 'project'; render();
  if (msg) toast(msg);
}

/* -- server-side folder / file browser (a page cannot pick server paths; no osascript) */
const FSB_TITLE = { 'new.dir': 'Choose the project folder', 'new.protocol': 'Choose the protocol file', 'open.protocol': 'Choose the protocol file', 'open.dir': 'Choose a project folder', bom: 'Choose a BOM file', image: 'Open image: choose a .hex, .bin or .elf' };
async function fsbOpen(target, start, note) {
  const file = target === 'bom' || target === 'image' || target.endsWith('.protocol');
  ui.fsb = { target, mode: file ? 'file' : 'dir', exts: target === 'bom' ? 'yaml,yml,csv,xlsx' : target === 'image' ? 'hex,bin,elf' : target.endsWith('.protocol') ? 'yaml,yml' : '', note: note || '', data: null, sub: '', all: false };
  await fsbGo(start || '');
}
async function fsbGo(path) {
  const f = ui.fsb; if (!f) return;
  const r = await api(`/api/fs/list?mode=${f.mode}&exts=${encodeURIComponent(f.exts)}${f.target === 'bom' && !f.all ? '&bom=1' : ''}` + (path ? '&path=' + encodeURIComponent(path) : ''));
  if (!r.ok) { toast((r.data && r.data.error) || 'cannot list that folder', true); return; }
  f.data = r.data; fsbRender();
}
function fsbRender() {
  const f = ui.fsb, root = $('#fsb-root'); if (!f || !root) return;
  const d = f.data || { entries: [], roots: [] };
  const ents = [...d.entries].sort((a, b) => (b.dir - a.dir) || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }));
  const badge = k => k === 'project' ? '<span class="fsbk project">project</span>' : k === 'batch' ? '<span class="fsbk batch">batch</span>' : '';
  const list = ents.map(e => e.unreadable
    ? `<div class="fsbe ${e.dir ? 'dir' : 'file'} locked" title="${esc(e.path)}: permission denied"><span class="fsbn faint"><span class="fsbi">${e.dir ? '&#9656;' : '&#9643;'}</span>${esc(e.name)}</span><span class="fsbk lock">no access</span></div>`
    : e.dir
    ? `<div class="fsbe dir"><button class="ghost fsbn" data-act="fsbgo" data-path="${esc(e.path)}" title="${esc(e.path)}"><span class="fsbi">&#9656;</span>${esc(e.name)}</button>${badge(e.kind)}${f.mode === 'dir' ? `<button class="sm" data-act="fsbpick" data-path="${esc(e.path)}">Choose</button>` : ''}</div>`
    : `<div class="fsbe file"><button class="ghost fsbn" data-act="fsbpick" data-path="${esc(e.path)}" title="${esc(e.path)}"><span class="fsbi">&#9643;</span>${esc(e.name)}</button><span class="mono small muted">${fmtSize(e.size)}</span></div>`).join('');
  const filt = f.target === 'bom' ? `<div class="fsbfilt small muted"><label class="pjradio"><input type="checkbox" id="fsball" ${f.all ? 'checked' : ''}><span>Show all .yaml / .csv / .xlsx files</span></label>${!f.all && d.hidden_files ? `<span>${d.hidden_files} yaml file${d.hidden_files === 1 ? '' : 's'} without a <span class="mono">bom:</span> list hidden</span>` : ''}</div>` : '';
  let here = '';
  if (f.mode === 'dir' && d.here) here = `<div class="fsbhere small">${(f.target === 'new.dir' ? inspNew(d.here) : inspOpen(d.here)).html.replace(/<button[\s\S]*?<\/button>/g, '')}</div>`;
  root.innerHTML = `<div class="veil fsbveil"><div class="dlg fsb" role="dialog" aria-label="${esc(FSB_TITLE[f.target] || 'Choose')}"><h2>${esc(FSB_TITLE[f.target] || 'Choose')}</h2>
    ${f.note ? `<div class="callout" style="margin:0 0 8px">${esc(f.note)}</div>` : ''}
    <div class="fsbroots">${(d.roots || []).map(r => `<button class="sm" data-act="fsbgo" data-path="${esc(r.path)}" title="${esc(r.path)}">${esc(r.label)}</button>`).join('')}</div>
    <div class="fsbbar"><button class="sm" data-act="fsbgo" data-path="${esc(d.parent || '')}" ${d.parent ? '' : 'disabled'} title="Parent folder">&uarr; Up</button><input id="fsbpath" value="${esc(d.path || '')}" autocomplete="off" spellcheck="false"><button class="sm" data-act="fsbgo2">Go</button></div>
    ${filt}
    <div class="fsblist">${list || `<div class="faint" style="padding:10px">${f.mode === 'file' ? 'No ' + esc(f.exts.split(',').map(x => '.' + x).join(', ')) + ' files or sub-folders here.' : 'No sub-folders here.'}</div>`}${d.truncated ? '<div class="faint small">Only the first 500 entries are listed.</div>' : ''}${d.error ? `<div class="pjbad small">${esc(d.error)}</div>` : ''}</div>
    ${here}
    ${f.mode === 'dir' && f.target === 'new.dir' ? `<div class="fsbsub"><label for="fsbsub" class="muted small">New sub-folder (optional)</label><input id="fsbsub" value="${esc(f.sub)}" placeholder="name of a folder to create here" autocomplete="off"></div>` : ''}
    <div class="btns"><button type="button" data-act="fsbclose">Cancel</button>${f.mode === 'dir' ? '<button type="button" class="primary" data-act="fsbuse">Use this folder</button>' : ''}</div></div></div>`;
  const fa = $('#fsball'); fa && fa.addEventListener('change', () => { f.all = fa.checked; fsbGo(d.path); });
  const pi = $('#fsbpath'); pi && pi.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); fsbGo(pi.value.trim()); } });
  root.onkeydown = e => { if (e.key === 'Escape') fsbClose(); };
}
function fsbClose() { ui.fsb = null; const r = $('#fsb-root'); if (r) r.innerHTML = ''; }
function fsbChoose(path) {
  const t = ui.fsb && ui.fsb.target; fsbClose();
  if (t === 'new.dir') { ui.pjNew.dir = path; pjInspect('new', true); }
  else if (t === 'new.protocol') ui.pjNew.protocol = path;
  else if (t === 'open.protocol') ui.pjOpenF.protocol = path;
  else if (t === 'open.dir') { ui.pjOpenF.dir = path; pjInspect('open', true); }
  else if (t === 'bom') { bomLoad(path); return; }
  else if (t === 'image') { AUTO.imagePicked(path); return; }
  render();
}

/* -- card 2 BOM: load / replace / remove a file; a drop gives a name and size, the server finds the path */
async function bomLoad(path, confirm) {
  const r = await api('/api/bom/load', { path, confirm: !!confirm });
  if (r.status === 409 && r.data.needs_confirm) {
    const n = r.data.assembly_events;
    const v = await dialog({ title: 'Replace the BOM', text: `${n} assembly event${n === 1 ? ' is' : 's are'} recorded against the current BOM. They stay in the journal. Replace anyway?`, ok: 'Replace anyway' });
    if (v) return bomLoad(path, true);
    return;
  }
  if (!r.ok) { ui.bomMsg = (r.data && r.data.error) || 'could not load that file'; toast(ui.bomMsg, true); render(); return; }
  ui.bomMsg = ''; ui.bomMatches = null; ui.probe = null;
  toast('BOM loaded: ' + baseName(path));
  await refresh();
}
async function bomRemove(confirm) {
  const r = await api('/api/bom/remove', { confirm: !!confirm });
  if (r.status === 409 && r.data.needs_confirm) {
    const n = r.data.assembly_events;
    const v = await dialog({ title: 'Remove the BOM file', text: `${n} assembly event${n === 1 ? ' is' : 's are'} recorded against the current BOM. They stay in the journal. Remove anyway?`, ok: 'Remove anyway', danger: true });
    if (v) return bomRemove(true);
    return;
  }
  if (!r.ok) { toast((r.data && r.data.error) || 'could not remove the BOM file', true); return; }
  ui.bomMsg = ''; ui.probe = null; toast('BOM file removed from the project');
  await refresh();
}
async function bomDropped(file) {
  if (!file) return;
  const r = await api(`/api/bom/locate?name=${encodeURIComponent(file.name)}&size=${file.size}`);
  const m = (r.ok && r.data.matches) || [];
  if (m.length === 1) return bomLoad(m[0]);
  if (m.length > 1) { ui.bomMatches = { name: file.name, paths: m }; render(); return; }
  const start = (S.project_bom && S.project_bom.file) ? dirName(S.project_bom.file) : dirName((WS().protocol || {}).path || '');
  fsbOpen('bom', start, `${file.name} was not found in the project, protocol, Downloads, Desktop or Documents folders. A browser drop does not give the file's location: find it here.`);
}
function bomFacts(b) {
  if (!b || !b.file) return '<div class="muted">No BOM loaded.</div>';
  return `<dl class="facts" style="margin:0"><dt>File</dt><dd class="pjpath"><b>${esc(b.name)}</b> <span class="muted">${midPath(b.file, 'mono small')}</span></dd>
    <dt>Source</dt><dd>${b.source === 'settings' ? 'chosen for this project <span class="muted small">(named in the project settings file)</span>' : 'from the protocol <span class="muted small">(its bom: list)</span>'}</dd>
    <dt>Size</dt><dd class="mono">${fmtSize(b.size_bytes)}${b.format ? ', ' + esc(b.format) : ''}${b.file_lines ? `, ${b.file_lines} lines in the file` : ''}${b.first_line ? `, BOM from line ${b.first_line}` : ''}</dd>
    <dt>Contents</dt><dd class="mono">${b.lines} BOM lines, ${b.designators} designators</dd>
    <dt>Fitted / DNF</dt><dd class="mono">${b.fitted} fitted, ${b.dnf} do-not-fit</dd></dl>`;
}
function bomBody(W, b) {
  let h = '';
  if (b.error) h += `<div class="callout stop">${esc(b.error)}</div>`;
  else if (W.bom_error) h += `<div class="callout stop">${esc(W.bom_error)}</div>`;
  if (ui.bomMsg) h += `<div class="callout stop">${esc(ui.bomMsg)}</div>`;
  if (!b.source || b.source === 'none') {
    h += `<div class="bomdrop" id="bomdrop"><button class="bomplus" data-act="bomadd" title="Choose a BOM file" aria-label="Choose a BOM file">+</button>
      <div><b>Drop a BOM file here or press + to choose one</b><br><span class="muted small">.yaml with a bom: list, .csv or .xlsx purchasing BOM. The project settings file names it; it is never copied.</span></div></div>`;
  } else if (ui.advanced) {
    h += bomFacts(b);
    h += `<div class="navrow"><button data-act="bomreplace">Replace&hellip;</button>${b.removable ? '<button data-act="bomremove">Remove</button>' : '<button disabled title="This BOM is part of the protocol file. Replace it with another file, or edit the protocol.">Remove</button><span class="muted small">part of the protocol file</span>'}
      <span class="sp"></span><span class="bomdrop mini" id="bomdrop">or drop a file here</span></div>`;
  }
  if (ui.bomMatches) h += `<div class="callout" style="margin-top:8px"><b>${ui.bomMatches.paths.length} files named ${esc(ui.bomMatches.name)} with that size.</b> Which one?
    ${ui.bomMatches.paths.map(p => `<div class="row"><button class="sm" data-act="bompick" data-path="${esc(p)}">Use</button><span class="mono small pth">${esc(p)}</span></div>`).join('')}
    <div class="row"><button class="sm" data-act="bommatchx">Cancel</button></div></div>`;
  return h;
}

/* -- the page */
function viewProject(el) {
  const P = pj(), W = WS(), sel = P.select, cc = P.crosscheck, acc = P.accepted, sk = P.ecad_skipped, E = W.ecad || {};
  const b = S.project_bom || {};
  if (sel) ui.ecadCleared = null;
  if (ui.ecadRestored && (!sel || ui.ecadRestored.to !== E.program)) ui.ecadRestored = null;
  const c1 = !!E.saved;
  if (c1 && !ui.probe && !ui.probeBusy && !sk) probe();
  const pr = ui.probe, st = (pr && pr.status) || {}, busy = ui.probeBusy, linked = !!st.connected;
  const stale = E.stale_selection, c3 = !!sel && !stale;
  const bomOk = b.source && b.source !== 'none' && !b.error, c4 = bomOk && (!!sk || !!acc), c4on = c3 || !!sk;
  const focus = !c1 ? 1 : sk ? 4 : !linked ? 2 : !c3 ? 3 : 4;
  const old = $('#pjsearch'), keepSearch = old ? old.value : (ui.probeSearch || '');
  const ae = document.activeElement, fid = ae && el.contains(ae) && ae.id ? ae.id : null;
  const sel0 = fid && ae.selectionStart != null ? [ae.selectionStart, ae.selectionEnd] : null;
  let top = el.scrollTop;
  const landing = !ui.pjLanded; ui.pjLanded = true;       // first draw of the page: start at the top, card 0 in view (F1)
  if (landing) top = 0;

  let h = `<div class="pane pjpane"><div class="crumb"><span>Project</span></div><h1 class="tt">Project</h1>
    <p class="muted" style="margin:0 0 10px">Polybench only reads from the ECAD program: it never opens, switches, saves or changes a document. Highlighting in the assembly steps needs this link.</p>`;
  // 0 project folder: always shown
  h += pjCard('folder', 0, 'Project folder', 'done', { body: pjFolderCard(W) });

  if (sk) h += `<div class="callout" id="pjskipnote"><b>Continuing without ECAD.</b> ${esc(sk.reason)} <span class="muted">(${esc(sk.ts.slice(0, 16).replace('T', ' '))}${sk.operator ? ', ' + esc(sk.operator) : ''})</span><br>The ECAD setup is skipped and highlighting is disabled in the assembly and test steps.
      <div class="row"><button data-act="pjunskip">Link ECAD again</button></div></div>`;

  // 1 ECAD setup: ONE box with three parts in order, platform dropdown, link status, document (UIR-14)
  if (sk) h += pjCard('ecad', 1, 'ECAD setup', 'skip', { open: false, summary: `Skipped: ${esc(sk.reason)}` });
  else {
    const plats = E.platforms || [];
    const opts = (!c1 ? '<option value="" selected disabled>Choose the ECAD program&hellip;</option>' : '')
      + plats.map(p => `<option value="${esc(p.id)}"${c1 && p.id === E.program ? ' selected' : ''}>${esc(p.label)}</option>`).join('');
    // part 1: platform
    let platB = `<div class="pjsec" id="pjc-plat"><label class="pjsub" for="pjplatsel">ECAD program</label><div class="pjrow"><select id="pjplatsel" ${ui.platBusy ? 'disabled' : ''}>${opts}</select>
      <button class="sm" data-act="pjreqopen" aria-expanded="${!!ui.ecadReqOpen}">Request another&hellip;</button></div>`;
    platB += !c1 ? `<div class="muted small" style="margin-top:6px">Choose the ECAD program this project uses. It is saved in the project settings file.</div>`
      : `<div class="muted small" style="margin-top:6px">${esc(platLabel(E.saved))} is saved in the project settings file.${E.program !== E.saved ? ` This run uses ${esc(platLabel(E.program))}.` : ''}</div>`;
    if (ui.ecadReqOpen) platB += `<div class="pjreq"><label for="pjreq" class="muted small">Which ECAD program would you like Polybench to support?</label><div class="pjrow"><input id="pjreq" value="${esc(ui.ecadReqText)}" placeholder="for example Altium Designer" maxlength="200" autocomplete="off"><button class="primary" data-act="pjreqsave">Save</button></div>
      <div class="faint small">Saved to a local file on this Mac; nothing is sent.</div></div>`;
    if (ui.ecadReqMsg) platB += `<div class="pjokline small">${ui.ecadReqMsg}</div>`;
    if ((E.requests || []).length) platB += `<div class="small muted" style="margin-top:6px">Requested before: ${E.requests.slice().reverse().map(r => `<b>${esc(r.text)}</b> <span class="faint">(${esc((r.ts || '').slice(0, 10))})</span>`).join(', ')}</div>`;
    platB += `</div>`;
    // part 2: link check, only once a platform is chosen
    let linkB = '';
    if (c1) {
      const chk = `<button class="sm" data-act="pjcheck" ${busy ? 'disabled' : ''}>${busy ? 'Checking...' : 'Check again'}</button>`;
      const hopen = !!ui.pjHelp;
      linkB = `<div class="pjsec" id="pjc-link"><div class="pjsub">${esc(ecadName())} link</div>`;
      if (!pr) linkB += `<div class="pjrow"><span class="muted">Checking ${esc(ecadName())}...</span><span class="sp"></span>${chk}</div>`;
      else if (!linked) linkB += `<div class="pjrow"><span class="vd fail">NOT REACHABLE</span><span class="small">Start ${esc(ecadName())} and open the board, then press Check again once.${st.detail ? ` <span class="mono muted">${esc(st.detail)}</span>` : ''}</span><span class="sp"></span>${chk}</div>`;
      else {
        const edTxt = st.editor === 'board' ? 'board editor' : st.editor === 'schematic' ? 'schematic editor' : 'no Electronics editor active';
        linkB += `<div class="pjrow"><span class="vd pass">CONNECTED</span><span>${esc(st.program || '')}${st.version ? ` <span class="muted">(${esc(st.version)}${ecadName() === 'Fusion' ? '; the MCP does not report Fusion\'s own version number' : ''})</span>` : ''}${st.simulated ? ' <span class="badge sim">SIMULATED</span>' : ''}</span><span class="sp"></span>${chk}</div>
          <dl class="facts" style="margin:6px 0 0"><dt>Active document</dt><dd><b>${esc(st.document || 'none')}</b> <span class="muted">${edTxt}${st.elements != null ? ', ' + st.elements + ' elements' : ''}${st.modified ? ', unsaved changes in ' + ecadName() : ''}</span></dd></dl>
          ${st.editor === 'none' || !st.document ? `<div class="callout" style="margin-top:8px">The active tab is not a board or schematic. ${ecadName() === 'Fusion' ? "In Fusion, click the board's Electronics tab, then" : 'In ' + esc(ecadName()) + ', open the board, then'} <b>Check again</b>.</div>` : ''}
          ${st.editor === 'schematic' ? `<div class="muted small" style="margin-top:6px">The schematic is active. Highlights work from either; the board tab is the one to keep open while assembling.</div>` : ''}`;
      }
      if (pr) linkB += `<div class="pjhelp"><button class="ghost onb-h" data-act="pjhelp" aria-expanded="${hopen}"><span class="onb-car">${hopen ? '&#9662;' : '&#9656;'}</span> Help connecting</button>${hopen ? setupHelp(st, pr.url || 'http://127.0.0.1:27182/mcp') : ''}</div>`;
      linkB += `</div>`;
    }
    // part 3: document
    let docB = '';
    if (c1) {
      let warn = '';
      if (ui.ecadCleared && !sel) warn += `<div class="callout pjcleared"><b>Switched to ${esc(platLabel(ui.ecadCleared.to))}.</b> The document selected in ${esc(platLabel(ui.ecadCleared.program))} (${esc(ui.ecadCleared.document || 'unnamed')}) was cleared: confirm the ${esc(platLabel(ui.ecadCleared.to))} document again under Document below.</div>`;
      if (ui.ecadRestored) warn += `<div class="callout pjrestored"><b>Switched to ${esc(platLabel(ui.ecadRestored.to))}.</b> The document confirmed earlier in ${esc(platLabel(ui.ecadRestored.to))} (${esc(ui.ecadRestored.document)}) is still open and unchanged: it was restored and the BOM was compared with it again${ui.ecadRestored.accepted ? '; the cross-check you accepted earlier still holds' : '; review the cross-check in the BOM card'}.</div>`;
      if (stale) warn += `<div class="callout pjstale"><b>The recorded document ${esc(stale.document || '')} was selected with ${esc(platLabel(stale.program))}.</b> This run uses ${esc(platLabel(E.program))}, so highlights will not use it: confirm the document again below.</div>`;
      docB = `<div class="pjsec" id="pjc-doc">`;
      if (!linked) docB += `<div class="pjsub">Document</div>${warn}` + (c3 ? `<div class="pjsum2"><b>${esc(sel.document)}</b> <span class="muted">${sel.elements} parts, fingerprint ${esc(sel.fingerprint)}; the link must be connected to change it</span></div>` : `<div class="muted small">Needs a connected ${esc(ecadName())} link.</div>`);
      else {
        const dOpen = cardOpen('doc', !c3 || !!ui.ecadRestored);
        const hdr = `<div class="pjrow"><div class="pjsub" style="margin:0">Document</div><span class="sp"></span>${c3 ? `<button class="sm" data-act="pjtoggle" data-id="doc" data-open="${dOpen ? 1 : 0}">${dOpen ? 'Hide' : 'Change'}</button>` : ''}</div>`;
        if (!dOpen) docB += hdr + warn + `<div class="pjsum2"><b>${esc(sel.document)}</b> <span class="muted">${sel.elements} parts, fingerprint ${esc(sel.fingerprint)}</span></div>`;
        else {
          const docs = pr.documents;
          let body = hdr + warn + `<div class="muted small" style="margin:6px 0">Documents with the same title are told apart by their contents: part count and a short fingerprint of the part list. Make the right one the active tab in ${esc(ecadName())}, press Check again, and use it here.</div>`;
      const fpNow = pr.fingerprint, actId = st.document_id;
      const tail = id => id ? '…' + String(id).slice(-6) : '';
      const openDocs = (docs && docs.open) || [], cands = (docs && docs.candidates) || [];
      const openIds = new Set(openDocs.map(d => d.id).filter(Boolean));
      const rows = openDocs.map(d => {
        const isAct = d.active, same = isAct && sel && !stale && fpNow === sel.fingerprint && d.name === sel.document && (!sel.document_id || sel.document_id === d.id);
        const can = isAct && st.editor !== 'none' && pr.crosscheck;
        const contents = isAct && pr.design_total != null ? pr.design_total + ' elements, ' + esc(pr.fingerprint) : '<span class="faint">not readable until active</span>';
        return `<tr class="${isAct ? 'me' : ''}"><td><b>${esc(d.name)}</b>${d.modified ? ' <span class="muted">(modified)</span>' : ''}<br><span class="faint mono small">${tail(d.id)}</span></td>
          <td class="pjstate">${isAct ? '<span class="vd pass">ACTIVE</span>' : '<span class="muted">open</span>'}</td><td class="mono">${contents}</td>
          <td class="pjact">${same ? '<span class="vd pass">SELECTED</span>' : can ? `<button class="primary sm" data-act="pjuse" data-fp="${esc(pr.fingerprint)}">Use this document</button>` : isAct ? '<span class="muted small">no parts to read</span>' : `<span class="muted small">make it the active tab in ${esc(ecadName())}</span>`}</td></tr>`;
      }).join('');
      body += `<div class="pjtw"><table class="across pjt"><tr><th>Open in ${esc(ecadName())}</th><th class="pjstate">State</th><th>Contents</th><th></th></tr>${rows || `<tr><td colspan="4" class="muted">No document is open in ${esc(ecadName())}.</td></tr>`}</table></div>`;
      const sameTitle = openDocs.filter(d => d.name === st.document).length;
      if (sameTitle > 1) body += `<div class="callout" style="margin:8px 0"><b>${sameTitle} open documents have the title ${esc(st.document)}.</b> The title does not tell them apart; the id tail and the fingerprint do. Polybench only reads the active one. To compare the others, activate each in ${esc(ecadName())} yourself and press <b>Check again</b>: it never opens or switches a document.</div>`;
      if (pr.design_total != null && pr.expected_elements) {
        const bs = pr.bom_source || {}, ok = pr.design_total === pr.expected_elements;
        body += `<div class="small ${ok ? 'muted' : ''}" style="margin:6px 0">Expected from the BOM: <b>${pr.expected_elements}</b> elements (${bs.fitted != null ? bs.fitted + ' fitted parts and ' + bs.dnf + ' do-not-fit: pads, test points and DNF parts' : ''}). ${ok ? 'The active document has exactly that.' : `<b style="color:var(--review)">The active document has ${pr.design_total}.</b>`}</div>`;
      }
      if (!(docs && docs.message && /no document hub/i.test(docs.message))) {
        body += `<div class="pjrow" style="margin:10px 0 4px"><label for="pjsearch" class="muted small">Other documents in the ${esc(ecadName())} hub</label><input id="pjsearch" value="${esc(keepSearch)}" placeholder="document title"><button class="sm" data-act="pjsearch">Find</button></div>`;
        if (docs && !docs.ok) body += `<div class="muted">${esc(docs.message || 'could not list documents')}</div>`;
        else if (cands.length) body += `<div class="pjtw"><table class="across pjt pjhub"><tr><th>Title</th><th>Location</th><th>Id</th><th></th></tr>${cands.map(c => `<tr><td>${esc(c.name)}</td><td>${esc(c.project || '')} / ${esc(c.folder || '')}</td><td class="mono">${tail(c.id)}</td><td class="muted small">${c.open || openIds.has(c.id) ? (c.id === actId ? 'active' : 'open') : 'not open'}</td></tr>`).join('')}</table></div>
          <div class="muted small">${cands.length} hub document${cands.length === 1 ? '' : 's'} match. The MCP gives no element count for a document that is not the active one. Open it in ${esc(ecadName())} yourself if you need to compare; Polybench will not.</div>`;
        else if (pr.search) body += `<div class="muted small">No hub document is titled like '${esc(pr.search)}'.</div>`;
      } else body += `<div class="muted small" style="margin-top:6px">${esc(docs.message)}.</div>`;
          docB += body;
        }
      }
      docB += `</div>`;
    }
    h += pjCard('ecad', 1, 'ECAD setup', c1 && linked && c3 ? 'done' : 'todo', { body: platB + linkB + docB });
  }

  if (!sk) h += `<div class="navrow pjskiprow"><span class="muted small">ECAD cannot be used for this batch?</span><button class="sm" data-act="pjskip">Continue without ECAD&hellip;</button></div>`;
  // 2 BOM (+ cross-check with the selected document). Standard view: one line, the problems, Accept. Advanced adds the file, every row and the DNF chips (UIR-15)
  {
    const adv = !!ui.advanced;
    let body = '';
    if (!c4on) body += `<div class="pjgatenote">Waiting for the ECAD document, or for <b>Continue without ECAD</b>.</div>`;
    let inner = bomBody(W, b);
    const live = pr && pr.crosscheck, showCc = sel && !stale && cc ? cc : null;
    if (adv) inner += `<div class="pjsub">Cross-check with the selected document${sel && !stale && !sk ? ` <span class="muted small">${esc(sel.document)}, ${sel.elements} parts, fingerprint ${esc(sel.fingerprint)}</span>` : ''}</div>`;
    if (sk) inner += bomOk && !adv ? `<div class="pjline" id="bomline"><b>${esc(b.name)}</b> <span class="muted">${b.lines} lines, ${b.designators} designators. Continuing without ECAD: no cross-check.</span></div>` : `<div class="muted">Continuing without ECAD: there is no document to compare the BOM with.</div>`;
    else if (!bomOk) { if (adv) inner += `<div class="muted">Load a BOM first.</div>`; }
    else if (showCc) {
      inner += adv ? ccTable(showCc) : ccSummary(showCc);
      if (live && pr.fingerprint && pr.fingerprint !== sel.fingerprint) inner += `<div class="callout" style="margin-top:8px"><b>The active ${esc(ecadName())} document is not the selected one</b> (fingerprint ${esc(pr.fingerprint)}, selected ${esc(sel.fingerprint)}). Highlights go to whatever is active. Switch back, or select this document instead.</div>`;
      if (acc) inner += `<div class="callout" style="margin-top:8px;border-color:var(--pass);background:var(--pass-bg)"><b>Reviewed.</b> ${acc.mismatches ? acc.mismatches + ' mismatch(es) noted: ' + esc(acc.note) : 'no mismatches.'} <span class="muted">(${esc(acc.ts.slice(0, 16).replace('T', ' '))}${acc.operator ? ', ' + esc(acc.operator) : ''})</span></div>`;
      else inner += `<div style="margin-top:10px"><label class="muted small" for="ccnote">${showCc.mismatches ? 'Say why the ' + showCc.mismatches + ' mismatch(es) are acceptable' : 'Optional note'}</label>
        <input id="ccnote" style="width:100%;margin:3px 0 8px" placeholder="${showCc.mismatches ? 'required, kept in the journal and the report' : ''}"><button class="primary" data-act="pjaccept">Accept the cross-check${showCc.mismatches ? ' with these mismatches noted' : ''}</button></div>`;
    } else inner += adv ? `<div class="muted">Confirm the ECAD document (ECAD setup) to compare its part list with the BOM.</div>`
      : `<div class="pjline" id="bomline"><b>${esc(b.name)}</b> <span class="muted">${b.lines} lines, ${b.designators} designators. Confirm the ECAD document to compare it with the BOM.</span></div>`;
    body += c4on ? inner : `<fieldset class="pjgate" disabled>${inner}</fieldset>`;
    const open = cardOpen('bom', !c4 || focus === 4 || !!ui.bomMsg);
    const sum = bomOk ? `<b>${esc(b.name)}</b> <span class="muted">${b.lines} lines, ${b.designators} designators; ${sk ? 'no cross-check (without ECAD)' : acc ? 'cross-check reviewed' : 'cross-check not reviewed'}</span>` : 'No BOM loaded.';
    h += pjCard('bom', 2, 'BOM', c4 ? 'done' : c4on ? 'todo' : 'wait', { open, toggle: c4, body, summary: sum });
  }

  // AI assistant (optional; content owned by another team: renderAiCard(S) -> html, ai_card.js)
  if (typeof renderAiCard === 'function') { try { h += renderAiCard(S); } catch (e) { h += pjCard('ai', null, 'AI assistant', null, { body: '<span class="faint">The assistant card could not be shown.</span>' }); } }

  h += howSection() + projectDone(P) + `</div>`;
  el.innerHTML = h;
  el.scrollTop = top;
  if (fid) { const n = document.getElementById(fid); if (n) { n.focus({ preventScroll: true }); if (sel0 && n.setSelectionRange) { try { n.setSelectionRange(sel0[0], sel0[1]); } catch { /* not a text box */ } } } }
  if (sel && !acc && !stale && c4on && ui.ccFocus !== sel.ts && !document.querySelector('.veil') && !fid) { ui.ccFocus = sel.ts; const n = $('#ccnote'); if (n && !landing) n.focus({ preventScroll: true }); }
}
/* "How Polybench works": the onboarding timeline, collapsed by default once a project is open (copy: onboarding.js). */
function howSection() {
  const open = !!ui.onbOpen;
  return `<div class="box onb-box" id="onbhow"><h3><button class="ghost onb-h" data-act="onbtoggle" aria-expanded="${open}"><span class="onb-car">${open ? '&#9662;' : '&#9656;'}</span> How Polybench works</button></h3>${open ? `<div class="onb-body">${ONB.timeline(esc)}</div>` : ''}</div>`;
}
function projectDone(P) {
  if (P.state !== 'done') return `<div class="muted small" style="margin-top:10px">Step 1 is complete when the ECAD document is confirmed and the BOM cross-check is reviewed (or you continue without ECAD with a reason).</div>`;
  const next = S.steps.find(s => s.kind === 'setup') || S.steps[1];
  return `<div class="callout" style="border-color:var(--pass);background:var(--pass-bg);margin-top:12px"><b>Project step complete.</b>
    <div class="row"><button class="primary go" data-act="step" data-id="${esc(next.id)}">Continue to ${esc(next.title)}</button></div></div>`;
}
/* The ECAD setup box is done when platform, link and document are all done, or ECAD was skipped with a reason (UIR-14); the checklist uses the same rule. */
function ecadSetupDone() {
  const P = pj(), E = WS().ecad || {}, st = (ui.probe && ui.probe.status) || {};
  return !!P.ecad_skipped || (!!E.saved && !!st.connected && !!P.select && !E.stale_selection);
}
function ecadSetupSub() {
  const P = pj(), E = WS().ecad || {}, st = (ui.probe && ui.probe.status) || {};
  if (P.ecad_skipped) return 'skipped, with reason';
  if (!E.saved) return 'choose the program';
  if (!st.connected) return platLabel(E.program) + ', link not checked or not reachable';
  if (!P.select || E.stale_selection) return platLabel(E.program) + ', document not selected';
  return `${platLabel(E.program)}, ${P.select.document}, ${P.select.elements} parts`;
}
function leftProject(el, hdr) {
  const P = pj(), W = WS(), sel = P.select, acc = P.accepted, sk = P.ecad_skipped, b = S.project_bom || {};
  const item = (ok, t, sub) => `<div class="trow"><span class="id ck ${ok ? 'ck-done' : 'ck-open'}">${ok ? '&#10003;' : '&#9675;'}</span><span class="ti" title="${esc(sub)}">${esc(t)}<br><span class="faint small">${esc(sub)}</span></span></div>`;
  const n = 1 + (!!(W.ecad || {}).saved || !!sk) + (!!sel || !!sk) + (!!b.source && b.source !== 'none') + (!!acc || !!sk);
  const stp = (S.steps || []).find(x => x.kind === 'project') || {};       // the same count as the timeline node (one source of truth)
  el.innerHTML = hdr('Project', stp.applicable ? `${stp.settled}/${stp.applicable}` : `${n}/5`) +
    item(true, 'Project folder', `${(W.project || {}).name || ''}, batch ${(W.batch || {}).name || ''}`) +
    item(ecadSetupDone(), 'ECAD setup', ecadSetupSub()) +
    item(!!b.source && b.source !== 'none', 'BOM loaded', b.name ? `${b.name}, ${b.lines} lines` : 'not yet') +
    item(!!acc || !!sk, 'BOM cross-check reviewed', sk ? 'not applicable' : acc ? (acc.mismatches ? acc.mismatches + ' mismatch(es) noted' : 'no mismatches') : 'not yet') +
    `<div class="sec-h">History</div>` + ((P.history || []).length ? P.history.slice().reverse().map(x => `<div class="trow"><span class="id mono" style="min-width:42px">${esc(x.ts.slice(11, 16))}</span><span class="ti" style="white-space:normal">${esc(x.what)}</span></div>`).join('') : '<div class="trow"><span class="id"></span><span class="ti faint" style="white-space:normal">Nothing recorded yet</span></div>');
}
/* Step-1 form fields keep their values in ui (the page is redrawn on every state event). */
function pjField(e) {
  const f = e.target.dataset && e.target.dataset.pjf; if (!f) return false;
  const [o, k] = f.split('.');
  if (ui[o]) ui[o].err = '';
  if (e.target.type === 'radio') { if (k === 'useNew') ui[o].useNew = true; else { ui[o][k] = e.target.value; ui[o].useNew = false; } }
  else { ui[o][k] = e.target.value; if (k === 'newBatch' && e.target.value) { ui[o].useNew = true; const r = document.querySelector('input[name=pjob][value="1"]'); if (r) r.checked = true; } }
  if (f === 'pjNew.dir') pjInspect('new');
  if (f === 'pjNew.confirm') { ui.pjNew.confirm = e.target.checked; render(); }
  if (f === 'pjNew.protocol' || f === 'pjOpenF.protocol') render();      // the hint and the button follow the field (focus is kept by viewProject)
  if (f === 'pjOpenF.dir') pjInspect('open');
  if (e.target.type === 'radio') render();
  return true;
}

/* ---- setup and report */
function viewSetup(el) {
  const m = S.meta, issues = S.protocol_issues || [];
  // a redraw replaces the inputs; keep the focused one (and what is typed in it) so serial entry is never interrupted
  const ae = document.activeElement;
  const keep = ae && el.contains(ae) && ae.tagName === 'INPUT' ? { id: ae.id, b: ae.dataset.b, value: ae.value, pos: ae.selectionStart } : null;
  el.innerHTML = `<div class="pane"><div class="crumb"><span>Batch setup</span></div><h1 class="tt">Batch ${esc(m.batch)}</h1>
    <div class="box"><h3>Batch</h3><div class="setup" style="padding:10px">
      <label>Operator</label><input id="op" value="${esc(S.operator)}" placeholder="your name, recorded with every value">
      <label>Boards</label><div><input id="nb" type="number" min="1" max="40" value="${S.boards.length}" style="width:70px"> <span class="muted">can change only before the first result</span></div>
      <label>Design</label><div>${esc(designLabel(S.design))}, plan revision ${esc(S.design.plan_revision || '')}</div>
      <label>Data</label><div>${S.origin === 'simulated' ? '<span class="badge sim">SIMULATED</span>' : 'real boards'} &middot; <span class="mono">${esc(m.dir)}</span></div></div></div>
    <div class="box"><h3>Board serials (scan or type, Enter for the next)</h3><div class="sgrid" style="padding:10px">${S.boards.map(b => `<span class="b">${b.label}</span><input class="serial" data-b="${b.label}" value="${esc(b.serial)}" placeholder="serial or batch label" autocomplete="off">`).join('')}</div></div>
    ${issues.length ? `<div class="box"><h3>Protocol warnings</h3><ul class="lintlist">${issues.map(i => `<li>${esc(i.file.split('/').pop())}:${i.line} ${esc(i.severity)}: ${esc(i.msg)}</li>`).join('')}</ul></div>` : ''}
    ${m.protocol_changed ? `<div class="callout">The protocol file changed since this batch was started. New results use the current file.</div>` : ''}
    <div class="navrow"><span class="sp"></span><button class="primary go" data-act="step" data-id="${(S.steps.find(s => s.kind === 'assembly') || S.steps.find(s => s.kind !== 'setup')).id}">Continue to ${esc((S.steps.find(s => s.kind === 'assembly') || S.steps.find(s => s.kind !== 'setup')).title)}</button></div></div>`;
  if (document.querySelector('.veil')) return;
  if (keep) {
    const again = keep.id ? $('#' + keep.id) : [...document.querySelectorAll('.serial')].find(x => x.dataset.b === keep.b);
    if (again) { again.value = keep.value; again.focus(); try { again.setSelectionRange(keep.pos, keep.pos); } catch { /* number input */ } }
  } else if (ui.setupFocused !== S.meta.batch) {   // first visit: land on the first empty serial
    ui.setupFocused = S.meta.batch;
    const first = [...document.querySelectorAll('.serial')].find(x => !x.value) || $('.serial');
    if (first && document.activeElement === document.body) first.focus();
  }
}
async function viewReport(el) {
  el.innerHTML = `<div class="pane"><div class="crumb"><span>Report</span></div><h1 class="tt">Report</h1>
    ${NOTES.reportBar()}
    <div class="navrow" style="margin:0 0 10px"><button data-act="repreview">Refresh preview</button><button class="primary" data-act="repgen">Generate report files</button><span id="repmsg" class="muted">${esc(ui.reportMsg)}</span></div>
    <iframe class="report-frame" id="repframe" sandbox></iframe></div>`;
  const r = await api('/api/report/preview'); if (r.ok && $('#repframe')) $('#repframe').srcdoc = r.data.html;
}

/* ---- welcome page: no project is open (first run, or the demo was closed and there was no project before it) */
function renderWelcome() {
  document.body.classList.add('welcome');
  document.documentElement.dataset.theme = ui.theme;
  const W = WS(), rec = (W.recent || []);
  $('#appbar').innerHTML = `<span class="brand"><b>Poly</b>bench <span style="font-weight:400;color:var(--bar-ink2)">DV</span></span><span class="ctx">No project open</span>
    <span class="sp"></span>${ui.connected ? '' : '<span class="badge bad">SERVER NOT REACHABLE</span>'}
    <button data-act="theme" title="Light or dark">${ui.theme === 'dark' ? 'Light' : 'Dark'}</button><button data-act="help" title="Keyboard help">?</button>`;
  $('#timeline').innerHTML = ''; $('#left').innerHTML = ''; $('#right').innerHTML = '';
  $('#work').className = 'noleft noright';
  const recent = rec.length ? `<div class="wel-rec"><div class="pjsub">Recent projects</div>${rec.map(r => r.missing
    ? `<div class="wel-recm"><span><b>${esc(r.name || baseName(r.dir))}</b> <span class="pjbad small">missing</span> <span class="mono small faint">${wbrPath(r.dir)}</span></span><button class="sm" data-act="pjrecrm" data-dir="${esc(r.dir)}">Remove from list</button></div>`
    : `<button class="pjrecb" data-act="welrecent" data-dir="${esc(r.dir)}" title="${esc(r.dir)}"><b>${esc(r.name || baseName(r.dir))}</b><span class="mono small muted">${wbrPath(r.dir)}</span><span class="small faint">batch ${esc(r.batch || '?')}, ${esc((r.opened || '').replace('T', ' ').slice(0, 16))}</span></button>`).join('')}</div>` : '';
  $('#centre').innerHTML = `<div class="wel" id="welcome">
    <h1 class="wel-h">Polybench</h1>
    <p class="wel-sub muted">Bench validation for a batch of prototype boards, one step at a time.</p>
    <div class="wel-act"><button class="primary big" id="welnew" data-act="pjnew">+ New project</button><button class="big" data-act="pjopen">Open project&hellip;</button></div>
    ${ui.pjForm === 'new' ? pjNewForm(W) : ui.pjForm === 'open' ? pjOpenForm(W) : ''}
    ${ui.pjForm === 'open' ? '' : recent}
    <p class="wel-demo"><button class="link" data-act="demoopen">Try the demo project</button><span class="faint small"> Simulated data in its own folder. Your projects and instruments are not touched.</span></p>
    <div class="wel-how"><h2>How Polybench works</h2>${ONB.timeline(esc)}</div></div>`;
  renderBottomWelcome();
}
function renderBottomWelcome() {
  const el = $('#bottom'); if (!el) return;
  ui.bottomTab = 'plugins';
  el.className = !ui.bottomOpen ? 'collapsed' : 'tall';
  el.innerHTML = `<div class="btabs">${PLG.tab((NP || {}).plugins, true)}<span class="sp"></span>
    <button class="sm ghost" data-act="toggleBottom" title="Collapse (Ctrl+J)">${ui.bottomOpen ? '&#9662;' : '&#9652;'}</button></div><div class="bbody" id="bbody"></div>`;
  if (ui.bottomOpen) PLG.fill($('#bbody'));
}

/* ---- right inspector */
function renderRight() {
  const st = curStep(), el = $('#right'), b = ui.board;
  const ck = typeof AOI !== 'undefined' && AOI.active(st);
  el.classList.toggle('drawer', !!(ck && ui.aoiDrawer));
  if (ck) { const sc = el.querySelector('.cklist'), top = sc ? sc.scrollTop : 0, so = el.scrollTop; el.innerHTML = AOI.checklist(); const n = el.querySelector('.cklist'); if (n) n.scrollTop = top; el.scrollTop = so; return; }
  const hdr = `<div class="ph"><span>Inspector</span><span class="sp"></span><button data-act="toggleRight" title="Collapse">&raquo;</button></div>`;
  const bd = S.boards.find(x => x.label === b); const { t } = cur();
  let h = hdr + `<div class="insp"><h4>Board ${b}</h4><dl><dt>Serial</dt><dd class="mono">${esc(bd && bd.serial) || '<span class="faint">not set</span>'}</dd><dt>Step</dt><dd>${esc(st.title)}</dd>${st.per_board[b] && st.per_board[b].attempt > 1 ? `<dt>Attempt</dt><dd>${st.per_board[b].attempt}</dd>` : ''}</dl></div>`;
  if (t) {
    const lim = t.limit || {};
    h += `<div class="insp"><h4>Limit provenance</h4><dl><dt>Class</dt><dd>${esc(t.class) || 'none'}</dd><dt>Source</dt><dd>${esc(t.source_text) || '-'}</dd>${lim.closes_on ? `<dt>Closes on</dt><dd>${esc(lim.closes_on)}</dd>` : ''}<dt>Kind</dt><dd>${esc(t.kind)}${t.unit ? ', ' + esc(t.unit) : ''}</dd>${ui.advanced ? `<dt>Raw</dt><dd class="mono" style="font-size:11px">${esc(JSON.stringify(lim))}</dd>` : ''}</dl></div>`;
  }
  const a = S.action;
  h += `<div class="insp"><h4>Run safety</h4>${a ? `<span class="safe run">RUNNING</span><div style="margin-top:4px">${esc(a.title)} on ${esc(a.board)}</div>` : `<span class="safe">IDLE</span><div class="muted" style="margin-top:4px">no action running; no output enabled by this app</div>`}</div>`;
  const hist = S.retests.filter(r => r.board === b);
  if (hist.length) h += `<div class="insp"><h4>Retests on ${b}</h4>${hist.map(r => `<div>${esc(r.step)} attempt ${r.attempt}: ${esc(r.cause)}${r.fix ? '<br><span class="muted">fix: ' + esc(r.fix) + '</span>' : ''}</div>`).join('')}</div>`;
  const nt = S.notes.filter(n => (n.boards || []).includes(b)); if (nt.length) h += `<div class="insp"><h4>Notes on ${b} (${nt.length})</h4>${nt.slice(-8).map(n => `<div class="nin"><span class="ncat">${esc(n.category)}</span> ${esc([n.test_id, (n.designators || []).join(' ')].filter(Boolean).join(' '))} ${esc(n.text)}</div>`).join('')}</div>`;
  el.innerHTML = h;
}

/* ---- bottom panel */
function renderBottom() {
  if (!S) { renderBottomWelcome(); return; }
  if (NOTES.composerFocused()) { NOTES.refreshKeepFocus(); return; }       // never pull the note box from under the operator's hands
  if (VOICE.composerFocused()) { VOICE.refreshKeep(); return; }            // nor the typed-utterance box
  const el = $('#bottom'); el.className = !ui.bottomOpen ? 'collapsed' : (ui.bottomTab === 'journal' && ui.jview !== 'events') || ui.bottomTab === 'voice' ? 'tall' : ui.bottomTab === 'plugins' ? 'tall' : '';
  const a = S.action;
  const tabs = [['console', 'Console'], ['controls', 'Controls'], ['journal', 'Journal'], ['voice', 'Voice']].map(([t, n]) => `<button class="btab ${ui.bottomTab === t ? 'on' : ''}" data-act="btab" data-id="${t}">${n}</button>`).join('');
  el.innerHTML = `<div class="btabs">${tabs}${PLG.tab(S.plugins, ui.bottomTab === 'plugins')}<span class="sp"></span>${a ? `<span class="muted" style="margin-right:8px">${esc(a.title)} on ${esc(a.board)}: ${esc(a.text)}</span><span class="prog"><b id="progb" style="width:${Math.round(100 * (a.fraction || 0))}%"></b></span><button class="sm danger" data-act="abort">Abort</button>` : ''}
    <button class="sm ghost" data-act="toggleBottom" title="Collapse (Ctrl+J)">${ui.bottomOpen ? '&#9662;' : '&#9652;'}</button></div><div class="bbody" id="bbody"></div>`;
  fillBottom();
}
function fillBottom() {
  const b = $('#bbody'); if (!b) return;
  if (ui.bottomTab === 'console') {
    b.innerHTML = consoleLines.length ? consoleLines.map(clHtml).join('') : '<div class="faint">Action output appears here.</div>'; b.scrollTop = b.scrollHeight;
  } else if (ui.bottomTab === 'controls') {
    const a = S.action;
    b.innerHTML = `<div class="ctl"><div><b>Actions</b><div style="margin:6px 0">${a ? `Running: <b>${esc(a.title)}</b> (${esc(a.plugin)}) on ${esc(a.board)}, ${a.state}. ${esc(a.text)}<br><button class="danger" data-act="abort" style="margin-top:6px">Abort and go to safe state</button>` : 'Nothing running.'}</div>
      <button data-act="safeall" ${a ? 'disabled' : ''}>Safe state (all plugins in this protocol)</button>
      <div class="muted" style="margin-top:6px">Abort sends cancel, waits for the plugin (no kill timer), then calls safe_state in a fresh process.</div></div>
      <div><b>Capabilities</b><table style="margin-top:6px"><tr><th>Name</th><th>Kind</th><th></th></tr>${S.capabilities.map(c => `<tr><td>${esc(c.name)}</td><td>${esc(c.kind)}</td><td class="muted">${c.optional ? 'optional' : 'required'}${S.origin === 'simulated' ? ', simulated' : ''}</td></tr>`).join('')}</table>
      <div class="muted" style="margin-top:6px">ECAD: ${esc(S.ecad_note)}</div></div></div>`;
  } else if (ui.bottomTab === 'voice') {
    VOICE.fillBottom(b);
  } else if (ui.bottomTab === 'plugins') {
    PLG.fill(b);
  } else if (ui.jview !== 'events') {
    NOTES.fillBottom(b);
  } else {
    NOTES.fillBottom(b);
    if (journalRows.length) paintEvents();
    api('/api/journal?limit=300').then(r => { if (ui.bottomTab !== 'journal' || ui.jview !== 'events' || !r.ok || !$('#evlist')) return; journalRows = r.data; paintEvents(); });
  }
}
/* UIR-18: the Journal's Events view has filter chips; the Assembly chip shows what the Assembly log tab used to (placements, scans,
   auto-place cancels, and the start, skip and reverse of the Assembly aid). Which event types belong to a chip is fixed here. */
const JFILTERS = [['all', 'All'], ['assembly', 'Assembly'], ['results', 'Results'], ['voice', 'Voice'], ['actions', 'Actions']];
const JTYPES = {
  assembly: ['placed', 'placed_batch', 'auto_cancel', 'scan', 'assembly_begin', 'assembly_skip', 'assembly_unskip'],
  results: ['entry', 'accept', 'undo', 'proposal', 'reject', 'skip', 'retest_open', 'serial', 'stop', 'stop_ack', 'gate_override', 'gate_reject', 'tier2_assign', 'tier2_remove', 'protocol_accepted'],
  voice: ['voice'],
  actions: ['action_start', 'action_end', 'action_confirm', 'safe_state', 'sequence'],
};
const jOf = (e, f) => f === 'all' || (JTYPES[f] || []).includes(e.type);
function paintEvents() {
  const box = $('#evlist'); if (!box) return;
  const f = JFILTERS.some(x => x[0] === ui.jfilter) ? ui.jfilter : 'all';
  let bar = $('#jfilters');
  if (!bar) { box.insertAdjacentHTML('beforebegin', '<div class="jfilters" id="jfilters" role="group" aria-label="Filter the journal"></div>'); bar = $('#jfilters'); }
  bar.innerHTML = JFILTERS.map(([k, n]) => `<button class="jchip${k === f ? ' on' : ''}" data-act="jfilter" data-f="${k}" aria-pressed="${k === f}">${n} <span class="n">${journalRows.filter(e => jOf(e, k)).length}</span></button>`).join('');
  const rows = [...journalRows].filter(e => jOf(e, f)).reverse();
  box.innerHTML = rows.length ? rows.map(e => `<div class="jr"><span class="id">${e.id}</span><span class="faint">${esc(e.ts.slice(11, 19))}</span> <span class="ty">${esc(e.type)}</span>${esc(summ(e))}${ui.advanced ? ' <span class="faint">' + e.hash + '</span>' : ''}</div>`).join('') : `<div class="faint small" style="padding:6px 2px">No ${f === 'all' ? '' : esc(f) + ' '}events in this batch yet.</div>`;
}
function summ(e) { const p = e.payload; const keys = ['board', 'boards', 'test_id', 'step', 'value', 'unit', 'verdict', 'reason', 'cause', 'text', 'plugin', 'state', 'code', 'line', 'via']; return keys.filter(k => p[k] != null && p[k] !== '').map(k => k + '=' + (typeof p[k] === 'object' ? JSON.stringify(p[k]) : p[k])).join('  '); }
const clHtml = l => `<div class="cl ${esc(l.level)}"><span class="ts">${esc(l.ts)}</span>${esc(l.text)}</div>`;

/* ---- dialogs */
function dialog({ title, text, fields = [], ok = 'OK', danger = false, options = null, noCancel = false, abort = null }) {
  return new Promise(res => {
    const tt = $('#toast'); if (tt) tt.style.display = 'none';        // an earlier message does not sit over the question
    const root = $('#dialog-root');
    const f = fields.map(x => `<label>${esc(x.label)}</label>${x.area ? `<textarea name="${x.name}" rows="3"></textarea>` : `<input name="${x.name}" autocomplete="off">`}`).join('');
    root.innerHTML = `<div class="veil"><div class="dlg" role="dialog"><h2>${esc(title)}</h2>${text ? `<p>${esc(text)}</p>` : ''}<form>${f}
      ${options ? `<div class="optbtns">${options.map((o, i) => `<button type="button" class="${i === 0 ? 'primary' : ''}" data-opt="${esc(o)}"><kbd>${i + 1}</kbd> ${esc(o)}</button>`).join('')}</div>` : ''}
      <div class="btns">${abort ? `<button type="button" class="danger" data-abort>${esc(abort)}</button>` : ''}${noCancel ? '' : '<button type="button" data-cancel>Cancel</button>'}${options ? '' : `<button type="submit" class="${danger ? 'danger' : 'primary'}">${esc(ok)}</button>`}</div></form></div></div>`;
    const form = $('form', root); const first = form.querySelector('input,textarea'); if (first) first.focus(); else { const b = form.querySelector('button'); b && b.focus(); }
    const close = v => { root.innerHTML = ''; res(v); setTimeout(() => ui.afterDialog && ui.afterDialog(), 0); };
    form.addEventListener('submit', e => { e.preventDefault(); const o = {}; fields.forEach(x => o[x.name] = form.elements[x.name].value.trim()); if (fields.some(x => x.required && !o[x.name])) { toast('A reason is required', true); return; } close(o); });
    form.addEventListener('click', e => { if (e.target.closest('[data-cancel]')) close(null); if (e.target.closest('[data-abort]')) close('\u0000abort'); const o = e.target.closest('[data-opt]'); if (o) close(o.dataset.opt); });
    root.onkeydown = e => { if (e.key === 'Escape' && !noCancel) close(null); if (options && /^[1-9]$/.test(e.key) && options[+e.key - 1]) close(options[+e.key - 1]); };
  });
}
function checkAsk() {
  const a = S.action; const key = a && a.ask ? a.run + '|' + a.ask.prompt : null;
  if (!key) { if (ui.askKey && !document.querySelector('[data-asking]')) { /* answered elsewhere */ } ui.askKey = null; return; }
  if (ui.askKey === key) return; ui.askKey = key;
  dialog({ title: a.title, text: a.ask.prompt, options: a.ask.options, noCancel: true, abort: 'Abort and go to safe state' })
    .then(v => { if (v === '\u0000abort') api('/api/action/cancel', {}).then(r => { if (!r.ok) toast(r.data.error, true); }); else if (v) api('/api/action/answer', { value: v }); });   /* the veil covers the run bar, so Abort is in the dialog */
  const d = $('.dlg'); if (d) d.dataset.asking = '1';
}
function showHelp() {
  $('#dialog-root').innerHTML = `<div class="veil"><div class="dlg"><h2>Keyboard</h2><div class="helpgrid">
    <kbd>Enter</kbd><span>Accept the typed value (or the proposal when the box is empty), then move on</span>
    <kbd>Alt+A</kbd><span>Jump to the Anomalies strip (Tab reaches Redo and Add note)</span>
    <kbd>Shift+Enter</kbd><span>Apply the typed value to every empty board for this test (a FAIL asks for its own confirmation first)</span>
    <kbd>Esc</kbd><span>Clear the box</span><kbd>Up / Down</kbd><span>Previous or next test</span>
    <kbd>Alt+Left / Right</kbd><span>Previous or next board (also PageUp / PageDown)</span>
    <kbd>Ctrl+K</kbd><span>Scan box (Assembly aid)</span><kbd>Ctrl+B / Ctrl+J</kbd><span>Collapse left panel / bottom panel</span>
    <kbd>c</kbd><span>Add a note linked to the current step, board, test or BOM line (when no box has focus; Alt+C works anywhere)</span>
    <kbd>y n 1 2 3</kbd><span>Quick answer for yes/no and choice tests, then Enter</span></div>
    <div class="btns"><button class="primary" data-cancel>Close</button></div></div></div>`;
  $('#dialog-root').onclick = e => { if (e.target.closest('[data-cancel]') || e.target.classList.contains('veil')) $('#dialog-root').innerHTML = ''; };
  $('#dialog-root').onkeydown = e => { if (e.key === 'Escape') $('#dialog-root').innerHTML = ''; };
}

/* ------------------------------------------------------------------ events */
document.addEventListener('click', async e => {
  const el = e.target.closest('[data-act]'); if (!el) return;
  const d = el.dataset, st = S && curStep();
  switch (d.act) {
    case 'step': ui.pjLanded = false; ui.gate = null; ui.retired = null; ui.step = d.id; ui.test = null; ui.line = null; ui.tick = null; ui.pv = null; ui.draft = ''; ui.test = null; if (curStep().kind === 'assembly') asmEmpty(); render(); break;
    case 'board': ui.board = d.id; ui.pv = null; ui.draft = ''; ui.armed = null; if (st.kind === 'assembly') asmEmpty(); render(); break;
    case 'sectoggle': {
      if (e.target.closest('[data-vo], button')) break;               // Guide and the like are their own buttons
      const [sid, secid] = d.k.split('|'), stp = S.steps.find(x => x.id === sid), sec = stp && stp.sections.find(x => x.id === secid);
      if (secid === 'todo' ? bomHoldsCurrent() : sec && holdsCurrent(sec)) break;   // the group of the current test stays open
      ui.secOpen[d.k] = el.getAttribute('aria-expanded') !== 'true'; render(); break; }
    case 'test': ui.gate = null; ui.retired = null; ui.test = d.id; ui.pv = null; ui.draft = ''; ui.armed = null; render(); break;
    case 'gate': ui.gate = +d.i; ui.retired = null; render(); break;
    case 'retired': ui.retired = d.id; ui.gate = null; render(); break;
    case 'gateacc': { const bs = d.b.split(','); const r = await cmd('gate_accept', { gate: +d.i, boards: bs }); if (r.ok) toast('Tier 2 opened for ' + bs.join(' ')); break; }
    case 'gaterej': { const v = await dialog({ title: 'Reject the proposal for ' + d.b, text: 'The board stays out of Tier 2. The reason is recorded.', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Reject' }); if (v) await cmd('gate_reject', { gate: +d.i, boards: [d.b], reason: v.reason }); break; }
    case 't2send': { const v = await dialog({ title: 'Send ' + ui.board + ' to Tier 2', text: 'Opens every Tier 2 section of ' + st.title + ' for this board (attempt ' + (sb(st, ui.board).attempt || 1) + '). The reason is recorded.', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Send to Tier 2' }); if (v) await cmd('tier2_assign', { board: ui.board, step: st.id, sections: 'all', reason: v.reason }); break; }
    case 'cell': ui.board = d.b; ui.test = d.id; ui.pv = null; render(); break;
    case 'line': ui.line = +d.id; lineChanged(); render(); selectLine(ui.line); break;
    case 'theme': ui.theme = ui.theme === 'dark' ? 'light' : 'dark'; lsSet('theme', ui.theme); render(); break;
    case 'help': showHelp(); break;
    case 'autoskip': case 'autounskip': case 'autotick': case 'autocheck': case 'autorun': case 'autobench': case 'autoans': case 'autoopenimg':
    case 'autoproceed': case 'autodefects': case 'autodiag': case 'autoback': case 'autoretest': AUTO.act(d, st); break;
    case 'toggleLeft': ui.leftOpen = !ui.leftOpen; lsSet('leftOpen', ui.leftOpen); render(); break;
    case 'toggleRight':
      if (typeof AOI !== 'undefined' && AOI.active(curStep()) && AOI.narrow()) ui.aoiDrawer = !ui.aoiDrawer;
      else { ui.rightOpen = !ui.rightOpen; lsSet('rightOpen', ui.rightOpen); }
      render(); break;
    case 'toggleBottom': ui.bottomOpen = !ui.bottomOpen; lsSet('bottomOpen', ui.bottomOpen); render(); break;
    case 'asmbegin': { const r = await cmd('assembly_begin', {}); if (r.ok) { asmEmpty(); render(); } break; }
    case 'asmskip': { const r = await cmd('assembly_skip', {}); if (r.ok) render(); break; }
    case 'asmunskip': { const r = await cmd('assembly_unskip', {}); if (r.ok) { asmEmpty(); render(); } break; }
    case 'jfilter': ui.jfilter = d.f; paintEvents(); break;
    case 'asmjournal': ui.bottomTab = 'journal'; ui.jview = 'events'; ui.jfilter = 'assembly'; ui.bottomOpen = true; lsSet('bottomOpen', true); render(); break;
    case 'btab': ui.bottomTab = d.id; ui.bottomOpen = true; renderBottom(); break;
    case 'plset': PLG.toggle(d.n); break;
    case 'plcopy': copyText(d.t); toast('Copied: ' + d.t); break;
    case 'onbtoggle': ui.onbOpen = !ui.onbOpen; lsSet('onbOpen', ui.onbOpen); render(); break;
    case 'welrecent': {
      const r = await api('/api/project/open', { dir: d.dir });
      if (!r.ok) { toast((r.data && r.data.error) || 'could not open that project', true); break; }
      await pjSwitched('Opened: ' + r.data.project + ', batch ' + r.data.batch); break; }
    case 'demoopen': {
      const r = await api('/api/demo/open', {});
      if (!r.ok) { toast((r.data && r.data.error) || 'could not open the demo', true); break; }
      await pjSwitched('Demo project: simulated data, in its own folder'); break; }
    case 'democlose': {
      const r = await api('/api/demo/close', {});
      if (!r.ok) { toast((r.data && r.data.error) || 'could not leave the demo', true); break; }
      await pjSwitched(r.data.note || (r.data.back === 'project' ? 'Back to ' + r.data.project : 'Demo closed')); break; }
    case 'prev': st.kind === 'assembly' && ui.test === null ? moveLine(-1) : moveTest(-1); break;
    case 'next': st.kind === 'assembly' && ui.test === null ? moveLine(1) : moveTest(1); break;
    case 'entrygo': submitEntry(false); break;
    case 'entryconfirm': submitEntry(true); break;
    case 'quick': { const inp = $('#val'); inp.value = d.v; onInput(); await submitEntry(false); break; }
    case 'acceptprop': { const c = cellOf(ui.board, ui.test); acceptProp(+d.id, !!d.confirm, c.proposals.find(p => p.id == d.id)); break; }
    case 'an-retest': {   /* Redo one test (never a step retest): re-run its action, or clear the entry box for re-entry */
      const a = (S.anomalies || []).find(x => x.board === d.b && x.test_id === d.id);
      if (a && a.redo) { ui.board = d.b; await startRun(a.redo.step, a.redo.section, a.redo.action, d.b, false); break; }
      ui.board = d.b; ui.test = d.id; ui.redo = d.b + '|' + d.id; ui.draft = ''; ui.armed = null; render();
      { const iv = $('#val'); if (iv) { iv.value = ''; iv.disabled = false; iv.focus(); } }
      toast('Type the new value for ' + d.id + '; it replaces the earlier one (kept in the history).'); break; }
    case 'recordpending': { const r = await cmd('record_pending', { board: d.b }, true); if (r.ok) toast(r.data.recorded + ' instrument reading' + (r.data.recorded === 1 ? '' : 's') + ' recorded' + (r.data.left ? ', ' + r.data.left + ' left (board stopped or gated)' : '')); else toast(r.data.error, true); break; }
    case 'an-note': ui.board = d.b; ui.test = d.id; render(); NOTES.openHere(); break;
    case 'rejectprop': await cmd('reject', { board: ui.board, test_id: ui.test, proposal_id: +d.id }); break;
    case 'entryall': submitBatch(false); break;
    case 'batchconfirm': submitBatch(true); break;
    case 'batchcancel': ui.batch = null; updateEntryCtl(); { const iv = $('#val'); iv && iv.focus(); } break;
    case 'acceptall': { const r = await cmd('accept_all', { board: ui.board, step: ui.step }); if (r.ok) toast(`Accepted ${r.data.accepted} clean; left ${r.data.left} for a decision`); break; }
    case 'skip': { const v = await dialog({ title: 'Skip ' + ui.test + ' on ' + ui.board, text: 'A skipped test is shown as skipped, never as passed.', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Skip' }); if (v) { await cmd('skip', { board: ui.board, test_id: ui.test, reason: v.reason }); advance(); } break; }
    case 'note': NOTES.openHere(); break;
    case 'retest': { const v = await dialog({ title: `Send ${ui.board} back to ${st.title}`, text: 'Opens a new attempt. Earlier attempts stay in the journal and the report.', fields: [{ name: 'cause', label: 'Cause (what was wrong)', required: true }, { name: 'fix', label: 'Fix applied (optional)' }], ok: 'Open retest' }); if (v) { const r = await cmd('retest', { board: ui.board, step: ui.step, cause: v.cause, fix: v.fix }); if (r.ok) { ui.test = null; render(); } } break; }
    case 'override': { const v = await dialog({ title: 'Override the gate', text: 'The reason is recorded and listed in the report.', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Override', danger: true }); if (v) await cmd('override_gate', { board: ui.board, step: d.step, reason: v.reason }); break; }
    case 'ackstop': { const v = await dialog({ title: 'Acknowledge the stop', text: 'Say what you did about it (rework, re-measure).', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Acknowledge' }); if (v) await cmd('ack_stop', { board: ui.board, stop_id: +d.id, reason: v.reason }); break; }
    case 'protoaccept': {
      const m = S.meta, h = x => String(x || '').slice(0, 12) || 'unknown', n = x => x == null ? 'unknown' : x;
      const v = await dialog({ title: 'Protocol changed', text: `Accepted before: ${h(m.protocol_baseline)} (${n(m.protocol_tests_before)} tests). Now: ${h(m.protocol_current)} (${n(m.protocol_tests_now)} tests). Accepting records this in the journal and clears the badge. Results stay by test ID.`, fields: [{ name: 'reason', label: 'Reason (optional)' }], ok: 'Accept new protocol' });
      if (v) await cmd('protocol_accept', { hash: m.protocol_current, old_hash: m.protocol_baseline, reason: v.reason });
      break; }
    case 'run': { await startRun(ui.step, d.sec, d.action, ui.board, false); break; }
    case 'expuse': { EXP.use(d.i); break; }
    case 'runok': { const pr = ui.pendingRun; ui.pendingRun = null; if (pr) await startRun(pr.step, pr.sec, pr.action, pr.board, true); break; }
    case 'runno': ui.pendingRun = null; render(); break;
    case 'abort': { const r = await api('/api/action/cancel', {}); if (!r.ok) toast(r.data.error, true); break; }
    case 'safeall': { const r = await api('/api/action/safe_state', {}); toast(r.ok ? 'Safe state requested for all plugins; see Console' : r.data.error, !r.ok); ui.bottomTab = 'console'; renderBottom(); break; }
    case 'place': { ui.excOpen = true; const on = !(S.placed[ui.board] || []).includes(d.d); await cmd('placed', { board: ui.board, designators: [d.d], on }); break; }
    case 'placeall': placeAll(); break;
    case 'nextlist': nextOnList(); break;
    case 'autocancel': autoCancel('button'); break;
    case 'btick': { const set = tickSet(S.bom.find(l => l.line === ui.line)); set.has(d.b) ? set.delete(d.b) : set.add(d.b); render(); break; }
    case 'tickall': { const set = tickSet(S.bom.find(l => l.line === ui.line)); S.boards.forEach(b => set.add(b.label)); render(); break; }
    case 'tickrest': { ui.tick = null; render(); break; }
    case 'tickno': { tickSet(S.bom.find(l => l.line === ui.line)).clear(); render(); break; }
    case 'scpause': scControl(sc.st && sc.st.paused ? 'resume' : 'pause'); break;
    case 'scstop': scControl(sc.st && sc.st.enabled ? 'stop' : 'start'); break;
    case 'screscan': scControl('rescan'); break;
    case 'scango': doScan(); break;
    case 'ecadpin': { const r = await api('/api/scanner/control', { action: 'board', value: d.key }); if (!r.ok) { toast((r.data && r.data.error) || 'cannot select that document', true); break; }
      sc.st = r.data; scannerPaint(); const t = cur().t; if (t) { for (let i = 0; i < 4; i++) { await new Promise(r => setTimeout(r, i ? 1500 : 600)); const rr = await selectTest(t, true); if (rr.ok && !(rr.data.ecad || {}).error) break; } } render(); break; }
    case 'hl': { const t = cur().t; const line = st.kind === 'assembly' && ui.test === null ? S.bom.find(l => l.line === ui.line) : null; const ds = line ? line.designators : (t && t.highlight) || []; const inAsm = !!line; const r = inAsm ? await cmd('select_line', { line: line.line }, true) : await selectTest(t, true); if (r.ok) { const res = r.data.ecad || {}; toast(res.error ? res.error : 'Selected in ' + ecadName() + ': ' + ds.join(', '), !!res.error); render(); } break; }
    case 'pjcheck': probe(); break;
    case 'pjsearch': probe($('#pjsearch').value.trim()); break;
    case 'pjuse': { const r = await cmd('project_select', { fingerprint: d.fp }, true); if (!r.ok) toast(r.data.error || 'could not select', true); else { toast('Selected ' + r.data.document); ui.probe = null; render(); } break; }
    case 'pjaccept': { const n = $('#ccnote'); const r = await cmd('crosscheck_accept', { note: n ? n.value : '' }, true); if (!r.ok) toast(r.data.error || 'could not accept', true); break; }
    case 'pjskip': { const v = await dialog({ title: 'Continue without ECAD', text: 'Assembly highlights are disabled for this batch until ECAD is linked again. The reason is recorded and listed in the report.', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Continue without ECAD' }); if (v) await cmd('ecad_skip', { reason: v.reason }); break; }
    case 'pjunskip': { await cmd('ecad_skip_clear', {}); ui.probe = null; render(); break; }
    case 'pjtoggle': ui.cardOpen[d.id] = d.open !== '1'; render(); break;
    case 'pjnew': ui.pjForm = ui.pjForm === 'new' ? null : 'new'; if (ui.pjForm) { ui.pjNew = {}; ui.pjInsp.new = null; } render(); if (ui.pjForm) { const n = $('#pjn-dir'); n && n.focus(); } break;
    case 'pjopen': ui.pjForm = ui.pjForm === 'open' ? null : 'open'; if (ui.pjForm) { ui.pjOpenF = {}; ui.pjInsp.open = null; } render(); if (ui.pjForm) { const n = $('#pjo-dir'); n && n.focus(); } break;
    case 'pjformclose': ui.pjForm = null; render(); break;
    case 'pjbrowse': {
      const t = d.target, W = WS();
      const isP = t.endsWith('.protocol');
      const cur = t === 'new.dir' ? ui.pjNew.dir : t === 'open.dir' ? ui.pjOpenF.dir : t === 'new.protocol' ? (ui.pjNew.protocol ?? (W.protocol || {}).path) : t === 'open.protocol' ? ui.pjOpenF.protocol : '';
      const start = cur ? (isP ? dirName(cur) : cur) : isP ? dirName((W.protocol || {}).path || '') : '';
      fsbOpen(t, start); break; }
    case 'fsbgo': fsbGo(d.path); break;
    case 'fsbgo2': fsbGo(($('#fsbpath') || {}).value || ''); break;
    case 'fsbpick': fsbChoose(d.path); break;
    case 'fsbuse': { const f = ui.fsb; if (!f || !f.data) break; const sub = (($('#fsbsub') || {}).value || '').trim().replace(/^\/+|\/+$/g, ''); fsbChoose(f.data.path.replace(/\/$/, '') + (sub ? '/' + sub : '')); break; }
    case 'fsbclose': fsbClose(); break;
    case 'pjuseproto': ui.pjOpenF.protocol = (WS().protocol || {}).path || ''; render(); break;
    case 'pjrecrm': { const r = await api('/api/recent/remove', { dir: d.dir }); const hold = S || NP; if (r.ok && hold && hold.workspace) hold.workspace.recent = r.data.recent; render(); break; }
    case 'pjrecent': case 'pjparent': ui.pjOpenF.dir = d.dir; pjInspect('open', true); render(); break;
    case 'pjnewgo': {
      const f = ui.pjNew; if (!(f.name || '').trim()) { toast('Give the project a name', true); const n = $('#pjn-name'); n && n.focus(); break; }
      const boards = (f.boards || '').trim() ? parseInt(f.boards, 10) : null;
      if (boards !== null && !(boards > 0 && boards < 100)) { toast('Boards: a number from 1 to 99, or leave it empty', true); break; }
      el.disabled = true;
      const r = await api('/api/project/new', { dir: (f.dir || '').trim(), name: f.name.trim(), protocol: String(f.protocol ?? ((WS().protocol || {}).path || '')).trim(), batch: (f.batch || '').trim(), boards, ecad: WS().ecad.program, confirm: !!f.confirm });
      el.disabled = false;
      if (!r.ok) { ui.pjNew.err = (r.data && r.data.error) || 'could not create the project'; toast(ui.pjNew.err, true); render(); break; }
      await pjSwitched('Project created: ' + r.data.project); break; }
    case 'pjopengo': {
      const f = ui.pjOpenF, batch = f.useNew ? (f.newBatch || '').trim() : f.batch;
      if (f.useNew && !batch) { toast('Name the new batch', true); break; }
      el.disabled = true;
      const r = await api('/api/project/open', { dir: (f.dir || '').trim(), batch, protocol: (f.protocol || '').trim() });
      el.disabled = false;
      if (!r.ok) { ui.pjOpenF.err = (r.data && r.data.error) || 'could not open the project'; toast(ui.pjOpenF.err, true); render(); break; }
      await pjSwitched((r.data.adopted ? 'Adopted and opened: ' : 'Opened: ') + r.data.project + ', batch ' + r.data.batch); break; }
    case 'pjbatch': {
      const W = WS();
      const v = await dialog({ title: 'Open batch ' + d.name, text: `Polybench switches to batch ${d.name} of ${W.project.name}. The current batch ${W.batch.name} stays as it is in its folder.`, ok: 'Open batch' });
      if (!v) break;
      const r = await api('/api/project/open', { dir: W.project.dir, batch: d.name });
      if (!r.ok) { toast((r.data && r.data.error) || 'could not open that batch', true); break; }
      await pjSwitched('Opened batch ' + r.data.batch); break; }
    case 'pjcopy': copyText(d.path); break;
    case 'pjreveal': { const r = await api('/api/reveal', { path: d.path }); toast(r.ok ? (r.data.stubbed ? 'Show in Finder (not opened: test mode): ' : 'Shown in Finder: ') + d.path : (r.data.error || 'cannot show that path'), !r.ok); break; }
    case 'pjhelp': ui.pjHelp = !ui.pjHelp; render(); break;
    case 'pjplat': await pjSetPlatform(d.id); break;
    case 'pjreqopen': ui.ecadReqOpen = !ui.ecadReqOpen; render(); if (ui.ecadReqOpen) { const n = $('#pjreq'); n && n.focus(); } break;
    case 'pjreqsave': {
      const t = (($('#pjreq') || {}).value || '').trim(); if (!t) { toast('Say which ECAD program you would like', true); break; }
      const r = await api('/api/ecad/request', { text: t });
      if (!r.ok) { toast((r.data && r.data.error) || 'could not save the request', true); break; }
      ui.ecadReqOpen = false; ui.ecadReqText = ''; ui.ecadReqMsg = `Request for <b>${esc(r.data.request.text)}</b> saved to <span class="mono">${esc(r.data.file)}</span>, nothing is sent.`;
      await refresh(); break; }
    case 'bomadd': case 'bomreplace': { const b = S.project_bom || {}; fsbOpen('bom', b.file ? dirName(b.file) : dirName((WS().protocol || {}).path || '')); break; }
    case 'bomremove': bomRemove(false); break;
    case 'bompick': ui.bomMatches = null; bomLoad(d.path); break;
    case 'bommatchx': ui.bomMatches = null; render(); break;
    case 'repreview': render(); break;
    case 'repgen': { const r = await api('/api/report/generate', {}); ui.reportMsg = r.ok ? 'Written to ' + r.data.folder : r.data.error; const m = $('#repmsg'); if (m) m.textContent = ui.reportMsg; break; }
  }
});
async function startRun(step, sec, action, board, confirmed) {
  const r = await api('/api/action/run', { step, section: sec, action, board, confirmed });
  if (!r.ok) { toast(r.data.error, true); return; }
  if (r.data.needs_prompt) { ui.pendingRun = { step, sec, action, board, prompt: r.data.needs_prompt, title: r.data.title }; render(); const b = $('#runok'); b && b.focus(); return; }
  ui.pendingRun = null; ui.bottomTab = 'console'; ui.bottomOpen = true; await refresh();
}
let hlT;
/* Select a test's parts, pins and nets. A server that predates select_test (static files are served live, Python needs a restart) answers 404:
   fall back to the designator highlight it has, so the page keeps working until the restart. */
async function selectTest(t, force) {
  const r = await cmd('select_test', { test_id: t.id, force: !!force }, true);
  if (!r.ok && r.status === 404 && (t.highlight || []).length) return cmd('highlight', { designators: t.highlight }, true);
  return r;
}
function autoHighlight(t) {   // once whenever a different test comes into focus, never on a re-render; the card's button repeats it on demand
  if (!t || ui.hlTest === t.id) return;
  const had = !!ui.hlTest; ui.hlTest = t.id; clearTimeout(hlT);
  if (S.project && S.project.ecad_skipped) return;                       // Continue without ECAD: nothing is sent
  if (!(t.highlight || []).length && !(t.nets || []).length && !had) return;
  hlT = setTimeout(() => selectTest(t, false), 250);
}

document.addEventListener('input', e => { if (e.target.id === 'val') onInput(); else if (e.target.id === 'pjreq') ui.ecadReqText = e.target.value; else if (e.target.type !== 'radio') pjField(e); });
/* BOM drop (BOM card): the drop gives a name and a size, the server looks for the file. Elsewhere on the project page a drop does nothing. */
document.addEventListener('dragover', e => { const z = e.target.closest && e.target.closest('#bomdrop'); if (z || (S && ui.step === 'project')) { e.preventDefault(); } if (z) z.classList.add('over'); });
document.addEventListener('dragleave', e => { const z = e.target.closest && e.target.closest('#bomdrop'); if (z) z.classList.remove('over'); });
document.addEventListener('drop', e => {
  const z = e.target.closest && e.target.closest('#bomdrop');
  if (!z && !(S && ui.step === 'project')) return;
  e.preventDefault(); if (!z) return;
  z.classList.remove('over'); if (z.closest('fieldset[disabled]')) return;
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]; if (f) bomDropped(f);
});
document.addEventListener('change', async e => {
  if (e.target.type === 'radio' && e.target.dataset.pjf) { pjField(e); return; }
  if (e.target.id === 'pjsearch') probe(e.target.value.trim());
  if (e.target.id === 'pjplatsel') { if (e.target.value) await pjSetPlatform(e.target.value); }
  if (e.target.id === 'adv') { ui.advanced = e.target.checked; lsSet('advanced', ui.advanced); render(); }
  if (e.target.id === 'advmode') { const sx = curStep(); lsSet('advance:' + sx.id, e.target.checked ? 'board' : 'test'); render(); }
  if (e.target.id === 'op') await cmd('operator', { name: e.target.value });
  if (e.target.id === 'nb') await cmd('boards', { n: +e.target.value });
  if (e.target.classList.contains('serial')) await saveSerial(e.target);
});
/* A redraw can replace a serial box between typing and leaving it; the new box holds the text but fires no change event, so the serial was
   never saved. Leaving the box saves it when the server does not hold that text yet (one save per board and text at a time). */
const serialSaving = {};
async function saveSerial(el) {
  const b = el.dataset.b, v = el.value.trim(), held = (S.boards.find(x => x.label === b) || {}).serial || '';
  if (v === held || serialSaving[b] === v) return;
  serialSaving[b] = v;
  try { await cmd('serial', { board: b, serial: el.value }); } finally { delete serialSaving[b]; }
}
document.addEventListener('focusout', e => { if (S && e.target.classList && e.target.classList.contains('serial')) saveSerial(e.target); });
document.addEventListener('keydown', e => {
  if (!S || document.querySelector('.veil')) return;
  const st = curStep(), inInput = ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName);
  const mod = e.ctrlKey || e.metaKey;
  if (e.target.dataset && e.target.dataset.act === 'sectoggle' && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); e.target.click(); return; }
  if (e.key === 'Escape' && ui.auto) { e.preventDefault(); autoCancel('esc'); return; }
  if (mod && e.key === 'b') { e.preventDefault(); ui.leftOpen = !ui.leftOpen; render(); return; }
  if (mod && e.key === 'j') { e.preventDefault(); ui.bottomOpen = !ui.bottomOpen; render(); return; }
  if (mod && e.key === 'k') { e.preventDefault(); if (st.kind !== 'assembly') { ui.step = (S.steps.find(s => s.kind === 'assembly') || st).id; ui.test = null; render(); } const sc = $('#scan'); sc && sc.focus(); return; }
  if (e.altKey && e.code === 'KeyA') { e.preventDefault(); const f = $('#anoms .anjump'); if (f) { f.scrollIntoView({ block: 'nearest' }); f.focus(); } return; }
  if (e.altKey && e.code === 'KeyC') { e.preventDefault(); NOTES.openHere(); return; }
  if (e.altKey && e.key === 'ArrowLeft') { e.preventDefault(); moveBoard(-1); return; }
  if (e.altKey && e.key === 'ArrowRight') { e.preventDefault(); moveBoard(1); return; }
  if (e.key === 'PageDown') { e.preventDefault(); moveBoard(1); return; }
  if (e.key === 'PageUp') { e.preventDefault(); moveBoard(-1); return; }
  if (e.target.classList && e.target.classList.contains('serial') && e.key === 'Enter') { e.preventDefault(); const all = [...document.querySelectorAll('.serial')]; const n = all[all.indexOf(e.target) + 1]; e.target.blur(); setTimeout(() => n && n.focus(), 150); return; }
  if (e.target.id === 'scan') { if (e.key === 'Enter') { e.preventDefault(); doScan(); } else if (e.key === 'ArrowDown') { e.preventDefault(); moveLine(1); } else if (e.key === 'ArrowUp') { e.preventDefault(); moveLine(-1); } return; }
  if (e.target.id === 'val') {
    if (e.key === 'Enter') { e.preventDefault(); if (e.shiftKey && e.target.value.trim()) submitBatch(false); else submitEntry(e.shiftKey); }
    else if (e.key === 'Escape') { ui.batch = null; e.target.value = ''; ui.draft = ''; ui.pv = null; ui.armed = null; updateEntryCtl(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveTest(-1); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); moveTest(1); }
    return;
  }
  if (inInput) return;
  if (e.key === '?') { showHelp(); return; }
  if ((e.key === 'c' || e.key === 'C') && !mod && !e.altKey) { e.preventDefault(); NOTES.openHere(); return; }
  if (st.kind === 'assembly' && ui.test === null) {
    if (e.key === 'Enter') { e.preventDefault(); placeAll(); } else if (e.key === 'ArrowDown') { e.preventDefault(); moveLine(1); } else if (e.key === 'ArrowUp') { e.preventDefault(); moveLine(-1); }
    else if (e.key.length === 1 && !mod) { const sc = $('#scan'); sc && sc.focus(); }
    return;
  }
  if ($('#val') && !$('#val').disabled) {
    if (e.key === 'ArrowUp') { e.preventDefault(); moveTest(-1); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); moveTest(1); }
    else if (e.key === 'Enter') { e.preventDefault(); const iv = $('#val'); iv.focus(); if (e.shiftKey && iv.value.trim()) submitBatch(false); else submitEntry(e.shiftKey); }
    else if (e.key.length === 1 && !mod && !e.altKey) { $('#val').focus(); }  // the character lands in the box
  }
});

/* ------------------------------------------------------------------ state and events */
let refSeq = 0, refApplied = 0;
/* The ECAD platform is a property of the project, not of this tab: another tab (or the command line) may change it. Every state
   broadcast compares it with what this tab last looked at; on a change the probe, the callouts and the open/closed cards start over (D5, D6). */
function ecadChanged() {
  const E = (S.workspace || {}).ecad || {}, now = (E.program || '') + '|' + (E.saved || '');
  if (ui.seenEcad !== undefined && ui.seenEcad !== now) pjResetEcad();
  else if (ui.probe && ui.probe.ecad && E.program && ui.probe.ecad !== E.program) pjResetEcad();
  ui.seenEcad = now;
}
/* Switching the platform keeps today's behaviour (KC-03): the server clears the stored document, and the operator confirms it again. */
async function pjSetPlatform(id) {
  const E = WS().ecad; if (id === E.program && E.saved === id) return;
  ui.platBusy = true; ui.ecadReqMsg = ''; render();
  const r = await api('/api/ecad/platform', { program: id });
  ui.platBusy = false;
  if (!r.ok) { toast((r.data && r.data.error) || 'could not switch the ECAD platform', true); render(); return; }
  pjResetEcad(); ui.seenEcad = r.data.program + '|' + r.data.program;   // this tab made the switch: its own refresh is not 'another tab'
  // every switch starts with no callout from an earlier one (D6)
  if (r.data.cleared) ui.ecadCleared = { ...r.data.cleared, to: r.data.program };
  if (r.data.restored) ui.ecadRestored = { ...r.data.restored, to: r.data.program };
  toast('ECAD platform: ' + platLabel(r.data.program)); await refresh();
}
function pjResetEcad() {
  ui.probe = null; ui.probeBusy = false; ui.probeGen = (ui.probeGen || 0) + 1; ui.probeSearch = '';
  ui.ecadCleared = null; ui.ecadRestored = null; ui.cardOpen.plat = false; ui.cardOpen.link = null; ui.cardOpen.doc = null; ui.ccFocus = null;
}
async function refresh() {
  const my = ++refSeq;               // every caller gets its own fetch; an older reply never overwrites a newer one
  const r = await api('/api/state');
  if (r.ok) {
    if (my > refApplied) {
      refApplied = my; ui.connected = true;
      if (r.data.no_project) { S = null; NP = r.data; stateReady(); render(); }
      else { S = r.data; NP = null; stateReady(); if (!refresh.rankDone) { refresh.rankDone = true; await loadRank(); } ecadChanged(); RES.detect(S); render(); RES.applyFlash(); }
    }
  }
  else if (r.status === 0) { ui.connected = false; if (S) renderAppbar(); else if (NP) renderWelcome(); }
}
function connect() {
  const es = new EventSource('/api/events');
  es.onopen = () => { ui.connected = true; fetch('/api/session', { cache: 'no-store' }).catch(() => {}).then(() => refresh()); };   // fresh operator cookie after a server restart
  es.onerror = () => { ui.connected = false; if (S) renderAppbar(); else if (NP) renderWelcome(); };
  es.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.kind === 'console') { consoleLines.push(m); if (consoleLines.length > 4000) consoleLines.shift(); if (ui.bottomTab === 'console') { const b = $('#bbody'); if (b) { const atEnd = b.scrollTop + b.clientHeight >= b.scrollHeight - 30; b.insertAdjacentHTML('beforeend', clHtml(m)); if (atEnd) b.scrollTop = b.scrollHeight; } } }
    else if (m.kind === 'action') { if (S) { if (m.current && (!S.action || S.action.run !== m.current.run) && !ui.bottomOpen) { ui.bottomOpen = true; ui.bottomTab = 'console'; } S.action = m.current; renderBottom(); renderRight(); checkAsk(); } refresh(); }
    else if (m.kind === 'state' || m.kind === 'sequence') refresh();
    else if (m.kind === 'project_switched') pjSwitched();
    else if (m.kind === 'meter' || m.kind === 'meter_advance' || m.kind === 'meter_focus') METER.onEvent(m);
    else if (m.kind === 'voice' || m.kind === 'voice_nav' || m.kind === 'voice_aoi' || m.kind === 'voice_status') VOICE.onEvent(m);
  };
}
(async function init() {
  if (/[?&]reset=1\b/.test(location.search)) {           // recovery: forget every saved layout / view choice, then reload clean
    try { Object.keys(localStorage).filter(k => k.startsWith('pbdv.')).forEach(k => localStorage.removeItem(k)); } catch { /* private mode */ }
    try { history.replaceState(null, '', location.pathname); } catch { /* ok */ }
  }
  ui.theme = lsGet('theme', 'light'); ui.autoOn = lsGet('autoplace', false); ui.advanced = lsGet('advanced', false); ui.advance = lsGet('advance', 'test');
  ui.ckGroup = lsGet('ckGroup', 'board'); ui.leftOpen = lsGet('leftOpen', window.innerWidth > 640); ui.rightOpen = lsGet('rightOpen', true); ui.bottomOpen = lsGet('bottomOpen', false); ui.onbOpen = lsGet('onbOpen', false);
  const r = await api('/api/console'); if (r.ok) consoleLines.push(...r.data);
  await refresh(); connect(); if (typeof METER !== 'undefined') METER.init();
})();

/* Holding Shift turns the entry into 'apply to all empty boards': Accept hides, the batch button goes green (operator request). */
(() => {
  const set = on => document.body.classList.toggle('shifty', on);
  document.addEventListener('keydown', e => { if (e.key === 'Shift') set(true); });
  document.addEventListener('keyup', e => { if (e.key === 'Shift') set(false); });
  window.addEventListener('blur', () => set(false));
})();
