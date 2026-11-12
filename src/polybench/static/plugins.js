'use strict';
/* Plugins tab of the bottom panel (one entry, with the count and a status colour). Read-only: it shows what GET /api/plugins reports (imports, package metadata and the
   journal; no instrument, port or ECAD program is contacted). Installing, removing and editing settings are not done here. */
const PLG = (() => {
  let data = null, busy = false, at = 0, open = {};
  const LABEL = { loaded: 'loaded', missing_extra: 'missing extra', missing_library: 'missing library', import_error: 'import error' };

  /* UIR-19: the one Plugins entry of the bottom bar, labelled "Plugins 22". From the summary that rides on /api/state. It turns amber when a
     plugin this protocol uses is not loaded (missing extra or library) and red when one fails to import. The count is every plugin found. */
  function tab(sum, on) {
    if (!sum) return `<button class="btab btab-plugins${on ? ' on' : ''}" data-act="btab" data-id="plugins">Plugins</button>`;
    const n = sum.attention, e = sum.errors || 0, cls = e ? ' bad' : n ? ' warn' : '';
    const tip = `${sum.loaded} of ${sum.total} loaded` + (e ? `, ${e} used by this protocol failed to import` : n ? `, ${n} used by this protocol and not loaded` : '');
    return `<button class="btab btab-plugins${on ? ' on' : ''}${cls}" data-act="btab" data-id="plugins" title="${esc(tip)}">Plugins ${sum.total}${cls ? '<i class="pl-flag" aria-hidden="true"></i><span class="sr">' + (e ? 'error' : 'needs attention') + '</span>' : ''}</button>`;
  }
  const when = ts => ts ? esc(String(ts).replace('T', ' ').slice(0, 19)) : '';
  function lastCell(r) {
    const l = r.last; if (!l) return '<span class="faint">not run</span>';
    const cls = l.state === 'ok' || l.state === 'document selected' ? 'pass' : l.state === 'started' || l.state === 'cancelled' ? 'review' : 'fail';      // journal states: ok, error, cancelled
    return `<span class="vd ${cls}">${esc(l.state)}</span> <span class="muted small">${when(l.ts)}${l.board ? ' &middot; ' + esc(l.board) : ''}</span>${l.text ? `<div class="faint small pl-txt">${esc(l.text)}</div>` : ''}`;
  }
  function statusCell(r) {
    let h = `<span class="pl-st ${esc(r.status)}">${esc(LABEL[r.status] || r.status)}</span>`;
    if (r.status === 'missing_extra' && r.install) h += `<div class="pl-cmd"><code>${esc(r.install.pip)}</code> <button class="sm" data-act="plcopy" data-t="${esc(r.install.pip)}">Copy</button></div><div class="faint small">pipx: <code>${esc(r.install.pipx)}</code></div>`;
    else if (r.detail) h += `<div class="small ${r.status === 'import_error' ? 'pjbad' : 'muted'} pl-txt">${esc(r.detail)}</div>`;
    return h;
  }
  /* JSON with 2-space indent, but an array of plain values stays on one line ([22, 0.15, "mA"]). */
  const pretty = v => JSON.stringify(v, (k, x) => Array.isArray(x) && x.every(e => e === null || typeof e !== 'object') ? '\u0000' + JSON.stringify(x) + '\u0000' : x, 2)
    .replace(/"\\u0000(.*?)\\u0000"/g, (m, a) => JSON.parse('"' + a + '"').replace(/,/g, ', '));
  function settingsRows(r) {
    if (!r.settings.length) return '';
    return `<tr class="pl-set"><td colspan="5">${r.settings.map(s => `<div class="pl-s"><div class="small"><b>${esc(s.where)}</b> <span class="faint">from</span> <span class="mono" title="${esc(s.file)}">${esc(String(s.file).split('/').slice(-2).join('/'))}</span></div><pre>${esc(pretty(s.params))}</pre></div>`).join('')}
      <div class="faint small">Read-only in this release: edit the protocol file to change a setting. Values under key, token, password or secret are hidden.</div></td></tr>`;
  }
  function paint() {
    const b = document.getElementById('bbody'); if (!b || ui.bottomTab !== 'plugins') return;
    if (!data) { b.innerHTML = '<div class="faint">Looking at the installed plugins...</div>'; return; }
    const sm = data.summary, rows = [...data.plugins].sort((a, b) => (b.used - a.used) || (a.kind === 'ecad') - (b.kind === 'ecad') || a.name.localeCompare(b.name));
    b.innerHTML = `<div class="pl-head small muted">${sm.loaded} of ${sm.total} loaded${sm.attention ? `, <b class="pjbad">${sm.attention} used by this protocol and not loaded</b>` : ''}${sm.unavailable_unused ? `, ${sm.unavailable_unused} not used here and unavailable` : ''}.
      ${data.protocol ? 'Protocol: <span class="mono" title="' + esc(data.protocol) + '">' + esc(data.protocol.split('/').slice(-2).join('/')) + '</span>.' : 'No project is open, so no protocol uses them yet.'}
      Installing or removing a plugin is done in a terminal, not here.</div>
      <table class="across pl-tab"><tr><th>Plugin</th><th>Source</th><th>Status</th><th>Used by this protocol</th><th>Last result in this batch</th></tr>
      ${rows.map(r => `<tr class="pl-r ${r.status !== 'loaded' && r.used ? 'attn' : ''}"><td><b class="mono">${esc(r.name)}</b> <span class="pl-kind">${r.kind === 'ecad' ? 'ECAD' : 'action'}</span></td>
        <td class="small">${esc(r.source.label)}</td><td>${statusCell(r)}</td>
        <td>${r.used ? `yes${r.settings.length ? ` <button class="sm" data-act="plset" data-n="${esc(r.kind + ':' + r.name)}">${open[r.kind + ':' + r.name] ? 'Hide settings' : 'Settings'}</button>` : ''}` : '<span class="faint">no</span>'}</td>
        <td>${lastCell(r)}</td></tr>${open[r.kind + ':' + r.name] ? settingsRows(r) : ''}`).join('')}</table>`;
  }
  async function load() {
    if (busy) return; busy = true;
    const r = await api('/api/plugins'); busy = false; at = Date.now();
    if (r.ok) { data = r.data; paint(); }
  }
  /* Called when the tab is drawn: paint what we have, and ask again if it is older than 1.5 s. */
  function fill(b) { paint(); if (Date.now() - at > 1500) load(); }
  function toggle(n) { open[n] = !open[n]; paint(); }
  function reset() { data = null; at = 0; open = {}; }
  return { tab, fill, toggle, load, reset };
})();
