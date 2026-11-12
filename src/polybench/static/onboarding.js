'use strict';
/* Onboarding copy and the stage timeline. Edit the text in PHASES below; nothing else in the app holds it.
   The stage names and their order are the 12 steps of docs/PRODUCT_SPEC.md (section 4) and the step bar of the app.
   status: '' (built), 'preview' (built, not yet used on a real batch) or 'unbuilt' (planned, not in this release). No performance claims here. */
const ONB = (() => {
  const PHASES = [
    { id: 'design', name: 'Design', sub: 'Before any board exists', stages: [
      { name: 'Project', gets: 'One folder for the design, its BOM and the ECAD link.' },
      { name: 'Schematic analysis', gets: 'Review the schematic before boards are built.', status: 'unbuilt' },
      { name: 'BOM review', gets: 'Check the BOM against the design.', status: 'unbuilt' },
      { name: 'Test plan', gets: 'The plan the bench steps run, approved by you.', status: 'preview' },
    ] },
    { id: 'bench', name: 'Bench', sub: 'With the built boards', stages: [
      { name: 'Batch setup', gets: 'The boards in this batch and their serial numbers.' },
      { name: 'Assembly aid', gets: 'Place parts one line at a time, highlighted in your ECAD program.' },
      { name: 'Visual', gets: 'Inspect each board and decide on every spot the checker lists.' },
      { name: 'Unpowered', gets: 'Resistance and continuity checks before first power.' },
      { name: 'Powered', gets: 'Supply and rails only. Exit: the board is safe to program.' },
      { name: 'Program and function', gets: 'Program it, then prove each function, buses and current per mode.' },
      { name: 'Characterisation', gets: 'Guided captures for one board, reduced to statistics.' },
      { name: 'Report', gets: 'Results, notes and findings as Markdown and HTML.' },
    ] },
  ];
  const TAG = { preview: ['Preview', 'Built, not yet used on a real batch'], unbuilt: ['Not built yet', 'Planned, not in this release'] };

  /* The timeline: two phases, one line per stage. `esc` is the app's HTML escaper. */
  function timeline(esc) {
    let n = 0;
    return `<ol class="onb-tl">${PHASES.map(ph => `<li class="onb-phase"><div class="onb-ph"><b>${esc(ph.name)}</b><span>${esc(ph.sub)}</span></div>
      <ol class="onb-stages">${ph.stages.map(s => { n++; const t = TAG[s.status];
        return `<li class="onb-st${s.status ? ' ' + s.status : ''}"><span class="onb-n">${n}</span><span class="onb-tx"><span class="onb-t"><b>${esc(s.name)}</b>${t ? `<i class="onb-tag" title="${esc(t[1])}">${esc(t[0])}</i>` : ''}</span><span class="onb-g">${esc(s.gets)}</span></span></li>`; }).join('')}</ol></li>`).join('')}</ol>`;
  }
  return { PHASES, timeline };
})();
