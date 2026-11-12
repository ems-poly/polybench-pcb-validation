/* Polarity card of the Assembly aid: symbol, part, board as inline SVG. Every orientation shown on the board panel comes from
   the card the server built out of design data (Fusion footprint + the BOM's structured marking); this file only draws.
   An amber card has no board panel at all. */
const POL = { cache: {}, sel: {}, W: 200, H: 150 };
const polEsc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const polNum = n => (Math.round(n * 100) / 100);
const polSvg = (inner, label) => `<svg viewBox="0 0 ${POL.W} ${POL.H}" role="img" aria-label="${polEsc(label)}" preserveAspectRatio="xMidYMid meet">${inner}</svg>`;
const T = (x, y, s, cls = '', anchor = 'middle') => `<text x="${x}" y="${y}" text-anchor="${anchor}" class="pt ${cls}">${polEsc(s)}</text>`;

/* ---------------------------------------------------------------- 1. symbol */
function polSymbol(card) {
  const k = card.symbol.kind, pins = card.symbol.pins || [];
  const cathode = card.part && card.part.terminal === 'cathode';
  const pin = n => pins.find(p => p.name === n);
  const padTxt = n => { const p = pin(n); return p && p.pad ? `pad ${p.pad}` : ''; };
  const lbl = (x, y, n, hot) => T(x, y, n, 'big' + (hot ? ' hot' : '')) + T(x, y + 13, padTxt(n), 'sm' + (hot ? ' hot' : ''));
  if (k === 'diode' || k === 'tvs' || k === 'led') {
    let s = `<path class="ln" d="M28 75H70M120 75H172"/><path class="ln fill" d="M70 48V102L120 75Z"/><path class="ln thick hotln" d="M120 46V104${k === 'tvs' ? 'M112 46H120M120 104H128' : ''}"/>`;
    if (k === 'led') s += `<path class="ln" d="M92 40l14-14m-8 0h8v8M106 48l14-14m-8 0h8v8"/>`;
    return polSvg(s + lbl(36, 40, 'A', false) + lbl(164, 40, 'K', true) + T(100, 140, k === 'tvs' ? 'TVS, one direction' : k === 'led' ? 'LED' : 'diode', 'sm'), 'schematic symbol: anode left, cathode bar right');
  }
  if (k === 'led_rgb_ca') {
    let s = `<path class="ln" d="M22 75H52"/><path class="ln thick hotln" d="M52 32V118"/>`;
    [['G', 36, 2], ['B', 75, 3], ['R', 114, 4]].forEach(([n, y]) => { s += `<path class="ln" d="M52 ${y}H70M104 ${y}H170"/><path class="ln fill" d="M70 ${y - 12}V${y + 12}L104 ${y}Z"/><path class="ln" d="M104 ${y - 12}V${y + 12}"/>` + T(160, y - 5, n, 'big', 'end'); });
    return polSvg(s + T(30, 66, 'A', 'big hot') + T(30, 92, 'pad 1', 'sm hot') + T(100, 143, 'common anode, 3 cathodes', 'sm'), 'schematic symbol: RGB LED, common anode pad 1');
  }
  if (k === 'ic') {
    let s = `<rect class="ln fill0" x="62" y="22" width="76" height="104" rx="2"/>`;
    for (let i = 1; i < 4; i++) s += `<path class="ln" d="M40 ${36 + i * 26}H62M138 ${36 + i * 26}H160"/>`;
    s += `<path class="ln" d="M138 36H160"/>`;
    s += `<path class="ln thick hotln" d="M36 36H62"/><circle class="dotf" cx="74" cy="34" r="4"/>` + T(30, 30, '1', 'big hot', 'end') + T(100, 78, 'IC', 'big') + T(100, 142, 'pin 1 = the dotted corner', 'sm');
    return polSvg(s, 'schematic symbol: IC outline with pin 1 marked');
  }
  if (k === 'crystal') {
    const s = `<path class="ln" d="M30 75H80M120 75H170M80 48V102M120 48V102"/><rect class="ln fill0" x="90" y="52" width="20" height="46"/>`;
    return polSvg(s + T(40, 62, '1', 'big hot') + T(40, 90, 'pad 1', 'sm hot') + T(160, 62, '3', 'big') + T(160, 90, 'pad 3', 'sm') + T(100, 128, 'pads 2 and 4 are ground', 'sm') + T(100, 142, '180° turn is harmless', 'sm'), 'schematic symbol: crystal on pads 1 and 3');
  }
  return polSvg(T(100, 78, 'symbol not defined', 'sm'), 'no symbol');
}

