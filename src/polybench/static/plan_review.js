'use strict';
/* Design step 4, AI plan draft review (docs/AI_CONNECTOR_ARCHITECTURE.md section 5). The assistant writes <plan>.draft.md next to
   the master; this panel shows its summary, lint and a diff against the master, with Approve and Reject. Only the operator
   approves: the server refuses both buttons' routes to the assistant, re-lints, and checks the master did not change. */

const PR = { data: null, key: '', busy: false };

(function prStyle() {
  const css = `
  .prv{border:2px solid var(--accent);border-radius:4px;padding:10px 12px;margin:10px 0 14px;background:var(--panel)}
  .prv h2{margin:0 0 6px;font-size:15px}
  .prv .diff{max-height:420px;overflow:auto;margin:8px 0;border:1px solid var(--border2);background:var(--panel3);font:12px/1.45 var(--mono)}
  .prv .diff div{white-space:pre-wrap;word-break:break-word;padding:0 8px}
  .prv .diff .add{background:var(--pass-bg)} .prv .diff .del{background:var(--fail-bg)} .prv .diff .hunk{color:var(--accent);font-weight:600}
  .prv .diff .hdr{color:var(--muted)}
  .prv ul.lint{margin:4px 0 8px;padding-left:18px;font-size:13px} .prv ul.lint li.error{color:var(--fail)}
  .prv .acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .prnone{margin:6px 0 12px}`;
  const s = document.createElement('style'); s.id = 'pr-style'; s.textContent = css; document.head.appendChild(s);
})();

function prEsc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

PR.wantKey = function () {
  const d = (S && S.ai && S.ai.draft) || {};
  return [d.draft_exists, d.summary, d.errors, d.stale, d.can_approve, S && S.events].join('|');
};

PR.load = async function () {
  if (PR.busy) return;
  PR.busy = true;
  const k = PR.wantKey();
  const r = await api('/api/ai/plan_draft');
  PR.busy = false;
  PR.data = r.ok ? r.data : { error: (r.data && r.data.error) || 'server not reachable' };
  PR.key = k;
  if (S && ui.step === 'testplan') render();
};

PR.diffHtml = function (diff) {
  return diff.split('\n').map(l => {
    const c = l.startsWith('+++') || l.startsWith('---') ? 'hdr' : l.startsWith('@@') ? 'hunk' : l.startsWith('+') ? 'add' : l.startsWith('-') ? 'del' : '';
    return `<div class="${c}">${prEsc(l) || '&nbsp;'}</div>`;
  }).join('');
};

PR.html = function () {
  if (!S || !S.ai) return '';
  if (PR.key !== PR.wantKey() && !PR.busy) { PR.load(); }
  const d = PR.data;
  if (!d) return '<div class="muted small prnone">Checking for an AI draft...</div>';
  if (d.error) return `<div class="muted small prnone">AI draft: ${prEsc(d.error)}</div>`;
  if (!d.configured) return '';
  if (!d.draft_exists) return `<div class="muted small prnone" id="prnone">No AI draft of this plan. An assistant connected on the Project page (card 5) can write one; it appears here for you to approve.</div>`;
  const L = d.lint || { errors: 0, warnings: 0, issues: [] };
  const badge = L.errors ? `<span class="badge bad">${L.errors} lint error${L.errors > 1 ? 's' : ''}</span>` : '<span class="badge ok">lint clean</span>';
  const issues = (L.issues || []).map(i => `<li class="${i.severity}"><span class="mono">line ${i.line}</span> ${i.severity}: ${prEsc(i.msg)}</li>`).join('');
  const why = d.can_approve ? '' : `<div class="callout" style="margin:6px 0"><b>Approve is not available:</b> ${prEsc(d.why_not || '')}</div>`;
  return `<div class="prv" id="prv"><h2>Draft from the AI assistant${d.client ? ' (' + prEsc(d.client) + ')' : ''}</h2>
    <div><b>${prEsc(d.summary || '(no summary)')}</b></div>
    <div class="small muted" style="margin:4px 0"><span class="mono">${prEsc(d.draft_name)}</span> next to <span class="mono">${prEsc(d.master_name)}</span> &middot; draft ${prEsc(d.hash)} &middot; based on ${prEsc(d.base_hash || '?')} &middot; master now ${prEsc(d.master_hash || '?')} &middot; ${prEsc((d.ts || '').replace('T', ' ').slice(0, 19))} ${badge} <span class="muted">${L.warnings} warning${L.warnings === 1 ? '' : 's'}, ${L.tests} tests</span></div>
    ${issues ? `<ul class="lint" id="prlint">${issues}</ul>` : ''}
    ${why}
    <div class="small muted">Changes against the master plan:</div>
    <div class="diff" id="prdiff">${d.diff ? PR.diffHtml(d.diff) : '<div>(no difference)</div>'}</div>
    <div class="acts"><button class="primary" data-pr="approve" ${d.can_approve ? '' : 'disabled'} title="Replace the master plan with this draft and add a revision row">Approve and replace the plan&hellip;</button>
      <button data-pr="reject">Reject&hellip;</button><span class="small muted">Approve writes the revision row, replaces ${prEsc(d.master_name)}, keeps a copy of the old one in the batch folder and journals plan_approved with you as the approver.</span></div></div>`;
};

