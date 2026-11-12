'use strict';
/* AI connector, operator side (docs/AI_CONNECTOR_ARCHITECTURE.md).
   - renderAiCard(S): card 3 "AI assistant" on the Project page, from S.ai (connection, setup commands, privacy, action log).
   - the question panel: questions the assistant asks the operator, shown in a strip above the work area (never over the editor's buttons) on every step until answered.
   - the operator's focus (step, board, test) is posted after each render so the assistant can see what is on screen.
   Nothing here sends anything outside this Mac. */

const AIC = { focusKey: '', focusT: null, lastFetch: 0, tick: null };

(function aiStyle() {
  const css = `
  .aic .aistat{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .aic .cmd{display:flex;gap:6px;align-items:flex-start;margin:4px 0 8px}
  .aic .cmd pre{flex:1;min-width:0;margin:0;padding:6px 8px;border:1px solid var(--border2);border-radius:2px;font:12px var(--mono);white-space:pre;overflow-x:auto;background:linear-gradient(to right,var(--panel3) 30%,transparent) left center/24px 100% no-repeat local,linear-gradient(to left,var(--panel3) 30%,transparent) right center/24px 100% no-repeat local,linear-gradient(to right,rgba(0,0,0,.4),transparent) left center/14px 100% no-repeat scroll,linear-gradient(to left,rgba(0,0,0,.4),transparent) right center/14px 100% no-repeat scroll,var(--panel3)}
  .aic .cmd pre::-webkit-scrollbar{height:10px} .aic .cmd pre::-webkit-scrollbar-track{background:var(--border2)} .aic .cmd pre::-webkit-scrollbar-thumb{background:var(--faint);border-radius:5px}
  .aic .privacy{border-left:3px solid var(--review);background:var(--review-bg);padding:6px 10px;margin:8px 0;font-size:13px}
  .aic .alog{max-height:220px;overflow:auto}
  .aic .arow{display:flex;gap:10px;align-items:baseline;padding:2px 0;border-bottom:1px solid var(--border2)}
  .aic .arow .at{flex:none;color:var(--muted)} .aic .arow .ax{flex:1;min-width:0;overflow-wrap:anywhere} .aic .arow .ao{flex:none}
  .aic .oc-refused,.aic .oc-error{color:var(--fail);font-weight:600}
  #app.has-aistrip{grid-template-rows:36px auto 58px auto minmax(0,1fr) auto}
  #aistrip{max-height:34vh;overflow:auto;background:var(--panel);border-bottom:2px solid var(--accent)}
  #aistrip:empty{display:none}
  #airec{background:var(--fail-bg);border-bottom:2px solid var(--fail);color:var(--text);padding:6px 12px;font-size:13px}
  #airec b{color:var(--fail)} #airec pre{margin:4px 0 0;padding:4px 8px;background:var(--panel3);border:1px solid var(--border2);font:12px var(--mono);white-space:pre-wrap;word-break:break-all}
  #aiq{padding:6px 12px}
  .aic .trial{border:1px solid var(--accent);border-radius:3px;padding:6px 8px;margin:4px 0 8px;background:var(--panel-w)}
  #aiq h3{margin:0 0 4px;font-size:13px;color:var(--accent);text-transform:uppercase;letter-spacing:.4px}
  #aiq .q{border-top:1px solid var(--border2);padding:6px 0;display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center}
  #aiq .q .where{flex:none}
  #aiq .q>.row:last-child{flex:1 1 320px}
  #aiq .q:first-of-type{border-top:0}
  #aiq .qt{font-size:14px;margin:0;flex:1 1 280px}
  #aiq .row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  #aiq input{flex:1;min-width:120px}`;
  const s = document.createElement('style'); s.id = 'ai-style'; s.textContent = css; document.head.appendChild(s);
})();

function aiEsc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function aiSeenText(a) {
  if (a.last_seen_s == null) return 'never';
  const age = a.last_seen_s + (Date.now() - AIC.lastFetch) / 1000;
  return age < 90 ? Math.round(age) + ' s ago' : Math.round(age / 60) + ' min ago';
}
function aiConnected(a) {
  if (a.last_seen_s == null) return false;
  return a.last_seen_s + (Date.now() - AIC.lastFetch) / 1000 < (a.heartbeat_window_s || 60);
}