/* ---------------------------------------------------------------- 2. part, seen from above */
function polPart(card) {
  const p = card.part || {}, fam = p.family || '', m = p.marking;
  if (m === 'band') {            // two-terminal, band at the cathode end (right)
    const big = fam === 'sma', w = big ? 108 : 84, x0 = 100 - w / 2;
    let s = `<rect class="term" x="${x0 - 14}" y="58" width="20" height="34"/><rect class="term hotf" x="${x0 + w - 6}" y="58" width="20" height="34"/>`;
    s += `<rect class="body" x="${x0}" y="52" width="${w}" height="46" rx="3"/><rect class="band" x="${x0 + w - 22}" y="52" width="14" height="46"/>`;
    s += T(x0 - 6, 118, 'A', 'big') + T(x0 + w + 4, 118, 'K', 'big hot') + T(x0 + w - 15, 40, 'band', 'sm hot') + `<path class="ln hotln" d="M${x0 + w - 15} 44V50"/>`;
    return polSvg(s + T(100, 142, 'top view, ' + (fam || 'two-terminal'), 'sm'), 'part from above: cathode band at the right end');
  }
  if (m === 'triangle') {        // 4-pad LED, black triangle at pad 1
    let s = `<rect class="body" x="62" y="32" width="76" height="76" rx="4"/>`;
    [[62, 32, 1], [138, 32, 4], [62, 108, 2], [138, 108, 3]].forEach(([x, y, n]) => { s += `<rect class="term${n === 1 ? ' hotf' : ''}" x="${x - (x < 100 ? 14 : -2)}" y="${y - (y < 70 ? 10 : -2)}" width="14" height="12"/>` + T(x + (x < 100 ? -21 : 21), y + (y < 70 ? -2 : 12), String(n), n === 1 ? 'sm hot' : 'sm'); });
    s += `<path class="tri" d="M70 40L96 40L70 66Z"/><circle class="lens" cx="108" cy="82" r="14"/>`;
    return polSvg(s + T(100, 132, 'black triangle = pad 1', 'sm hot') + T(100, 146, 'light-emitting face up', 'sm'), 'part from above: black triangle at pad 1');
  }
  if (m === 'chamfer_underside') {   // crystal, 4 pads, chamfer under pad 1
    let s = `<rect class="body metal" x="58" y="38" width="84" height="70" rx="3"/>`;
    [[58, 38, 1], [142, 38, 4], [58, 108, 2], [142, 108, 3]].forEach(([x, y, n]) => { s += `<rect class="term${n === 1 ? ' hotf' : ''}" x="${x - (x < 100 ? 8 : -2)}" y="${y - (y < 70 ? 6 : -2)}" width="12" height="12" opacity=".9"/>` + T(x + (x < 100 ? -18 : 18), y + (y < 70 ? 2 : 12), String(n), n === 1 ? 'sm hot' : 'sm'); });
    s += `<path class="ln hotln dash" d="M64 44l16 0l-16 16Z"/>`;
    return polSvg(s + T(100, 132, 'chamfer on the underside at pad 1', 'sm hot') + T(100, 146, 'not visible from above', 'sm'), 'part from above: pad 1 corner, chamfer on the underside');
  }
  if (m === 'pin1_dot') {        // leadless / gull-wing IC
    const sw = p.corner === 'SW';
    const dy = sw ? 106 : 42;
    let s = `<rect class="body" x="56" y="26" width="88" height="98" rx="3"/>`;
    if (fam === 'soic' || fam === 'tssop') for (let i = 0; i < 4; i++) s += `<rect class="term" x="46" y="${34 + i * 22}" width="10" height="8"/><rect class="term" x="144" y="${34 + i * 22}" width="10" height="8"/>`;
    else for (let i = 0; i < 5; i++) s += `<rect class="term" x="${66 + i * 15}" y="122" width="8" height="6"/><rect class="term" x="${66 + i * 15}" y="22" width="8" height="6"/><rect class="term" x="52" y="${40 + i * 16}" width="6" height="8"/><rect class="term" x="142" y="${40 + i * 16}" width="6" height="8"/>`;
    s += `<circle class="dotf" cx="72" cy="${dy}" r="6"/>`;
    if (p.legend) s += T(106, 80, p.legend, 'big leg');
    return polSvg(s + T(100, 142, (p.legend ? 'marking ' + p.legend + ' upright: ' : '') + 'dot = pin 1', 'sm hot'), 'part from above: pin 1 dot at a corner');
  }
  return polSvg(T(100, 78, 'body marking not defined', 'sm'), 'no part drawing');
}