/* UIR-11: a bar pinned to the bottom of the viewer, so the plan's state and the next action are visible without scrolling. It adds no
   route: Approve is the same button (data-pr="approve", the same dialog and /api/ai/plan_approve) as in the draft panel, and Continue is a
   plain step change. A plan with no AI draft waiting has nothing to approve here; its front-matter status is shown as it is. */
PR.bar = function () {
  const d = PR.data, tp = (typeof TP !== 'undefined' && TP.cache[TP.key()]) || {};
  const setup = S && S.steps && S.steps.find(s => s.kind === 'setup');
  const next = setup ? `<button class="${d && d.draft_exists ? '' : 'primary'}" data-act="step" data-id="${prEsc(setup.id)}" id="prnext">Continue to ${prEsc(setup.title || 'Batch setup')} &rarr;</button>` : '';
  const status = tp.properties && tp.properties.status ? String(tp.properties.status) : '';
  let state, act = '';
  if (d && d.draft_exists) {
    state = `<span class="badge warn">Draft waiting</span> <span class="prb-t">${prEsc(d.summary || '(no summary)')}</span>` + (d.can_approve ? '' : ` <span class="muted">Approve is not available: ${prEsc(d.why_not || '')}</span>`);
    act = `<button class="primary" data-pr="approve" id="prapprove" ${d.can_approve ? '' : 'disabled'} title="Open the approval dialog: replaces the master plan with this draft">Approve test plan&hellip;</button><button data-pr-look="1" title="Scroll up to the draft and its diff">Review draft</button>`;
  } else {
    const ok = /^(approved|current|final)$/i.test(status);
    state = ok ? `<span class="badge ok">Plan approved</span> <span class="prb-t">status: ${prEsc(status)}</span>`
      : `<span class="badge">Plan${status ? ' status: ' + prEsc(status) : ''}</span> <span class="prb-t muted">no AI draft waiting</span>`;
  }
  return `<div class="prbar" id="prbar" role="region" aria-label="Test plan approval"><div class="prb-s">${state}</div><div class="prb-a">${act}${next}</div></div>`;
};

/* Step 4 is drawn by testplan.js; the panel goes right under the file line, above the rendered plan. */
(function hookTp() {
  if (typeof TP === 'undefined' || typeof TP.centre !== 'function') return;
  const orig = TP.centre;
  TP.centre = function (el) {
    orig.call(TP, el);
    const pane = el.querySelector('.pane.tp');
    if (!pane) return;
    const meta = pane.querySelector('.tp-meta');
    const box = document.createElement('div');
    box.id = 'prslot';
    box.innerHTML = PR.html();
    if (meta) meta.after(box); else pane.prepend(box);
    pane.insertAdjacentHTML('beforeend', PR.bar());
  };
})();

document.addEventListener('click', e => {
  const l = e.target.closest('[data-pr-look]'); if (!l) return;
  const t = document.getElementById('prv') || document.getElementById('prslot'), c = document.getElementById('centre');
  if (t && c) c.scrollTop += t.getBoundingClientRect().top - c.getBoundingClientRect().top - 8;
});

document.addEventListener('click', async e => {
  const b = e.target.closest('[data-pr]'); if (!b) return;
  e.preventDefault();
  const d = PR.data; if (!d || !d.draft_exists) return;
  if (b.dataset.pr === 'approve') {
    const ld = d.load || {}, ids = a => (a.length ? a.slice(0, 40).join(', ') + (a.length > 40 ? ` and ${a.length - 40} more` : '') : 'none');
    const regen = ld.regenerates === false ? 'This protocol does not generate its tests from the plan, so no tests change.' : 'The tests will be regenerated from the new plan now.';
    const v = await dialog({ title: 'Approve the AI draft', text: `Replaces ${d.master_name} with the draft (${d.summary}). ${regen} Test IDs added (${(ld.added || []).length}): ${ids(ld.added || [])}. Test IDs retired (${(ld.retired || []).length}): ${ids(ld.retired || [])}; their recorded results stay, marked retired. The revision row and the journal record you as the approver.`, fields: [{ name: 'operator', label: 'Your name', required: true }], ok: 'Approve and replace' });
    if (!v) return;
    const r = await api('/api/ai/plan_approve', { hash: d.hash, operator: v.operator });
    if (r.ok) { toast(`Plan approved: ${r.data.revision}, ${r.data.old_hash} -> ${r.data.new_hash}` + (r.data.reload ? (r.data.reload.ok ? ', tests regenerated' : ', tests NOT regenerated: ' + (r.data.reload.error || '')) : '')); }
    else toast(r.data.error || 'approve failed', true);
  } else {
    const v = await dialog({ title: 'Reject the AI draft', text: 'The draft moves to the batch folder; the assistant reads your reason.', fields: [{ name: 'reason', label: 'Reason', required: true }], ok: 'Reject' });
    if (!v) return;
    const r = await api('/api/ai/plan_reject', { reason: v.reason, operator: (S && S.operator) || '' });
    if (r.ok) toast('Draft rejected'); else toast(r.data.error || 'reject failed', true);
  }
  PR.data = null; PR.key = '';
  if (typeof TP !== 'undefined') { TP.cache = {}; }
  await refresh();
});