/* Card 3, collapsed by default to one line. Returns HTML; the Project page puts it after the BOM card. Optional, not gated. */
function renderAiCard(S) {
  const a = (S && S.ai) || null;
  if (!a) return '';
  if (a.error) return `<div class="box aic pjcard st-opt" id="aicard" data-card="ai"><h3><span class="no">3</span> AI assistant <span class="muted small">optional</span></h3><div class="pjb muted">${aiEsc(a.error)}</div></div>`;
  const on = a.mode !== 'off' && aiConnected(a);
  const modeTxt = { propose: 'read, highlight, propose values, notes, questions, plan drafts', read: 'read only', off: 'off: every assistant request is refused' }[a.mode] || a.mode;
  const stat = a.mode === 'off' ? '<span class="vd fail" id="aistat">OFF</span>'
    : on ? `<span class="vd pass" id="aistat">CONNECTED</span> <span><b>${aiEsc(a.client || 'assistant')}</b></span> <span class="muted small">last request <span id="aiseen">${aiSeenText(a)}</span></span>`
    : `<span class="vd none" id="aistat">NOT CONNECTED</span> <span class="muted small">${a.last_seen_s == null ? 'no assistant has connected since the app started' : 'last request <span id="aiseen">' + aiSeenText(a) + '</span>'}</span>`;
  const cmd = (id, label, text) => `<div class="small muted">${label}</div><div class="cmd"><pre id="${id}">${aiEsc(text)}</pre><button class="sm" data-ai-copy="${id}" title="Copy to the clipboard">Copy</button></div>`;
  const rows = (a.actions || []).map(r => `<div class="arow"><span class="mono small at">${aiEsc((r.ts || '').slice(11, 19))}</span><span class="small ax">${aiEsc(r.text)}</span><span class="small ao oc-${aiEsc(r.outcome)}">${r.outcome && r.outcome !== 'ok' ? aiEsc(r.outcome.toUpperCase()) : ''}</span></div>`).join('');
  const dr = a.draft || {};
  const draft = dr.draft_exists ? `<div class="callout" style="margin:6px 0">The assistant wrote a test-plan draft: <b>${aiEsc(dr.summary || '')}</b>${dr.errors ? ` <span class="badge bad">${dr.errors} lint error${dr.errors > 1 ? 's' : ''}</span>` : ''}. <button class="sm" data-act="step" data-id="testplan">Review in step 4</button></div>` : '';
  const qn = (a.questions || []).length;
  const modeLine = `Permissions: <b>${aiEsc(a.mode)}</b> <span class="muted">(${aiEsc(modeTxt)})</span>`;
  // collapsed (the default): one line with the permission mode and the connection state (UIR-16)
  const statShort = a.mode === 'off' ? '' : on ? `<span class="vd pass" id="aistat">CONNECTED</span>` : `<span class="vd none" id="aistat">NOT CONNECTED</span>`;
  const sum = `<span id="aisum">${modeLine}${statShort ? ' ' + statShort : ''}${qn ? ` <span class="badge bad">${qn} open question${qn > 1 ? 's' : ''}</span>` : ''}</span>`;
  const open = typeof cardOpen === 'function' ? cardOpen('ai', false) : false;
  // part 1: connection and permissions; part 2: connect an assistant; part 3: activity
  const p1 = `<div class="pjsec aipart" id="ai-conn"><div class="pjsub">1. Connection and permissions</div>
    <div class="aistat">${stat}</div>
    <div class="small" style="margin:6px 0">${modeLine}. Only you accept results, accept a FAIL, override a gate or approve a plan. Set with POLYBENCH_AI_PERMISSIONS.${a.mode_note ? ` <span class="badge bad">${aiEsc(a.mode_note)}</span>` : ''}</div>
    <div class="privacy"><b>Privacy.</b> What the assistant reads (results, photos, plan, part list) leaves this Mac through the assistant's cloud service. Polybench itself sends nothing.</div></div>`;
  const p2 = `<div class="pjsec aipart" id="ai-setup"><div class="pjsub">2. Connect an assistant</div>
    <details ${on ? '' : 'open'}><summary class="small"><b>Setup commands</b></summary>
      <div class="trial"><b>Try it (recommended first): does not change your Claude Code configuration.</b>
        <div class="small muted">Step 1 writes a small config file in ~/.config/polybench. Step 2 starts Claude Code with it for that session only; nothing is added to Claude Code's own settings.</div>
        ${cmd('aic-trial1', 'Step 1: write the trial config (once)', a.setup.trial_setup)}
        ${cmd('aic-trial2', 'Step 2: start Claude Code with it, interactive (in your project folder). Claude asks you to approve each Polybench tool the first time', a.setup.claude_trial)}
        ${cmd('aic-trial3', 'Or one question without a terminal session (claude -p cannot show the permission prompt, so this form pre-approves the Polybench tools only)', a.setup.claude_trial_p)}</div>
      ${cmd('aic-claude', 'Permanent: EDITS your Claude Code configuration (adds the polybench server for every session). Run once in your project folder', a.setup.claude)}
      ${cmd('aic-codex', 'Codex: add to ~/.codex/config.toml', a.setup.codex)}
      ${cmd('aic-skills', 'Skills for Claude Code (bench assistant, test plan, troubleshooting, plugin authoring)', a.setup.skills)}
      <div class="small muted">Codex reads AGENTS.md in the Polybench repo. Polybench never edits an assistant's settings; these commands do only what you run.</div>
    </details></div>`;
  const p3 = `<div class="pjsec aipart" id="ai-activity"><div class="pjsub">3. Activity</div>
    ${qn ? `<div class="small"><b>${qn} open question${qn > 1 ? 's' : ''}</b> from the assistant (strip above the work area).</div>` : ''}
    ${draft}
    <div class="small" style="margin-top:6px"><b>Assistant actions</b> <span class="muted">(newest first; also in the journal with source: assistant)</span></div>
    ${rows ? `<div class="alog" id="ailog">${rows}</div>` : '<div class="muted small" id="ailog">No assistant action yet.</div>'}</div>`;
  return pjCard('ai', 3, 'AI assistant', null, { open, toggle: true, labels: ['Show', 'Hide'], body: p1 + p2 + p3, summary: sum,
    extra: '<span class="muted small" style="text-transform:none;letter-spacing:0">optional, not needed to continue</span>' }).replace('<div class="box pjcard', '<div class="box aic pjcard').replace('id="pjc-ai"', 'id="aicard"');
}