/* ---------------------------------------------------------------- 3. board */
function polBoard(card) {
  const b = card.board, W = POL.W, H = POL.H, pad = 14, top = 12;
  const flip = b.side === 'bottom';
  const pts = [];
  b.pads.forEach(p => p.poly.forEach(q => pts.push(q))); b.body.forEach(q => pts.push(q));
  b.silk.lines.concat(b.silk.arcs).forEach(l => pts.push(l.p1, l.p2)); b.silk.circles.forEach(c => { pts.push([c.c[0] - c.r, c.c[1] - c.r], [c.c[0] + c.r, c.c[1] + c.r]); });
  const xs = pts.map(q => q[0]), ys = pts.map(q => q[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sc = Math.min((W - 2 * pad) / (x1 - x0 || 1), (H - top - 22) / (y1 - y0 || 1));
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const X = x => polNum(W / 2 + (flip ? -(x - cx) : (x - cx)) * sc), Y = y => polNum(top + (H - top - 16) / 2 - (y - cy) * sc);
  const P = q => `${X(q[0])} ${Y(q[1])}`;
  let s = `<rect class="pcb" x="0" y="0" width="${W}" height="${H}"/>`;
  s += `<polygon class="bodyg" points="${b.body.map(q => X(q[0]) + ',' + Y(q[1])).join(' ')}"/>`;
  b.pads.forEach(p => { s += `<polygon class="pad${p.pol ? ' pol' : ''}" points="${p.poly.map(q => X(q[0]) + ',' + Y(q[1])).join(' ')}"/>`; });
  b.silk.lines.forEach(l => { s += `<path class="silk" d="M${P(l.p1)}L${P(l.p2)}" stroke-width="${polNum(Math.min(3, Math.max(1, l.width * sc)))}"/>`; });
  b.silk.arcs.forEach(a => {
    const d = Math.hypot(a.p2[0] - a.p1[0], a.p2[1] - a.p1[1]) * sc, ang = Math.abs(a.curve) * Math.PI / 360, r = polNum(d / (2 * Math.sin(ang) || 1));
    s += `<path class="silk mark" d="M${P(a.p1)}A${r} ${r} 0 ${Math.abs(a.curve) > 180 ? 1 : 0} ${(a.curve > 0) !== flip ? 1 : 0} ${P(a.p2)}" stroke-width="${polNum(Math.min(3, Math.max(1.4, a.width * sc)))}"/>`;
  });
  b.silk.circles.forEach(c => { s += `<circle class="silkdot" cx="${X(c.c[0])}" cy="${Y(c.c[1])}" r="${polNum(Math.min(4.5, Math.max(2, c.r * sc)))}"/>`; });
  // the body marking on the placed part, at the polarity pad's end
  const mk = b.mark, mx = X(mk.at[0]), my = Y(mk.at[1]);
  if (mk.type === 'band') {
    const ux = (flip ? -1 : 1) * mk.dir[0], uy = -mk.dir[1], len = mk.len * sc, nx = -uy, ny = ux, w = Math.max(5, 0.32 * sc);
    const a = [mx + nx * len / 2, my + ny * len / 2], c = [mx - nx * len / 2, my - ny * len / 2];
    s += `<path class="bandm bk" d="M${polNum(a[0])} ${polNum(a[1])}L${polNum(c[0])} ${polNum(c[1])}" stroke-width="${polNum(w + 3)}"/><path class="bandm" d="M${polNum(a[0])} ${polNum(a[1])}L${polNum(c[0])} ${polNum(c[1])}" stroke-width="${polNum(w)}"/>`;
  } else if (mk.type === 'dot') s += `<circle class="dotm" cx="${mx}" cy="${my}" r="${polNum(Math.min(7, Math.max(3.5, (mk.r || 0.25) * sc)))}"/>`;
  else if (mk.type === 'triangle') { const t = Math.min(26, Math.max(10, 0.34 * sc)), sx = (flip ? -1 : 1) * mk.dir[0], sy = -mk.dir[1];     // right angle at the body corner, legs run inwards
    s += `<path class="trim" d="M${polNum(mx)} ${polNum(my)}L${polNum(mx - sx * t)} ${polNum(my)}L${polNum(mx)} ${polNum(my - sy * t)}Z"/>`; }
  else s += `<circle class="dotm hollow" cx="${mx}" cy="${my}" r="${polNum(Math.min(7, Math.max(3.5, (mk.r || 0.25) * sc)))}"/>`;
  const pp = b.pads.find(p => p.pol), px = pp.poly.reduce((a, q) => a + q[0], 0) / 4, py = pp.poly.reduce((a, q) => a + q[1], 0) / 4;
  const lx = X(px), ly = Y(py), ox = X(b.origin[0]), oy = Y(b.origin[1]);
  const off = Math.max(...pp.poly.map(q => Math.hypot(X(q[0]) - lx, Y(q[1]) - ly))) + 12;
  let vx = lx - ox, vy = ly - oy; const vl = Math.hypot(vx, vy) || 1; vx /= vl; vy /= vl;      // label outside the pad, away from the part centre
  s += T(Math.min(W - 20, Math.max(20, lx + vx * off)), Math.min(H - 20, Math.max(12, ly + vy * (off * 0.8) + 4)), 'pad ' + b.pol_pad, 'sm hot bd');
  s += T(100, H - 5, (flip ? 'bottom side, seen from below' : 'top side') + ' · ' + polNum(b.angle) + '° · Fusion view', 'xs onpcb');
  return polSvg(s, 'board footprint at its real rotation with the polarity pad highlighted');
}

/* ---------------------------------------------------------------- the card */
function polCardInner(card) {
  const amber = card.status === 'amber';
  const chips = (card.designators || []).length > 1 ? `<div class="polchips">${card.designators.map(d => `<button class="sm${d === card.designator ? ' cur' : ''}" data-act="pold" data-d="${polEsc(d)}">${polEsc(d)}</button>`).join('')}</div>` : '';
  const third = amber ? `<div class="polamber" role="alert"><b>${polEsc(card.amber)}</b><span>${polEsc(card.reason || '')}</span></div>` : polBoard(card);
  return `<div class="polhead"><span class="polttl">Polarity <b>${polEsc(card.designator)}</b></span>${chips}</div>
    <div class="polpanels"><figure><figcaption>1 Symbol</figcaption>${polSymbol(card)}</figure><figure><figcaption>2 Part</figcaption>${polPart(card)}</figure>
      <figure class="${amber ? 'amberfig' : ''}"><figcaption>3 Board</figcaption>${third}</figure></div>
    <div class="poltext${amber ? ' isamber' : ''}">${polEsc(card.text)}</div>
    ${card.caution ? `<div class="polcaution">${polEsc(card.caution)}</div>` : ''}
    <div class="polsrc">${polEsc(card.source)}</div>`;
}
function polHtml(line) {            // synchronous, from the cache; the fetch repaints #polslot
  const d = POL.sel[line.line] || line.designators[0], key = line.line + '|' + d, c = POL.cache[key];
  if (!c) { polFetch(line.line, d); return `<div class="polcard loading faint small">Polarity: reading the footprint from the ECAD…</div>`; }
  if (Date.now() - c.at > 20000) polFetch(line.line, d);
  if (c.err) return `<div class="polcard amber"><div class="polamber"><b>${polEsc('Orientation not confirmed from design data, check the silkscreen')}</b><span>${polEsc(c.err)}</span></div></div>`;
  return `<div class="polcard ${c.card.status}">${polCardInner(c.card)}</div>`;
}
async function polFetch(line, d) {
  const key = line + '|' + d; if (POL.busy === key) return; POL.busy = key;
  const r = await api(`/api/polarity?line=${line}&d=${encodeURIComponent(d)}`);
  POL.busy = null;
  POL.cache[key] = r.ok ? { at: Date.now(), card: r.data } : { at: Date.now(), err: (r.data && (r.data.error || r.data.detail)) || 'polarity request failed' };
  const slot = document.getElementById('polslot');
  if (slot && slot.dataset.line === String(line)) { const ln = (S.bom || []).find(l => l.line === line); if (ln) slot.innerHTML = polHtml(ln); }
}
document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('[data-act="pold"]'); if (!b) return;
  const slot = document.getElementById('polslot'); if (!slot) return;
  const line = +slot.dataset.line; POL.sel[line] = b.dataset.d;
  const ln = (S.bom || []).find(l => l.line === line); if (ln) slot.innerHTML = polHtml(ln);
  e.stopPropagation();
}, true);
