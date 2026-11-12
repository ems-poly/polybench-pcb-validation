/* Operator notes: add from anywhere (button, key c, Alt+C), linked to the current context, listed in the Journal tab with a filter,
   marked on the tests, BOM lines and AOI candidates they touch, exported as errata candidates from the Report step.
   Loaded after app.js and uses its globals (S, ui, api, cmd, esc, $, toast, curStep, testInfo, tickSet, boardsTxt, render, renderBottom, refresh). */
const NOTES = (() => {
  const CATS = [['observation', 'Observation'], ['design issue', 'Design issue (next revision)'], ['process', 'Process'], ['question', 'Question']];
  const catLabel = c => (CATS.find(x => x[0] === c) || [0, c])[1];
  const ready = () => !!S && S.notes_v === 2;
  const all = () => (S && S.notes) || [];
  const stepTitle = id => { const s = S.steps.find(x => x.id === id); return s ? s.title.replace(/\s*\(.*\)\s*$/, '') : (id || 'no step'); };
  const nf = () => (ui.nf = ui.nf || { mode: 'all', val: null });

  /* ---- context: what the operator is looking at now */
  function context() {
    const st = curStep(); if (!st) return { step: '', boards: [], test_id: '', line: null, designators: [], aoi_key: '', scan: '' };
    const c = { step: st.id, boards: ui.board ? [ui.board] : [], test_id: '', line: null, designators: [], aoi_key: '', scan: '' };
    if (st.kind === 'assembly' && ui.test === null && !ui.noSel) {
      const l = S.bom.find(x => x.line === ui.line);
      if (l) {
        c.line = l.line; c.designators = l.designators.slice();
        const t = [...tickSet(l)]; if (t.length) c.boards = t;
        const sc = [...(S.scans || [])].reverse().find(x => x.line === l.line); if (sc) c.scan = sc.code || '';
      }
    } else if (ui.test) { const i = testInfo(ui.test); if (i && i.st.id === st.id) c.test_id = ui.test; }
    if (st.kind === 'visual' && ui.vmode === 'review' && S.aoi && S.aoi.run && ui.aoiKey) c.aoi_key = ui.aoiKey;
    return c;
  }
  function linkText(n) {
    const p = [stepTitle(n.step)];
    if (n.line != null) p.push('line ' + n.line); else if (n.lines && n.lines.length) p.push('line ' + n.lines.join(','));
    if (n.test_id) p.push(n.test_id);
    if (n.designators.length) p.push(n.designators.join(' '));
    if (n.boards.length) p.push(boardsTxt(n.boards));
    if (n.aoi_key) p.push('AOI ' + n.aoi_key.split('|').pop().replace('AOI-', ''));
    if (n.scan) p.push('scan ' + n.scan);
    return p.join(' · ');
  }

  /* ---- lookups and markers */
  const forTest = tid => all().filter(n => n.test_id === tid);
  const forLine = ln => all().filter(n => (n.lines || []).includes(ln));
  const forAoi = key => all().filter(n => n.aoi_key === key);
  function mk(kind, val, extra = '') {
    if (!ready()) return '';
    const list = kind === 'test' ? forTest(val) : kind === 'line' ? forLine(val) : forAoi(val);
    if (!list.length) return '';
    const dd = list.some(n => n.category === 'design issue');
    return `<span class="nmk${dd ? ' dd' : ''}${extra}" data-nt="mk" data-kind="${kind}" data-val="${esc(val)}" role="button" tabindex="0" title="${list.length} note${list.length === 1 ? '' : 's'}${dd ? ' (design issue)' : ''}: click to list">&#9998;${list.length}</span>`;
  }

  /* ---- add / edit popover */
  function open(init, editId) {
    if (!ready()) { toast('Notes need the server restarted (this page is newer than the running server)', true); return Promise.resolve(null); }
    const c = init || context();
    const v = { text: '', category: 'observation', suggested_fix: '', ...c, designators: (c.designators || []).slice(), boards: (c.boards || []).slice() };
    if (ui.nd && ui.nd.text && !editId) v.text = ui.nd.text;
    const root = $('#dialog-root');
    const steps = S.steps.map(s => `<option value="${esc(s.id)}" ${s.id === v.step ? 'selected' : ''}>${esc(s.title.replace(/\s*\(.*\)\s*$/, ''))}</option>`).join('');
    root.innerHTML = `<div class="veil"><div class="dlg notedlg" role="dialog" aria-label="${editId ? 'Edit note' : 'Add note'}"><h2>${editId ? 'Edit note ' + editId : 'Add note'}</h2>
      <form id="ndf"><label for="nd-text">Note</label><textarea id="nd-text" name="text" rows="3" placeholder="What did you see, and what should change?"></textarea>
      <div class="ndrow"><div><label for="nd-cat">Category</label><select id="nd-cat">${CATS.map(([k, t]) => `<option value="${k}" ${k === v.category ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
        <div id="nd-fixw" ${v.category === 'design issue' ? '' : 'hidden'}><label for="nd-fix">Suggested fix (optional, one line)</label><input id="nd-fix" autocomplete="off"></div></div>
      <div class="ndlinks"><div class="ndh">Linked to <span class="muted">(edit before saving; designators are checked against the BOM)</span></div>
        <div class="ndgrid"><label for="nd-step">Step</label><select id="nd-step"><option value="">none</option>${steps}</select>
          <label for="nd-boards">Boards</label><input id="nd-boards" autocomplete="off" placeholder="S01 S02">
          <label for="nd-test">Test</label><input id="nd-test" autocomplete="off" placeholder="test id, e.g. V12">
          <label for="nd-line">BOM line</label><input id="nd-line" autocomplete="off" inputmode="numeric" placeholder="line number">
          <label for="nd-des">Designators</label><input id="nd-des" autocomplete="off" placeholder="R3 C4">
          <label for="nd-aoi">AOI candidate</label><input id="nd-aoi" autocomplete="off" placeholder="none">
          <label for="nd-scan">Scan</label><input id="nd-scan" autocomplete="off" placeholder="none"></div></div>
      <div id="nd-err" class="nderr" role="alert"></div>
      <div class="btns"><span class="faint small" style="margin-right:auto">Ctrl+Enter saves, Esc cancels</span><button type="button" data-cancel>Cancel</button><button type="submit" class="primary" id="nd-save">${editId ? 'Save edit' : 'Save note'}</button></div></form></div></div>`;
    const set = (id, val) => { $('#' + id).value = val == null ? '' : val; };
    set('nd-text', v.text); set('nd-fix', v.suggested_fix); set('nd-boards', v.boards.join(' ')); set('nd-test', v.test_id); set('nd-line', v.line);
    set('nd-des', v.designators.join(' ')); set('nd-aoi', v.aoi_key); set('nd-scan', v.scan);
    $('#nd-cat').onchange = e => { $('#nd-fixw').hidden = e.target.value !== 'design issue'; };
    $('#nd-text').focus();
    const prefillAoi = async () => {            // an AOI candidate was open: take its board and designators unless the caller already gave them
      if (!v.aoi_key || editId || v.designators.length) return;
      const r = await api('/api/aoi/item?key=' + encodeURIComponent(v.aoi_key));
      if (r.ok && $('#nd-des') && !$('#nd-des').value) { set('nd-des', (r.data.designators || []).join(' ')); if (r.data.board) set('nd-boards', r.data.board); }
    };
    prefillAoi();
    return new Promise(res => {
      const close = val => { root.innerHTML = ''; res(val); setTimeout(() => ui.afterDialog && ui.afterDialog(), 0); };
      const save = async () => {
        const text = $('#nd-text').value.trim();
        if (!text) { $('#nd-err').textContent = 'A note needs some text.'; $('#nd-text').focus(); return; }
        const body = {
          text, category: $('#nd-cat').value, suggested_fix: $('#nd-fix').value.trim(), step: $('#nd-step').value,
          boards: $('#nd-boards').value.split(/[\s,]+/).filter(Boolean).map(x => x.toUpperCase()), test_id: $('#nd-test').value.trim(),
          line: $('#nd-line').value.trim() || null, designators: $('#nd-des').value.split(/[\s,]+/).filter(Boolean), aoi_key: $('#nd-aoi').value.trim(), scan: $('#nd-scan').value.trim(),
        };
        if (editId) body.ref = editId;
        $('#nd-save').disabled = true;
        const r = await cmd(editId ? 'note_edit' : 'note', body, true);
        if (!r.ok) { $('#nd-save').disabled = false; $('#nd-err').textContent = r.data.error || 'could not save'; return; }
        if (!editId) { ui.nd = null; ui.ncDraft = ''; }
        toast(editId ? 'Note edited (new journal event)' : 'Note saved');
        close(r.data);
      };
      const form = $('#ndf');
      form.addEventListener('submit', e => { e.preventDefault(); save(); });
      form.addEventListener('click', e => { if (e.target.closest('[data-cancel]')) { if (!editId) ui.nd = { text: $('#nd-text').value }; close(null); } });
      root.onkeydown = e => {
        if (e.key === 'Escape') { if (!editId) ui.nd = { text: $('#nd-text').value }; close(null); }
        else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
      };
    });
  }
  const openHere = () => { if (!document.querySelector('.veil')) open(); };

  /* ---- Journal tab: composer, filter, list */
  function matches(n) {
    const f = nf(), c = context();
    switch (f.mode) {
      case 'step': return n.step === (f.val ?? c.step);
      case 'board': return n.boards.includes(f.val ?? ui.board);
      case 'test': { const t = f.val ?? c.test_id; return !!t && n.test_id === t; }
      case 'line': { const l = f.val != null ? +f.val : c.line; return l != null && (n.lines || []).includes(l); }
      case 'des': { const ds = String(f.val ?? c.designators.join(' ')).toUpperCase().split(/[\s,]+/).filter(Boolean); return ds.some(d => n.designators.includes(d)); }
      case 'aoi': return n.aoi_key === f.val;
      default: return true;
    }
  }
  const MODES = [['all', 'All'], ['step', 'This step'], ['board', 'This board'], ['test', 'This test'], ['line', 'This BOM line'], ['des', 'This designator']];
  function listHtml() {
    const rows = all().filter(matches).reverse();
    if (!rows.length) return `<div class="faint">${all().length ? 'No note matches this filter.' : 'No notes yet. Type one above, or press c.'}</div>`;
    return rows.map(n => `<div class="ni cat-${n.category.replace(' ', '-')}"><span class="id">#${n.id}</span><span class="faint">${esc((n.edited_ts || n.ts).slice(5, 16).replace('T', ' '))}</span>
      <span class="ncat">${esc(n.category)}</span><span class="nlk">${esc(linkText(n))}</span>
      <button class="sm ghost" data-nt="edit" data-id="${n.id}">Edit</button>
      <span class="ntx">${esc(n.text)}${n.suggested_fix ? ` <span class="nfix">Fix: ${esc(n.suggested_fix)}</span>` : ''}${n.n_versions > 1 ? ` <span class="faint">(edited ${n.n_versions - 1}x)</span>` : ''}</span>
    </div>`).join('');
  }
  function ctxHtml() {
    const c = context(); if (!c.step) return '';
    const t = [stepTitle(c.step)];
    if (c.line != null) t.push('line ' + c.line); if (c.test_id) t.push(c.test_id);
    if (c.designators.length) t.push(c.designators.join(' ')); if (c.boards.length) t.push(boardsTxt(c.boards)); if (c.aoi_key) t.push('AOI ' + c.aoi_key.split('|').pop().replace('AOI-', ''));
    return 'Linked to: ' + esc(t.join(' · '));
  }
  function fillBottom(b) {
    if (!ready()) { b.innerHTML = '<div class="callout stop"><b>Notes need a server restart.</b> This page is newer than the running Polybench server.</div>'; return; }
    const f = nf(), n = all().length;
    b.innerHTML = `<div class="nj"><div class="njbar"><button class="btab ${ui.jview === 'events' ? '' : 'on'}" data-nt="view" data-v="notes">Notes (${n})</button><button class="btab ${ui.jview === 'events' ? 'on' : ''}" data-nt="view" data-v="events">Events</button>
      ${ui.jview === 'events' ? '' : `<span class="sp"></span><label class="small muted" for="nfm">Show</label><select id="nfm" aria-label="Filter notes">${MODES.map(([k, t]) => `<option value="${k}" ${f.mode === k ? 'selected' : ''}>${t}</option>`).join('')}${f.mode === 'aoi' ? '<option value="aoi" selected>This AOI item</option>' : ''}</select>
      <input id="nfv" class="nfv" autocomplete="off" aria-label="Designators to match" placeholder="R3 C4" ${f.mode === 'des' ? '' : 'hidden'} value="${esc(f.val ?? context().designators.join(' '))}">
      <span class="faint small" id="nfcount"></span>`}</div>
      ${ui.jview === 'events' ? '<div id="evlist"></div>' : `<form class="ncomp" id="ncf"><textarea id="ncq" rows="2" aria-label="New note" placeholder="Add a note (Ctrl+Enter saves). It is linked to what you are looking at."></textarea>
        <div class="ncside"><select id="ncc" aria-label="Category">${CATS.map(([k, t]) => `<option value="${k}" ${k === (ui.ncCat || 'observation') ? 'selected' : ''}>${t}</option>`).join('')}</select>
          <input id="ncfix" autocomplete="off" aria-label="Suggested fix" placeholder="Suggested fix (optional)" ${ui.ncCat === 'design issue' ? '' : 'hidden'}>
          <span class="row"><button type="submit" class="primary sm">Add note</button><button type="button" class="sm" data-nt="more" title="Edit the links before saving">Links&hellip;</button></span></div></form>
        <div class="nctx faint small" id="nctx">${ctxHtml()}</div><div class="nlist" id="nlist">${listHtml()}</div>`}</div>`;
    if (ui.jview === 'events') return;
    $('#ncq').value = ui.ncDraft || ''; $('#ncfix').value = ui.ncFix || '';
    $('#nfcount').textContent = `${all().filter(matches).length} of ${n}`;
  }
  const composerFocused = () => { const a = document.activeElement; return !!a && ['ncq', 'ncfix', 'nfv', 'ncc', 'nfm'].includes(a.id) && ui.bottomTab === 'journal' && ui.jview !== 'events'; };
  function refreshKeepFocus() {
    const l = $('#nlist'); if (l) l.innerHTML = listHtml();
    const c = $('#nctx'); if (c) c.innerHTML = ctxHtml();
    const k = $('#nfcount'); if (k) k.textContent = `${all().filter(matches).length} of ${all().length}`;
  }

  /* ---- Report step: errata export */
  function reportBar() {
    const last = (S.errata_exports || []).slice(-1)[0];
    return `<div class="callout" style="margin:0 0 10px"><b>Errata for the next revision.</b> A collection of all your notes and failure points that will influence your next revision. Give it to your coding agent or merge it into the design's errata.
      <div class="row"><button class="primary" style="white-space:nowrap" data-nt="errata" ${ready() ? '' : 'disabled'}>Export errata candidates</button><span id="errmsg" class="muted">${last ? `last export ${esc(last.ts.slice(0, 16).replace('T', ' '))}: ${last.count} candidate${last.count === 1 ? '' : 's'} in ${esc(S.meta.dir + '/' + last.file)}` : ''}</span></div></div>`;
  }

  /* ---- events */
  document.addEventListener('click', async e => {
    const el = e.target.closest('[data-nt]'); if (!el) return;
    const d = el.dataset;
    if (d.nt === 'mk') {
      e.stopPropagation(); e.preventDefault();
      ui.nf = { mode: d.kind === 'test' ? 'test' : d.kind === 'line' ? 'line' : 'aoi', val: d.val };
      ui.bottomTab = 'journal'; ui.jview = 'notes'; ui.bottomOpen = true; lsSet('bottomOpen', true); renderBottom();
    } else if (d.nt === 'view') { ui.jview = d.v; renderBottom(); }
    else if (d.nt === 'edit') {
      const n = all().find(x => x.id === +d.id); if (!n) return;
      open({ text: n.text, category: n.category, suggested_fix: n.suggested_fix, step: n.step, boards: n.boards, test_id: n.test_id, line: n.line, designators: n.designators, aoi_key: n.aoi_key, scan: n.scan }, n.id);
    } else if (d.nt === 'more') { const q = $('#ncq'), cat = $('#ncc'), fx = $('#ncfix'); const c = context(); c.text = q ? q.value : ''; c.category = cat ? cat.value : 'observation'; c.suggested_fix = fx ? fx.value : ''; ui.ncDraft = ''; open(c); }
    else if (d.nt === 'add') openHere();
    else if (d.nt === 'errata') {
      const r = await api('/api/report/errata', {}); const m = $('#errmsg');
      const t = r.ok ? `Written ${r.data.count} candidate${r.data.count === 1 ? '' : 's'} to ${r.data.path}` : (r.data.error || 'export failed');
      if (m) { m.textContent = t; m.classList.toggle('bad', !r.ok); } if (!r.ok) toast(t, true);
    }
  }, true);
  document.addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.dataset && e.target.dataset.nt === 'mk') { e.preventDefault(); e.target.click(); }
    if (e.target.id === 'ncq' && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('#ncf').requestSubmit(); }
  });
  document.addEventListener('input', e => {
    if (e.target.id === 'ncq') ui.ncDraft = e.target.value;
    else if (e.target.id === 'ncfix') ui.ncFix = e.target.value;
    else if (e.target.id === 'nfv') { nf().val = e.target.value; refreshKeepFocus(); }
  });
  document.addEventListener('change', e => {
    if (e.target.id === 'nfm') { ui.nf = { mode: e.target.value, val: null }; renderBottom(); }
    else if (e.target.id === 'ncc') { ui.ncCat = e.target.value; const fx = $('#ncfix'); if (fx) fx.hidden = e.target.value !== 'design issue'; }
  });
  document.addEventListener('submit', async e => {
    if (e.target.id !== 'ncf') return;
    e.preventDefault();
    const text = $('#ncq').value.trim(); if (!text) { toast('A note needs some text', true); return; }
    const cat = $('#ncc').value, c = context();
    const r = await cmd('note', { ...c, text, category: cat, suggested_fix: $('#ncfix').value.trim() }, true);
    if (!r.ok) { toast(r.data.error || 'could not save the note', true); return; }
    ui.ncDraft = ''; ui.ncFix = ''; toast('Note saved'); renderBottom(); const q = $('#ncq'); q && q.focus();
  });

  /* the AOI review page is an iframe: it reports which candidate is open and receives the note counts */
  window.addEventListener('message', e => {
    if (e.origin !== location.origin || !e.data) return;
    if (e.data.pbCursor) ui.aoiKey = e.data.pbCursor;
    else if (e.data.pbNote) { ui.aoiKey = e.data.pbNote; openHere(); }
    else if (e.data.pbReady) afterRender();
  });
  function afterRender() {
    const f = $('#aoiframe'); if (!f || !f.contentWindow || !ready()) return;
    const counts = {}; all().forEach(n => { if (n.aoi_key) counts[n.aoi_key] = (counts[n.aoi_key] || 0) + 1; });
    f.contentWindow.postMessage({ pbNotes: counts }, location.origin);
  }
  return { open, openHere, context, mk, fillBottom, composerFocused, refreshKeepFocus, reportBar, afterRender, ready };
})();