/* ---- question panel */
function aiStrip() {
  let st = document.getElementById('aistrip');
  const app = document.getElementById('app'), work = document.getElementById('work');
  if (!st && app && work) { st = document.createElement('div'); st.id = 'aistrip'; app.insertBefore(st, work); }
  return st;
}
function aiQuestionsPaint() {
  const a = (S && S.ai) || {};
  const qs = a.questions || [], rec = a.recovery;
  const st = aiStrip(); if (!st) return;
  const app = document.getElementById('app');
  if (!qs.length && !rec) { st.innerHTML = ''; app.classList.remove('has-aistrip'); return; }
  app.classList.add('has-aistrip');
  const keep = {}; st.querySelectorAll('input[data-q]').forEach(i => { keep[i.dataset.q] = i.value; });
  const had = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.q : null;
  const recHtml = rec ? `<div id="airec" role="alert"><b>The master test plan does not load. Running on the last good plan.</b> ${aiEsc(rec.error || '')}
    <div class="small">The master file was not changed. To put the last good plan back as the master, run this in a terminal (or approve a corrected AI draft in step 4):</div><pre>${aiEsc(rec.command || rec.lint_command || '')}</pre></div>` : '';
  const qHtml = qs.length ? `<div id="aiq" role="dialog" aria-label="Questions from the AI assistant"><h3>Question from the AI assistant</h3>` + qs.map(q => `<div class="q"><p class="qt">${aiEsc(q.text)}</p>
    ${q.board || q.test_id ? `<div class="small muted where">${aiEsc([q.board, q.test_id].filter(Boolean).join(' / '))}</div>` : ''}
    ${(q.options || []).length ? `<div class="row">${q.options.map(o => `<button class="sm" data-aiq-opt="${aiEsc(q.qid)}" data-v="${aiEsc(o)}">${aiEsc(o)}</button>`).join('')}</div>` : ''}
    <div class="row"><input data-q="${aiEsc(q.qid)}" placeholder="type an answer" autocomplete="off"><button class="sm primary" data-aiq-ans="${aiEsc(q.qid)}">Answer</button><button class="sm" data-aiq-dis="${aiEsc(q.qid)}">Dismiss</button></div></div>`).join('')
    + `<div class="small muted">An answer is a message to the assistant, not a result.</div></div>` : '';
  st.innerHTML = recHtml + qHtml;
  st.querySelectorAll('input[data-q]').forEach(i => { if (keep[i.dataset.q]) i.value = keep[i.dataset.q]; if (had === i.dataset.q) i.focus(); });
}
async function aiAnswer(qid, answer, dismissed) {
  const r = await api('/api/ai/answer', { qid, answer, dismissed: !!dismissed });
  if (!r.ok) toast(r.data.error || 'could not answer', true); else { toast(dismissed ? 'Question dismissed' : 'Answer sent to the assistant'); refresh(); }
}
document.addEventListener('click', e => {
  const c = e.target.closest('[data-ai-copy]');
  if (c) {
    const t = document.getElementById(c.dataset.aiCopy).textContent;
    (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(() => toast('Copied')).catch(() => {
      const r = document.createRange(); r.selectNodeContents(document.getElementById(c.dataset.aiCopy)); const s = getSelection(); s.removeAllRanges(); s.addRange(r); toast('Selected: press Cmd+C');
    });
    return;
  }
  const o = e.target.closest('[data-aiq-opt]'); if (o) { aiAnswer(o.dataset.aiqOpt, o.dataset.v, false); return; }
  const a = e.target.closest('[data-aiq-ans]');
  if (a) { const i = document.querySelector(`#aiq input[data-q="${a.dataset.aiqAns}"]`); const v = i ? i.value.trim() : ''; if (!v) { toast('Type an answer, or Dismiss', true); return; } aiAnswer(a.dataset.aiqAns, v, false); return; }
  const d = e.target.closest('[data-aiq-dis]'); if (d) aiAnswer(d.dataset.aiqDis, '', true);
});
document.addEventListener('keydown', e => {
  const i = e.target.closest && e.target.closest('#aiq input[data-q]');
  if (i && e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); const v = i.value.trim(); if (v) aiAnswer(i.dataset.q, v, false); }
}, true);

