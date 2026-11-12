/* AOI in the Visual step: mode bar, photo upload (drag and drop), run, and the embedded issue review.
   Loaded after app.js and uses its globals (S, ui, api, esc, $, toast, startRun, render, refresh). */
const DW = () => (typeof S !== 'undefined' && S.aoi && S.aoi.mode === 'diff') ? 'issue' : 'defect';
const AOI = (() => {
  const mode = () => ui.vmode === 'review' ? 'review' : 'aoi';       // Visual is AOI only (16 Oct): no manual checklist mode
  const MODES = [['aoi', 'AOI photos'], ['review', 'Issue review']];

  function bar() {
    const a = S.aoi, c = a.counts;
    const badge = m => m === 'aoi' ? `<span class="aoi-n">${a.photos.length}</span>` : m === 'review' && c.total ? `<span class="aoi-n${c.decided < c.total ? ' open' : ''}">${c.decided}/${c.total}</span>` : '';
    const open = narrow() ? ui.aoiDrawer : ui.rightOpen;
    return `<div class="bar2 aoibar"><span class="lbl">Visual</span>` + (a.mode === 'diff' ? `<span class="tag" style="background:#b26a00;color:#fff;border-radius:4px;padding:2px 7px;font-size:11px;font-weight:700;text-transform:uppercase;margin-right:6px" title="Layout-free diff mode: experimental, unvalidated">Experimental diff</span>` : '') + MODES.map(([m, t]) =>
      `<button class="bchip${mode() === m ? ' cur' : ''}" data-aoi="mode" data-m="${m}">${t}${badge(m)}</button>`).join('') +
      `<span class="sp"></span><button class="bchip${open ? ' cur' : ''}" data-aoi="ck" title="The AOI issue checklist (right-hand panel)" aria-expanded="${!!open}">Checklist${c.total ? `<span class="aoi-n${c.decided < c.total ? ' open' : ''}">${c.decided}/${c.total}</span>` : ''}</button></div>`;
  }
  const narrow = () => window.matchMedia('(max-width:1099px)').matches;
  const active = st => !!(st && st.kind === 'visual' && S.aoi);

  /* ---- AOI issue checklist: replaces the inspector in the AOI views. Everything comes from S.aoi (the journal state), so it
     is the same count as the review list and the visual test cells. */
  const GROUPS = [['board', 'Board'], ['component', 'Component'], ['kind', 'Kind']];
  const KIND_NAME = { POLSYS: 'Polarity (batch)', POL: 'Polarity', BRIDGE: 'Bridge', MISSING: 'Missing', LIFT: 'Lifted', ANOM: 'Differs from peers', PART: 'Part fail' };
  const STATE = { confirmed: ['Y', 'confirmed, not reworked'], rejected: ['N', 'no fault found (N)'], unsure: ['I', 'ignored (I)'], undecided: ['', 'undecided'] };
  const nat = t => String(t).split(/(\d+)/).map(x => /^\d+$/.test(x) ? x.padStart(6, '0') : x).join('');
  const itName = x => x.manual ? `${x.manual_kind}${x.designators.length ? ' ' + x.designators.join(' ') : ''}` : x.id.replace('AOI-', '');
  const kindOf = x => x.manual ? 'Added by you: ' + x.manual_kind : (KIND_NAME[x.kind] || x.kind);
  const keyOf = (x, g) => g === 'board' ? x.board : g === 'component' ? (x.designator || '(no component)') : kindOf(x);
  function groups(g) {
    const m = new Map();
    S.aoi.items.forEach(x => { const k = keyOf(x, g); if (!m.has(k)) m.set(k, []); m.get(k).push(x); });
    return [...m.entries()].sort((a, b) => nat(a[0]).localeCompare(nat(b[0]))).map(([k, items]) => ({
      k, items: items.sort((x, y) => nat(x.board + itName(x)).localeCompare(nat(y.board + itName(y)))),
      open: items.filter(x => !x.decision).length, confirmed: items.filter(x => x.decision === 'confirmed').length }));
  }
  function checklist() {
    const a = S.aoi, c = a.counts, g = ui.ckGroup || 'board';
    const hdr = `<div class="ph"><span>AOI issue checklist</span><span class="sp"></span><button data-act="toggleRight" title="Collapse" aria-label="Close the checklist">&raquo;</button></div>`;
    if (!a.run) return hdr + `<div class="insp"><p class="muted" id="ckempty">No AOI run yet: issues appear here after Run AOI</p></div>`;
    const gs = groups(g), flat = gs.flatMap(x => x.items);
    const boards = Object.keys(a.per_board).sort((x, y) => nat(x).localeCompare(nat(y)));
    const pct = c.total ? Math.round(100 * c.decided / c.total) : 100;
    let h = hdr + `<div class="insp ckhead"><div class="ckprog"><b id="ckoverall">${c.decided} of ${c.total} decided</b><span class="muted"> &middot; ${c.confirmed} ${DW()}s (${flat.filter(x => x.reworked).length} fixed) &middot; ${c.rejected} no fault &middot; ${c.unsure} ignored</span></div>
      <i class="ckbar"><b style="width:${pct}%"></b></i>
      <div class="ckboards">${boards.map(b => { const p = a.per_board[b]; return `<span class="ckb${p.state === 'done' ? ' done' : ''}${p.unsure ? ' warn' : ''}" data-board="${b}" title="${b}: ${p.confirmed} ${DW()}s (${p.reworked || 0} fixed), ${p.rejected} no fault, ${p.unsure} ignored, ${p.undecided} undecided">${b} ${p.total - p.undecided}/${p.total}${p.unsure ? ` &#9888;${p.unsure}` : ''}</span>`; }).join('')}</div>
      <div class="ckctl"><span class="muted">Group by:</span><span class="seg" role="group" aria-label="Group by">${GROUPS.map(([k, t]) => `<button class="${g === k ? 'on' : ''}" data-ck="group" data-g="${k}" aria-pressed="${g === k}">${t}</button>`).join('')}</span>
        <button class="sm" data-ck="next" ${flat.some(x => !x.decision) ? '' : 'disabled'} title="Next undecided issue in this grouping">Next undecided</button></div></div>`;
    const collapsed = ui.ckCollapsed || {};
    h += `<div class="cklist">` + gs.map(grp => {
      const id = g + ':' + grp.k, shut = collapsed[id];
      return `<div class="ckg" data-grp="${esc(grp.k)}"><button class="ckgh" data-ck="fold" data-id="${esc(id)}" aria-expanded="${!shut}"><i>${shut ? '&#9656;' : '&#9662;'}</i><b>${esc(grp.k)}</b>
        <span class="ckn" title="total, open, confirmed">${grp.items.length} total &middot; ${grp.open} open &middot; ${grp.confirmed} confirmed</span></button>` +
        (shut ? '' : grp.items.map(x => { const st = x.decision || 'undecided', [glyph, rtext] = STATE[st], text = x.reworked ? DW() + ', fixed' : (x.decision === 'confirmed' && S.aoi.mode === 'diff' ? 'issue on ' + (S.aoi.decisions[x.key] || {}).issue_board : rtext), glyph2 = x.reworked ? 'F' : glyph;
          return `<div class="cki${x.key === ui.aoiKey ? ' cur' : ''}" data-ck="go" data-key="${esc(x.key)}" data-state="${st}" tabindex="0" role="button" title="${esc(x.board + ' ' + itName(x))}: ${text}">
            <span class="cks s-${st}" aria-hidden="true">${glyph2 || '&#9675;'}</span><span class="ckt">${g === 'board' ? '' : `<b>${x.board}</b> `}${esc(itName(x))}</span>
            ${x.manual ? '<span class="ckm" title="Found by you; AOI missed it">added by you</span>' : ''}<span class="cksl">${text}</span>${st === 'confirmed' && !x.reworked ? `<button class="sm" data-ck="rework" data-key="${esc(x.key)}" title="The board was fixed on the bench: the issue stops blocking Unpowered">Reworked</button>` : ''}</div>`; }).join('')) + `</div>`;
    }).join('') + `</div>`;
    return h;
  }
  function jump(key) {
    ui.aoiKey = key; if (narrow()) ui.aoiDrawer = false;
    const f = document.getElementById('aoiframe');
    if (mode() === 'review' && f && f.contentWindow) { f.contentWindow.postMessage({ pbGoto: key }, location.origin); render(); }
    else { ui.vmode = 'review'; ui.aoiGoto = key; render(); }
  }
  function ckClick(el) {
    const d = el.dataset;
    if (d.ck === 'group') { ui.ckGroup = d.g; lsSet('ckGroup', d.g); renderRight(); }
    else if (d.ck === 'fold') { ui.ckCollapsed = { ...(ui.ckCollapsed || {}), [d.id]: !(ui.ckCollapsed || {})[d.id] }; renderRight(); }
    else if (d.ck === 'go') jump(d.key);
    else if (d.ck === 'rework') {
      const it = S.aoi.items.find(x => x.key === d.key);
      dialog({ title: 'Mark ' + (it ? it.board + ' ' + itName(it) : 'the issue') + ' reworked', text: 'The issue stays recorded as confirmed; it stops blocking Unpowered for this board.', fields: [{ name: 'note', label: 'Note (optional)' }], ok: 'Reworked' })
        .then(v => v && cmd('aoi_rework', { key: d.key, note: v.note }));
    }
    else if (d.ck === 'next') {
      const flat = groups(ui.ckGroup || 'board').flatMap(x => x.items), at = flat.findIndex(x => x.key === ui.aoiKey);
      const n = flat.length, nx = [...Array(n).keys()].map(i => flat[(at + 1 + i) % n]).find(x => !x.decision);
      if (nx) jump(nx.key);
    }
  }
  document.addEventListener('keydown', e => { const el = e.target.closest && e.target.closest('.cki'); if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); ckClick(el); } });
  window.addEventListener('message', e => {
    if (e.origin !== location.origin || !e.data) return;
    if (e.data.pbCursor && S) { ui.aoiKey = e.data.pbCursor; if (active(curStep())) renderRight(); }
    if (e.data.pbReady && ui.aoiGoto) { const f = document.getElementById('aoiframe'); if (f && f.contentWindow) f.contentWindow.postMessage({ pbGoto: ui.aoiGoto }, location.origin); ui.aoiGoto = null; }
  });

  /* Left panel of the Visual step: the AOI section and the per-board AOI progress. Nothing else (the manual checklist is archived). */
  const LIVE_NOTE = 'Capture a frame from a USB camera or microscope and run AOI on it; later a live overlay. Roadmap T6.5.';
  const liveBtn = () => `<button class="aoi-live" disabled aria-disabled="true" title="${esc(LIVE_NOTE)}">Live capture <span class="tag soon">Coming (V2)</span></button>`;
  function left(st) {
    const sec = (st.sections || []).find(s => s.id === 'aoi');
    const a = S.aoi, pb = a.per_board || {};
    let h = `<div class="sec aoisec"><div class="sec-h"><span class="sh-t">${esc(sec ? sec.title : 'Automated optical inspection (AOI)')}</span></div>`;
    h += `<div class="sec-a">` + ((sec && sec.actions) || []).filter(x => ui.advanced || !x.advanced).map(x =>
      `<button data-act="run" data-sec="${esc(sec.id)}" data-action="${esc(x.id)}" ${S.action ? 'disabled' : ''} title="Run on ${esc(ui.board)}: ${esc(x.plugin)}"><span class="play">&#9654;</span>${esc(x.title)}</button>`).join('') + liveBtn() + `</div>`;
    h += `<div class="muted aoi-livenote" id="aoilivenote">${esc(LIVE_NOTE)}</div></div>`;
    h += `<div class="sec"><div class="sec-h"><span class="sh-t">Boards</span><span class="c">${a.counts.decided}/${a.counts.total} decided</span></div>`;
    h += S.boards.map(b => {
      const p = pb[b.label] || {}, ps = st.per_board[b.label] || {}, ai = ps.aoi || {};
      const state = ps.state || 'empty';
      let txt;
      if (!ai.photo) txt = '<span class="faint">no photo</span>';
      else if (!ai.run) txt = '<span class="faint">photo, not run yet</span>';
      else txt = `${ai.decided}/${ai.total} decided` + (ai.confirmed ? ` &middot; <b class="bad">${ai.confirmed} ${DW()}${ai.confirmed === 1 ? '' : 's'}${ai.reworked ? ` (${ai.reworked} fixed)` : ''}</b>` : '') + (ai.unsure ? ` &middot; <span class="tag pol" title="Ignored does not block, it is listed in the report">&#9888; ${ai.unsure} ignored</span>` : '');
      const dot = state === 'done' ? 'pass' : state === 'attention' ? 'fail' : state === 'partial' ? 'proposed' : 'empty';
      return `<div class="trow aoib${b.label === ui.board ? ' cur' : ''}" data-act="board" data-id="${b.label}" data-state="${state}" title="${esc(ai.reason || 'AOI review complete')}"><span class="id">${b.label}</span><span class="ti">${txt}</span><span class="dots"><i class="dot s-${dot}"></i></span></div>`;
    }).join('') + `</div>`;
    return h;
  }

  const relOf = p => p.file.replace(/^aoi\/photos\//, '');
  const thumbUrl = (p, w = 120) => `/aoi/photo/${encodeURI(relOf(p))}?thumb=${w}`;
  const usedBadge = (p, t = 'AOI used') => (ui.aoiUsed || []).includes(p.file) ? `<span class="aoi-used" title="The sharpest photo of this board and side: this one is analysed by the next run">${t}</span>` : '';

  function photoRow(p) {
    const labels = S.boards.map(b => `<option value="${b.label}" ${b.label === p.board ? 'selected' : ''}>${b.label}</option>`).join('');
    return `<div class="aoi-ph${p.board ? '' : ' un'}" data-phfile="${esc(p.file)}"><span class="thw"><img loading="lazy" draggable="true" data-aoi-file="${esc(p.file)}" data-aoi="view" data-file="${esc(p.file)}" title="Click to view full size. Drag onto a board tile to move this photo." src="${thumbUrl(p, 240)}" alt="${esc(p.name)}">${usedBadge(p)}</span>
      <div class="aoi-meta"><div class="nm" title="${esc(p.name)}">${esc(p.name)}</div>
      <div class="aoi-sel"><select data-aoi="board" data-file="${esc(p.file)}" aria-label="Board">${p.board ? '' : '<option value="">board?</option>'}${labels}</select>
      <select data-aoi="side" data-file="${esc(p.file)}" aria-label="Side"><option ${p.side === 'top' ? 'selected' : ''}>top</option><option ${p.side === 'bottom' ? 'selected' : ''}>bottom</option></select>
      <button class="sm" data-aoi="remove" data-file="${esc(p.file)}" title="Remove this photo from the batch (the file is kept in aoi/removed)">Remove</button></div></div></div>`;
  }

  // a tile shows up to three thumbnails (the photo AOI will use first), then "+n"
  function tile(b) {
    const mine = S.aoi.photos.filter(p => p.board === b.label).sort((x, y) => ((ui.aoiUsed || []).includes(y.file) - (ui.aoiUsed || []).includes(x.file)) || (x.side === 'top' ? -1 : 1));
    const th = mine.slice(0, 3).map(p => `<span class="thw"><img draggable="true" data-aoi-file="${esc(p.file)}" data-aoi="view" data-file="${esc(p.file)}" alt="${esc(p.name)}" title="${esc(p.name)}: click to view, drag to another tile to move" src="${thumbUrl(p)}">${usedBadge(p, 'used')}</span>`).join('');
    return `<div class="aoi-tile${mine.length ? ' has' : ''}" data-tile="${b.label}" tabindex="0" role="button" aria-label="${b.label}: ${mine.length} photo${mine.length === 1 ? '' : 's'}. Drop photos here or press to choose files"
      title="Drop photos on ${b.label}, or click to choose files for ${b.label}"><b>${b.label}</b><span class="n">${mine.length} photo${mine.length === 1 ? '' : 's'}</span>
      <div class="tth">${th || '<span class="phold">+</span>'}${mine.length > 3 ? `<span class="more">+${mine.length - 3}</span>` : ''}</div></div>`;
  }

  function viewPhotos(el, st) {
    const a = S.aoi, run = a.run, busy = !!S.action;
    const unassigned = a.photos.filter(p => !p.board).length;
    const un = a.photos.filter(p => !p.board);
    const unCard = un.length ? `<div class="aoi-board unassigned"><div class="hd"><b>No board</b><span class="muted">${un.length} photo${un.length === 1 ? '' : 's'}</span></div>${un.map(photoRow).join('')}</div>` : '';
    const perBoard = unCard + S.boards.map(b => {
      const mine = a.photos.filter(p => p.board === b.label);
      const pb = a.per_board[b.label];
      return `<div class="aoi-board"><div class="hd"><b>${b.label}</b><span class="muted">${mine.length} photo${mine.length === 1 ? '' : 's'}</span>${pb ? `<span class="muted">${pb.total} issue${pb.total === 1 ? '' : 's'}, ${pb.undecided} open</span>` : ''}</div>${mine.map(photoRow).join('') || '<div class="faint">no photo</div>'}</div>`;
    }).join('');
    el.innerHTML = bar() + `<div class="pane"><div class="crumb"><span>${esc(st.title)}</span><span class="sep">/</span><span>AOI photos</span></div>
      <h1 class="tt">Photos and AOI run</h1>
      <div id="aoidrop" class="aoi-drop" tabindex="0">Drop board photos here, or <label class="lnk"><input type="file" id="aoifile" multiple accept=".heic,.heif,.jpg,.jpeg,.png" hidden>choose files</label>.
        <div class="muted">Several per board are fine; the sharpest top photo of each board is analysed. Here the board is read from the file name (S03, B3); otherwise choose it below.</div></div>
      <div class="aoi-tilehd muted">Or assign to a board directly: drop photos on its tile, or click the tile to choose files. The file name is then ignored.</div>
      <div class="aoi-tiles" id="aoitiles">${S.boards.map(tile).join('')}</div>
      <div id="aoiup" class="muted" aria-live="polite">${(ui.aoiLog || []).map(l => `<div class="${l.bad ? 'bad' : ''}">${esc(l.t)}</div>`).join('')}</div>
      <div class="row aoi-run"><button class="primary" data-aoi="run" ${busy || !a.photos.length && !S.meta.simulate ? 'disabled' : ''}>&#9654; Run AOI on ${a.photos.filter(p => p.board).length} photo${a.photos.filter(p => p.board).length === 1 ? '' : 's'}</button>
        ${liveBtn()}
        <button data-aoi="mode" data-m="review" ${run ? '' : 'disabled'}>Open issue review${a.counts.total ? ` (${a.counts.decided}/${a.counts.total})` : ''}</button>
        <span class="muted">${busy ? 'running: see the Console below' : run ? `last run ${esc(run.ts)}: ${a.counts.total} issues${run.synthetic ? ' &middot; SIMULATED, no photos analysed' : ''}` : 'no run yet'}</span></div>
      ${run && (run.notes || []).length ? run.notes.map(n => `<div class="callout stop"><b>${esc(n)}</b></div>`).join('') : ''}
      ${unassigned ? `<div class="callout"><b>${unassigned} photo${unassigned === 1 ? ' has' : 's have'} no board.</b> Choose one in the list; unassigned photos are never used.</div>` : ''}
      <div class="aoi-grid" id="aoigrid">${perBoard}</div>
      <div class="callout" style="margin-top:12px">${S.aoi.mode === 'diff' ? 'EXPERIMENTAL diff mode, no layout: AOI lists regions where a board differs from the reference board. Two photos cannot say which board is wrong; you decide Issue (Y) / No fault (N) / Unsure (U) and name the board. It cannot see polarity, missing parts or bridges as such. Nothing becomes a PASS because AOI found nothing.' : 'AOI is review assistance. It finds candidates (bridges, missing or lifted parts, parts that differ from the other boards, polarity) and your Defect - fixed (F) / No fault found (N) / Ignore (I) decides. It cannot show that a joint, a bridge or a polarity is good, so nothing becomes a PASS because AOI found nothing.'}</div></div>`;
    wire();
    loadUsed();
  }

  function viewReview(el, st) {
    const run = S.aoi.run, f = document.getElementById('aoiframe'), b = el.querySelector('.aoibar');
    if (run && f && b && el.contains(f)) {          // keep the review page (and its open board view); it re-reads the state itself
      b.outerHTML = bar();
      if (f.contentWindow) f.contentWindow.postMessage({ pbSync: 1 }, location.origin);
      return;
    }
    el.innerHTML = bar() + (run ? `<iframe class="aoi-frame" id="aoiframe" src="/aoi/review/" title="AOI issue review"></iframe>`
      : `<div class="pane"><div class="empty">No AOI run yet. Upload photos and run AOI first.</div></div>`);
  }

  function centre(el, st) { return mode() === 'review' ? viewReview(el, st) : viewPhotos(el, st); }

  // which photo each board and side will be analysed from: asked of the server (same choice as the run) when the photo set changes
  async function loadUsed() {
    const sig = S.aoi.photos.map(p => p.file + '|' + p.board + '|' + p.side).sort().join(';');
    if (ui.aoiUsedSig === sig) return;
    ui.aoiUsedSig = sig;
    try {
      const r = await fetch('/api/aoi/used'); if (!r.ok) return;
      const u = (await r.json()).used;
      if (JSON.stringify(u) !== JSON.stringify(ui.aoiUsed || []) && ui.aoiUsedSig === sig) { ui.aoiUsed = u; if (mode() === 'aoi') render(); }
    } catch (e) { ui.aoiUsedSig = null; }
  }

  function overlay(id, html) {
    closeOverlay(id);
    const o = document.createElement('div'); o.id = id; o.className = 'aoi-ov'; o.innerHTML = html; document.body.appendChild(o);
    o.addEventListener('click', e => { if (e.target === o) closeOverlay(id); });
    return o;
  }
  const closeOverlay = id => { const o = document.getElementById(id); if (o) o.remove(); };

  // full-size viewer: a JPEG preview from the server (HEIC included), arrows move through the batch's photos
  let viewIdx = -1;
  const ordered = () => [...S.aoi.photos].sort((x, y) => (x.board || '~').localeCompare(y.board || '~') || x.name.localeCompare(y.name));
  function viewer(file) {
    const list = ordered(); viewIdx = list.findIndex(p => p.file === file); const p = list[viewIdx]; if (!p) return closeOverlay('aoiview');
    const o = overlay('aoiview', `<div class="aoi-vw" role="dialog" aria-label="Photo viewer"><div class="vh"><b>${esc(p.board || 'no board')}</b> <span class="muted">${esc(p.side)} &middot; ${esc(p.name)}</span>
      ${(ui.aoiUsed || []).includes(p.file) ? '<span class="aoi-used">AOI used</span>' : ''}<span class="sp"></span>
      <button class="sm" data-aoi="vprev" ${viewIdx ? '' : 'disabled'} aria-label="Previous photo">&larr;</button><button class="sm" data-aoi="vnext" ${viewIdx < list.length - 1 ? '' : 'disabled'} aria-label="Next photo">&rarr;</button>
      <a class="sm" href="/aoi/photo/${encodeURI(relOf(p))}" target="_blank" rel="noopener" title="The original file">original</a>
      <button class="sm" data-aoi="remove" data-file="${esc(p.file)}">Remove</button><button class="sm" data-aoi="vclose" aria-label="Close">&times;</button></div>
      <div class="vb"><img id="aoiviewimg" src="${thumbUrl(p, 1800)}" alt="${esc(p.name)}"></div></div>`);
    return o;
  }

  function confirmRemove(file) {
    const p = S.aoi.photos.find(x => x.file === file); if (!p) return;
    const o = overlay('aoiconfirm', `<div class="aoi-vw aoi-cf" role="alertdialog" aria-label="Remove photo"><p><b>Remove ${esc(p.name)}</b> from ${esc(p.board || 'the batch')}?</p>
      <p class="muted">The file is moved to aoi/removed/ in the batch folder, not deleted. The next AOI run does not use it. The removal is recorded in the journal.</p>
      <input id="aoirmwhy" type="text" placeholder="Reason (optional), e.g. blurred, wrong board" maxlength="200" style="width:100%">
      <div class="row" style="margin-top:10px;gap:8px"><button class="primary" id="aoirmok">Remove photo</button><button id="aoirmno">Keep</button></div></div>`);
    const why = $('#aoirmwhy'); why.focus();
    const go = async () => {
      const r = await api('/api/aoi/remove', { file, reason: why.value });
      closeOverlay('aoiconfirm'); closeOverlay('aoiview');
      if (!r.ok) log(r.data.error || 'remove failed', true); else log(`Removed ${p.name} from ${p.board || 'the batch'}${why.value.trim() ? ' (' + why.value.trim() + ')' : ''}; file kept in aoi/removed`);
      await refresh();
    };
    $('#aoirmok').onclick = go; $('#aoirmno').onclick = () => closeOverlay('aoiconfirm');
    why.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { if (document.getElementById('aoiconfirm')) closeOverlay('aoiconfirm'); else closeOverlay('aoiview'); }
    else if (document.getElementById('aoiview') && !document.getElementById('aoiconfirm') && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      const l = ordered(), n = l[viewIdx + (e.key === 'ArrowLeft' ? -1 : 1)]; if (n) viewer(n.file);
    }
  });

  const HOW = { tile: 'dropped on tile', click: 'chosen for tile', filename: 'from file name', move: 'moved' };
  function log(t, bad) { ui.aoiLog = [...(ui.aoiLog || []), { t, bad: !!bad }].slice(-8); const b = $('#aoiup'); if (b) b.innerHTML = ui.aoiLog.map(l => `<div class="${l.bad ? 'bad' : ''}">${esc(l.t)}</div>`).join(''); }

  // board: set when the files were dropped on, or chosen for, a tile; how: 'tile' | 'click' | undefined (general area)
  async function upload(files, board, how) {
    let n = 0;
    for (const f of files) {
      const box = $('#aoiup'); if (box) box.insertAdjacentHTML('beforeend', `<div class="busy">uploading ${++n} of ${files.length}: ${esc(f.name)}</div>`);
      try {
        const r = await fetch(`/api/aoi/photos?name=${encodeURIComponent(f.name)}${board ? '&board=' + encodeURIComponent(board) : ''}`, { method: 'POST', body: f });
        const j = await r.json();
        if (!r.ok) log(`${f.name}: ${j.error || r.status}`, true);
        else if (board) log(`Assigned to ${j.board} (${HOW[how] || HOW.tile})${j.moved_from ? `, was ${j.moved_from}` : j.duplicate ? ', already uploaded' : ''}: ${f.name}`);
        else if (j.board) log(`Assigned to ${j.board} (${HOW.filename})${j.duplicate ? ', already uploaded' : ''}: ${f.name}`);
        else log(`${f.name}: no board in the file name${j.duplicate ? ' (already uploaded)' : ''}. Choose a board in the list, or drop it on a tile`, !j.duplicate);
      } catch (e) { log(f.name + ': server not reachable', true); }
    }
    await refresh();
  }

  async function move(file, board) {
    const r = await api('/api/aoi/move', { file, board });
    if (!r.ok) log(r.data.error || 'move failed', true);
    else { const p = S.aoi.photos.find(x => x.file === file); log(r.data.moved ? `Assigned to ${board} (moved from ${r.data.from || 'no board'}): ${p ? p.name : file}` : `Already on ${board}`); }
    await refresh();
  }

  const isFiles = e => [...(e.dataTransfer ? e.dataTransfer.types : [])].includes('Files');
  const isPhoto = e => [...(e.dataTransfer ? e.dataTransfer.types : [])].includes('application/x-aoi-photo');

  function pick(board) {
    const i = document.createElement('input'); i.type = 'file'; i.multiple = true; i.accept = '.heic,.heif,.jpg,.jpeg,.png'; i.style.display = 'none';
    document.body.appendChild(i);       // created here, not in the view: a state broadcast re-renders the view while the chooser is open
    i.addEventListener('change', () => { const fs = [...i.files]; i.remove(); if (fs.length) upload(fs, board, 'click'); });
    i.addEventListener('cancel', () => i.remove());
    i.click();
  }

  function wireTiles() {
    document.querySelectorAll('.aoi-tile').forEach(t => {
      const board = t.dataset.tile;
      t.addEventListener('dragover', e => { if (isFiles(e) || isPhoto(e)) { e.preventDefault(); e.dataTransfer.dropEffect = isPhoto(e) ? 'move' : 'copy'; t.classList.add('over'); } });
      t.addEventListener('dragenter', e => { if (isFiles(e) || isPhoto(e)) { e.preventDefault(); t.classList.add('over'); } });
      t.addEventListener('dragleave', e => { if (!t.contains(e.relatedTarget)) t.classList.remove('over'); });
      t.addEventListener('drop', e => {
        e.preventDefault(); e.stopPropagation(); t.classList.remove('over');
        const file = e.dataTransfer.getData('application/x-aoi-photo');
        if (file) move(file, board); else if (e.dataTransfer.files.length) upload([...e.dataTransfer.files], board, 'tile');
      });
      t.addEventListener('click', e => { if (!e.target.closest('[data-aoi="view"]')) pick(board); });
      t.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(board); } });
    });
  }

  document.addEventListener('dragstart', e => {
    const im = e.target.closest && e.target.closest('img[data-aoi-file]'); if (!im) return;
    e.dataTransfer.setData('application/x-aoi-photo', im.dataset.aoiFile); e.dataTransfer.effectAllowed = 'move';
    document.body.classList.add('aoi-dragging');
  });
  document.addEventListener('dragend', () => document.body.classList.remove('aoi-dragging'));

  function wire() {
    wireTiles();
    const z = $('#aoidrop'); if (!z) return;
    ['dragenter', 'dragover'].forEach(t => z.addEventListener(t, e => { e.preventDefault(); z.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => z.addEventListener(t, e => { e.preventDefault(); z.classList.remove('over'); }));
    z.addEventListener('drop', e => upload([...e.dataTransfer.files]));
    const f = $('#aoifile'); if (f) f.addEventListener('change', () => { const fs = [...f.files]; f.value = ''; upload(fs); });
  }

  document.addEventListener('click', async e => {
    const ck = e.target.closest('[data-ck]'); if (ck) { ckClick(ck); return; }
    const el = e.target.closest('[data-aoi]'); if (!el) return;
    const d = el.dataset;
    if (d.aoi === 'ck') { if (narrow()) ui.aoiDrawer = !ui.aoiDrawer; else { ui.rightOpen = !ui.rightOpen; lsSet('rightOpen', ui.rightOpen); } render(); }
    else if (d.aoi === 'mode') { ui.vmode = d.m; render(); }
    else if (d.aoi === 'run') {
      const sec = curStep().sections.find(s => (s.actions || []).some(a => a.id === 'aoi_run'));
      if (sec) await startRun(ui.step, sec.id, 'aoi_run', ui.board, false);
    } else if (d.aoi === 'view') { e.stopPropagation(); viewer(d.file); }
    else if (d.aoi === 'remove') confirmRemove(d.file);
    else if (d.aoi === 'vclose') closeOverlay('aoiview');
    else if (d.aoi === 'vprev' || d.aoi === 'vnext') { const l = ordered(), n = l[viewIdx + (d.aoi === 'vprev' ? -1 : 1)]; if (n) viewer(n.file); }
  });
  document.addEventListener('change', async e => {
    const el = e.target.closest('select[data-aoi]'); if (!el) return;
    const d = el.dataset, p = S.aoi.photos.find(x => x.file === d.file); if (!p) return;
    const body = { file: d.file, board: d.aoi === 'board' ? el.value : p.board, side: d.aoi === 'side' ? el.value : p.side };
    const r = await api('/api/aoi/assign', body); if (!r.ok) toast(r.data.error, true); await refresh();
  });
  // the review page is an iframe: its decisions arrive through the server's state broadcast, which refreshes S
  return { bar, centre, checklist, active, narrow, left };
})();
