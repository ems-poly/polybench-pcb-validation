/* Design step 4, Test plan: shows the board's test plan markdown, read-only. The server renders the markdown (testplan.py, every source
   character escaped); this file adds a second allow-list pass before the HTML reaches the page. */
const TP_STEP = { id: 'testplan', kind: 'testplan', title: 'Test plan', state: 'na', sections: [], per_board: {}, settled: 0, applicable: 0, fails: 0, retests: 0 };
const TP = { idx: 0, sub: null, cache: {}, busy: false };
const TP_TAGS = new Set('P BR H1 H2 H3 H4 H5 H6 UL OL LI A CODE PRE EM STRONG DEL MARK BLOCKQUOTE HR TABLE THEAD TBODY TR TH TD DIV SPAN'.split(' '));
const tpEsc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

TP.sanitise = function (html) {
  const doc = new DOMParser().parseFromString('<body>' + html + '</body>', 'text/html');       // parsing does not run scripts
  const walk = node => {
    for (const ch of Array.from(node.childNodes)) {
      if (ch.nodeType === 3) continue;
      if (ch.nodeType !== 1 || !TP_TAGS.has(ch.tagName)) { ch.remove(); continue; }
      for (const a of Array.from(ch.attributes)) {
        const n = a.name, v = a.value;
        const ok = n === 'class' || n === 'data-lang' || n === 'data-tp-open' || n === 'rel' || n === 'target' ||
          (n === 'href' && /^(https?:|mailto:|#$)/i.test(v)) || (n === 'style' && /^text-align:(left|right|center)$/.test(v));
        if (!ok) ch.removeAttribute(n);
      }
      walk(ch);
    }
  };
  walk(doc.body);
  return doc.body.innerHTML;
};

TP.files = () => (S && S.test_plan && S.test_plan.files) || [];
TP.key = () => TP.idx + '|' + (TP.sub || '');
TP.load = async function (fresh) {
  const k = TP.key();
  if (TP.busy) return;
  TP.busy = true;
  const q = `/api/testplan?i=${TP.idx}` + (TP.sub ? '&sub=' + encodeURIComponent(TP.sub) : '') + (fresh ? '&t=' + Date.now() : '');
  const r = await api(q);
  TP.busy = false;
  TP.cache[k] = r.ok ? r.data : { configured: true, exists: false, path: '', error: (r.data && r.data.error) || 'server not reachable' };
  if (S && ui.step === 'testplan') render();
};

/* UIR-09 / UIR-10: the plan's ## and ### headings are its contents list. TP.headings reads them from the server's rendered html, in file
   order, so the list is the document's own sections. A heading's key is level + text + its n-th occurrence; the collapsed state is stored
   per plan file under that key. */
TP.headings = function (html) {
  const doc = new DOMParser().parseFromString('<body>' + html + '</body>', 'text/html'), seen = {}, out = [];
  doc.body.querySelectorAll('h2,h3').forEach(h => {
    const level = +h.tagName[1], text = h.textContent.trim().replace(/\s+/g, ' '), b = level + '|' + text;
    seen[b] = (seen[b] || 0) + 1;
    out.push({ level, text, key: b + '|' + seen[b], id: 'tps-' + out.length });
  });
  return out;
};
TP.storeKey = () => 'pb.tp.collapsed:' + ((TP.cache[TP.key()] && TP.cache[TP.key()].path) || TP.key());
TP.collapsed = function () { try { return new Set(JSON.parse(localStorage.getItem(TP.storeKey()) || '[]')); } catch (e) { return new Set(); } };
TP.saveCollapsed = function (set) { try { localStorage.setItem(TP.storeKey(), JSON.stringify([...set])); } catch (e) { /* storage blocked: the state lasts until reload */ } };

/* Wrap each ## / ### heading and everything up to the next heading of the same or a higher level in a section, so a click on the heading
   folds the body. Runs on the sanitised html; the result carries only class, id and data attributes. */
TP.decorate = function (html, heads, collapsed) {
  const doc = new DOMParser().parseFromString('<body>' + html + '</body>', 'text/html'), root = doc.body;
  const stack = [{ level: 1, body: root }];
  let n = 0;
  for (const node of Array.from(root.childNodes)) {
    const lv = node.nodeType === 1 && (node.tagName === 'H2' || node.tagName === 'H3') ? +node.tagName[1] : 0;
    if (lv) {
      while (stack.length > 1 && stack[stack.length - 1].level >= lv) stack.pop();
      const h = heads[n++], sec = doc.createElement('div'), body = doc.createElement('div'), f = collapsed.has(h.key);
      sec.className = 'tp-sec l' + lv + (f ? ' folded' : '');
      sec.dataset.tpsec = h.key; sec.id = h.id;
      const btn = doc.createElement('button');
      btn.className = 'tp-tog'; btn.dataset.tpsecTog = h.key; btn.title = 'Fold or unfold this section'; btn.setAttribute('aria-expanded', f ? 'false' : 'true');
      node.insertBefore(btn, node.firstChild);
      body.className = 'tp-sb';
      sec.append(node, body);
      stack[stack.length - 1].body.appendChild(sec);
      stack.push({ level: lv, body });
    } else stack[stack.length - 1].body.appendChild(node);
  }
  return root.innerHTML;
};

TP.left = function (el, hdr) {
  const fs = TP.files();
  const d = TP.cache[TP.key()], heads = d && d.exists && d.html ? TP.headings(TP.sanitise(d.html)) : [];
  const toc = heads.length ? `<div class="tp-tochd">Contents</div><nav class="tp-toc" aria-label="Contents of the test plan">${heads.map(h =>
    `<a href="#${h.id}" class="tocrow l${h.level}" data-tp-go="${h.id}" title="${tpEsc(h.text)}">${tpEsc(h.text)}</a>`).join('')}</nav>` : '';
  el.innerHTML = hdr('Test plan', '') + (fs.length ? fs.map((f, i) =>
    `<div class="trow${i === TP.idx ? ' cur' : ''}" data-tp="file" data-i="${i}" title="${tpEsc(f.path)}"><span class="id">${f.exists ? '&#10003;' : '&#9675;'}</span><span class="ti">${tpEsc(f.name)}</span></div>`).join('') + toc
    : '<div class="trow"><span class="id"></span><span class="ti faint" style="white-space:normal">No test plan in the protocol</span></div>');
};

/* The entry for the section in view (a heading inside a folded section has no box, so it is skipped): the last heading whose top is at or above the top of the viewer (+ a margin). Runs after each draw
   (the left list is drawn before the centre, so the centre calls it) and on scroll. */
TP.spy = function () {
  const c = document.getElementById('centre'), links = document.querySelectorAll('#left .tocrow');
  if (!c || !links.length) return;
  if (!c.dataset.tpSpy) { c.dataset.tpSpy = '1'; c.addEventListener('scroll', () => TP.spy(), { passive: true }); }
  const top = c.getBoundingClientRect().top + 24;
  let cur = null;
  document.querySelectorAll('#centre .tp-sec').forEach(sec => { const h = sec.firstElementChild; if (h && h.getClientRects().length && h.getBoundingClientRect().top <= top) cur = sec.id; });
  if (!cur) cur = links[0].dataset.tpGo;
  links.forEach(a => a.classList.toggle('cur', a.dataset.tpGo === cur));
};

TP.go = function (id) {
  const t = document.getElementById(id); if (!t) return;
  const set = TP.collapsed();
  let changed = false;
  for (let p = t; p && p.id !== 'centre'; p = p.parentElement) if (p.classList && p.classList.contains('tp-sec') && p.classList.contains('folded')) { p.classList.remove('folded'); set.delete(p.dataset.tpsec); changed = true; }
  if (changed) TP.saveCollapsed(set);
  const c = document.getElementById('centre');
  c.scrollTop += t.getBoundingClientRect().top - c.getBoundingClientRect().top - 8;
  TP.spy();
};

/* Fold or unfold one section (key) or all of them (fold: true / false). */
TP.fold = function (key, fold) {
  const set = TP.collapsed();
  for (const s of document.querySelectorAll('#centre .tp-sec')) {
    if (key && s.dataset.tpsec !== key) continue;
    const f = key ? !s.classList.contains('folded') : fold;
    s.classList.toggle('folded', f);
    const b = s.firstElementChild && s.firstElementChild.querySelector('.tp-tog'); if (b) b.setAttribute('aria-expanded', f ? 'false' : 'true');
    if (f) set.add(s.dataset.tpsec); else set.delete(s.dataset.tpsec);
  }
  TP.saveCollapsed(set);
  TP.spy();
};

/* Step 4 under the rendered plan: what the plan generated, how it covers the BOM, whether it lints clean. All of it comes from the server's
   plan_report (planfmt.py), so the numbers are those of the tests the bench actually runs. */
TP.coverage = function (r) {
  if (!r || !r.coverage) return '';
  const c = r.coverage, L = r.lint || { errors: 0, warnings: 0, issues: [] }, bom = c.bom;
  const lint = L.errors ? `<span class="badge bad">${L.errors} error${L.errors > 1 ? 's' : ''}</span>` : '<span class="badge ok">lint clean</span>';
  const tiers = Object.entries(c.by_tier).map(([k, n]) => `<tr><td>${tpEsc(k)}</td><td class="mono">${n}</td></tr>`).join('');
  const secs = c.sections.map(s => `<tr><td class="mono">${tpEsc(s.step)}</td><td>Tier ${s.tier}</td><td>${tpEsc(s.title)}</td><td class="mono">${tpEsc((s.designators || []).join(' '))}</td><td class="mono">${s.tests}</td></tr>`).join('');
  const miss = bom ? bom.uncovered.map(m => `<span class="dchip sm${m.dnf ? ' dnf' : ''}" title="BOM line ${m.line} ${tpEsc(m.value)}${m.dnf ? ' (not fitted: a test pad or DNF part)' : ''}">${tpEsc(m.designator)}</span>`).join('') : '';
  const issues = (L.issues || []).map(i => `<li class="${i.severity}"><span class="mono">line ${i.line}</span> ${tpEsc(i.msg)}</li>`).join('');
  const hist = (r.history || []).map(h => `<span class="mono">${tpEsc(h.hash)}</span> ${tpEsc(h.ts || '')} (${h.tests} tests)`).join('<br>');
  return `<div class="tp-cov" id="tpcov"><h2>Coverage</h2>
    <div class="tp-covhead"><span>${r.generate ? (r.generated ? 'Tests are <b>generated from this plan</b>' : 'Generation is on, but the plan has errors: the running tests are unchanged') : 'The plan is <b>not generating</b> the tests (<code>generate: true</code> is not set)'}</span>
      <span class="mono">${tpEsc(r.hash || '')}</span>${lint}<span class="sp"></span><button data-tp="regen" title="Read the protocol and the plan again and swap the tests in without a restart">Regenerate tests</button></div>
    <div class="tp-covgrid">
      <div><h3>Tests generated: <span class="mono" id="tpcov-n">${c.tests}</span></h3><table class="across">${tiers}</table></div>
      <div><h3>Designators covered: <span class="mono" id="tpcov-bom">${bom ? bom.covered + ' of ' + bom.total : 'no BOM'}</span></h3>
        ${bom ? (bom.uncovered.length ? `<p class="muted">Not covered by any group, Highlight or probe (${bom.uncovered.length}):</p><div class="tp-miss">${miss}</div>` : '<p class="muted">Every BOM designator is covered.</p>') : ''}</div></div>
    <h3>Sections</h3><table class="across tp-secs"><tr><th>Step</th><th>Tier</th><th>Section</th><th>Covers</th><th>Tests</th></tr>${secs}</table>
    <h3>Lint: ${L.errors} errors, ${L.warnings} warnings</h3>${issues ? `<ul class="tp-lint">${issues}</ul>` : '<p class="muted">No finding.</p>'}
    ${(r.gates || []).length ? `<h3>Decision gate</h3><ul>${r.gates.map(g => `<li>${tpEsc(g.title)}: ${tpEsc(g.rules.join(', '))}</li>`).join('')}</ul>` : ''}
    ${(r.retired || []).length ? `<h3>Retired tests (results kept)</h3><p class="mono">${r.retired.map(x => tpEsc(x.id)).join(' ')}</p>` : ''}
    ${hist ? `<h3>Plan loads</h3><p class="muted">${hist}</p>` : ''}</div>`;
};

TP.centre = function (el) {
  const fs = TP.files();
  const planned = '<div class="tp-planned">Planned: generate this plan for approval from the schematic and layout loaded in step 1.</div>';
  const crumb = '<div class="crumb"><span>Design</span><span class="sep">/</span><span>Test plan</span></div>';
  if (!fs.length) {
    el.innerHTML = `<div class="pane">${crumb}<h1 class="tt">Test plan</h1><div class="empty tp-none">No test plan configured. Add <code>test_plan: &lt;path to markdown&gt;</code> to the top protocol file.</div>${planned}</div>`;
    return;
  }
  if (TP.idx >= fs.length) TP.idx = 0;
  const k = TP.key(), d = TP.cache[k];
  if (!d) { el.innerHTML = `<div class="pane">${crumb}<div class="empty">Loading the test plan...</div></div>`; TP.load(false); return; }
  const tabs = fs.length > 1 ? `<div class="bar2">${fs.map((f, i) => `<button class="${i === TP.idx && !TP.sub ? 'primary' : ''}" data-tp="file" data-i="${i}">${tpEsc(f.name)}</button>`).join('')}</div>` : '';
  const back = TP.sub ? `<button class="sm" data-tp="back">&laquo; ${tpEsc(fs[TP.idx].name)}</button> ` : '';
  const g = d.git;
  const gtxt = g ? (g.commit ? `commit <span class="mono">${tpEsc(g.commit)}</span> ${tpEsc(g.date || '')}${g.subject ? ' &ndash; ' + tpEsc(g.subject) : ''}${g.dirty ? ' <span class="badge bad">edited since commit</span>' : ''}` : '<span class="muted">in a git repo, not committed yet</span>') : '<span class="faint">not in a git repo</span>';
  const meta = `<div class="tp-meta">${back}<span class="mono tp-path" title="${tpEsc(d.path)}">${tpEsc(d.path)}</span>
    ${d.exists ? `<span>modified <b>${tpEsc(d.mtime)}</b></span><span>${gtxt}</span>` : ''}<span class="sp"></span><button data-tp="reload" title="Read the file again">Reload</button></div>`;
  let body;
  if (!d.exists) body = `<div class="empty tp-none"><b>No test plan file yet.</b><br>Expected at <span class="mono">${tpEsc(d.path || (fs[TP.idx] && fs[TP.idx].path) || '')}</span>${d.error ? '<br>' + tpEsc(d.error) : ''}</div>`;
  else {
    const props = d.properties && Object.keys(d.properties).length ? `<div class="tp-props">${Object.entries(d.properties).map(([a, b]) => `<span><b>${tpEsc(a)}</b> ${tpEsc(b)}</span>`).join('')}</div>` : '';
    const clean = TP.sanitise(d.html), heads = TP.headings(clean);
    const ctl = heads.length ? '<div class="tp-fold"><button class="sm" data-tp-all="fold" title="Fold every section">Collapse all</button><button class="sm" data-tp-all="unfold" title="Unfold every section">Expand all</button></div>' : '';
    body = props + ctl + `<div class="tp-doc">${TP.decorate(clean, heads, TP.collapsed())}</div>` + TP.coverage(d.plan);
  }
  el.innerHTML = `<div class="pane tp">${crumb}${tabs}${meta}${body}${planned}</div>`;
  TP.spy();
};

document.addEventListener('click', e => {
  const go = e.target.closest('[data-tp-go]'), all = e.target.closest('[data-tp-all]');
  const hd = e.target.closest('.tp-sec > h2, .tp-sec > h3'), tog = hd && hd.querySelector('.tp-tog');
  if (e.target.closest('.tp-doc a') && !e.target.closest('.tp-tog')) return;                      // a link inside a heading follows the link
  if (go) { e.preventDefault(); TP.go(go.dataset.tpGo); }
  else if (tog) { e.preventDefault(); TP.fold(tog.dataset.tpsecTog); }
  else if (all) { e.preventDefault(); TP.fold(null, all.dataset.tpAll === 'fold'); }
});
document.addEventListener('click', async e => {
  const el = e.target.closest('[data-tp],[data-tp-open]'); if (!el) return;
  e.preventDefault();
  if (el.dataset.tpOpen) { TP.sub = el.dataset.tpOpen; render(); return; }
  switch (el.dataset.tp) {
    case 'reload': delete TP.cache[TP.key()]; TP.load(true); break;
    case 'regen': { const r = await cmd('plan_reload', {}, true); if (r.ok && r.data.ok) { toast('Tests regenerated from the plan' + (r.data.plan_loaded ? ' (plan_loaded journalled)' : '')); } else toast((r.data && r.data.error) || 'could not regenerate', true); delete TP.cache[TP.key()]; TP.load(true); break; }
    case 'file': TP.idx = +el.dataset.i; TP.sub = null; render(); break;
    case 'back': TP.sub = null; render(); break;
  }
});
