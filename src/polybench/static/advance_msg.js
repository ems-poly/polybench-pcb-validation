/* Message for "nothing more to enter" after Enter on the last open cell.
   rows: one entry per cell that is not settled: {b: board, why: 'gate' | 'open'}.
   'gate' = the board's decision gate is closed (its cells cannot be entered yet),
   'open' = enterable (cannot happen when advance() found nothing to jump to, kept for safety). */
function advanceSummary(rows) {
  const boards = why => [...new Set(rows.filter(r => r.why === why).map(r => r.b))];
  const g = boards('gate'), o = boards('open');
  if (!g.length && !o.length) return 'Everything in this step is settled for all boards.';
  const n = (a, one, many) => a.length + ' ' + (a.length === 1 ? one : many);
  const parts = [];
  if (o.length) parts.push(n(o, 'board still has', 'boards still have') + ' open cells');
  if (g.length) parts.push(n(g, 'board waits', 'boards wait') + ' on the decision gate');
  const head = o.length ? 'Open cells remain here' : 'Nothing more to enter here';
  return head + ': ' + parts.join('; ') + '.';
}
if (typeof module !== 'undefined') module.exports = { advanceSummary };