/* ---- focus: what the operator is looking at, for get_focus */
function aiPostFocus() {
  if (typeof ui === 'undefined' || !S) return;
  const f = { step: ui.step || '', board: ui.board || '', test: ui.test || '', view: ui.step === 'project' ? 'project' : '' };
  const k = JSON.stringify(f);
  if (k === AIC.focusKey) return;
  AIC.focusKey = k; clearTimeout(AIC.focusT);
  AIC.focusT = setTimeout(() => { api('/api/ai/focus', f); }, 250);
}

/* after every render: the panel, the focus, and a light clock for "last request N s ago" (no fetch) */
(function hookRender() {
  if (typeof render !== 'function') return;
  const orig = render;
  render = function () {                                  // eslint-disable-line no-global-assign
    if (S !== AIC.lastS) { AIC.lastS = S; AIC.lastFetch = Date.now(); }      // a new snapshot: last_seen_s is fresh
    orig.apply(this, arguments);
    try { aiQuestionsPaint(); aiPostFocus(); } catch (e) { console.warn('ai_card', e); }
  };
  AIC.tick = setInterval(() => {
    const a = S && S.ai; const st = document.getElementById('aistat'); if (!a || !st) return;
    const seen = document.getElementById('aiseen'); if (seen) seen.textContent = aiSeenText(a);
    if ((st.textContent === 'CONNECTED') !== (a.mode !== 'off' && aiConnected(a)) && typeof render === 'function') render();
  }, 5000);
})();
