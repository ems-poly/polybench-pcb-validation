/* Hands-free voice operation: the Voice tab (live transcript, controls, settings), push-to-talk, the pending-accept countdown,
   voice navigation and context reporting. Server side: voice/controller.py (docs/VOICE.md section 3.4).
   Loaded after app.js and uses its globals (S, ui, api, cmd, esc, $, toast, render, renderBottom, curStep). */
const VOICE = (() => {
  const SRC = { ptt: 'PTT', handsfree: 'hands-free', typed: 'typed', clip: 'clip' };
  const log = [];                          // utterance entries, oldest first, unique by utt
  let st = null, stAt = 0;                 // last /api/voice/status and when it arrived
  let pollT = null, beatT = null, held = false, heldBy = '', heldCode = '', typed = '';
  let ctxSent = '', ctxT = null, stFetchT = null;

  const cfg = () => (st && st.config) || {};
  const on = () => !!(st && st.enabled);
  const el = id => document.getElementById(id);
  /* the push-to-talk key as the operator reads it: the configured key is the one source of truth for the bar, the hint and the tooltips */
  const keyName = code => { const c = code || 'Backquote'; return c === 'Backquote' ? '`' : /^Key[A-Z]$/.test(c) ? c.slice(3) : /^Digit\d$/.test(c) ? c.slice(5) : c.replace(/^(Arrow|Numpad)/, '$1 '); };

  /* ---- transcript model */
  function upsert(raw) {
    const e = raw && (raw.payload || raw); if (!e || e.utt == null) return false;
    const ent = { ...e }; if (!ent.ts) ent.ts = raw.ts || new Date().toISOString();
    const i = log.findIndex(x => x.utt === ent.utt);
    if (i >= 0) log[i] = { ...log[i], ...ent }; else { log.push(ent); log.sort((a, b) => a.utt - b.utt); }
    while (log.length > 300) log.shift();
    return i < 0;
  }
  function mergeSnapshot() { if (S && Array.isArray(S.voice)) S.voice.forEach(upsert); }
  const hhmmss = ts => { const m = /T(\d\d:\d\d:\d\d)/.exec(ts || ''); if (m) return m[1]; const d = new Date(); return d.toTimeString().slice(0, 8); };

  function valuesTxt(e) {
    const vs = e.values || []; if (!vs.length) return '';
    const f = v => `${v.board} ${v.test_id} = ${v.value}${v.unit ? ' ' + v.unit : ''}`;
    if (vs.length <= 3) return 'values ' + vs.map(f).join(', ');
    return `values ${vs.slice(0, 2).map(f).join(', ')} + ${vs.length - 2} more`;
  }
  function intentTxt(e) {
    if (e.intent === 'values') return valuesTxt(e) || 'values';
    const x = [e.intent || '']; if (e.test_id && e.intent !== 'values') x.push(e.test_id); if ((e.boards || []).length && e.intent !== 'values') x.push(e.boards.join(' '));
    return x.filter(Boolean).join(' ');
  }
  function rowHtml(e) {
    const llm = e.route === 'llm';
    const route = e.route === 'none' || !e.route ? '' : llm ? `<span class="vb llm" title="Interpreted by the local language model">LLM ${esc(e.model || '')}</span>` : '<span class="vb gram">grammar</span>';
    const out = e.outcome ? `<span class="vb out-${esc(e.outcome)}" title="${esc(e.detail || '')}">${esc(e.outcome)}</span>` : '';
    const full = (e.values || []).map(v => `${v.board} ${v.test_id} = ${v.value} ${v.unit || ''}`).join('\n');
    return `<div class="vrow${llm ? ' llmrow' : ''}" data-utt="${e.utt}"><span class="vt">${esc(hhmmss(e.ts))}</span><span class="vb src">${esc(SRC[e.source] || e.source || '')}</span>
      <span class="vtr">${esc(e.transcript || '')}</span><span class="vin" title="${esc(full)}">${esc(intentTxt(e))}</span>${route}${out}
      ${e.reply ? `<span class="vrp" title="Spoken reply">&#8627; <span class="vreply">${esc(e.reply)}</span></span>` : ''}${e.detail && e.outcome !== 'applied' ? `<span class="vdet faint">${esc(e.detail)}</span>` : ''}</div>`;
  }
  function paintList() {
    const l = el('vlist'); if (!l) return;
    const atEnd = l.scrollTop + l.clientHeight >= l.scrollHeight - 30;
    l.innerHTML = log.length ? log.map(rowHtml).join('') : '<div class="faint vempty">Nothing said yet. Hold the talk button or the ` key, or type an utterance below.</div>';
    if (atEnd || !l.dataset.scrolled) { l.scrollTop = l.scrollHeight; l.dataset.scrolled = '1'; }
  }

  /* ---- tab */
  function issues() {
    if (!st) return 'Voice is not available on this server (restart Polybench after updating).';
    const x = [];
    /* the input warnings (speech to text, language model, microphone) only matter while Voice input is on; each part is its own line */
    if (on() && st.stt && !st.stt.ok) x.push(`Speech to text not installed: ${st.stt.detail || ''}.\nInstall: ${st.stt.install || ".venv/bin/pip install -e '.[voice]'"}\nMicrophone permission: System Settings > Privacy > Microphone, for the terminal that runs Polybench.`);
    if (on() && st.llm && !st.llm.ok && cfg().llm_enabled !== false) x.push(`Language model not reachable: ${st.llm.detail || ''}.\nInstall: ${st.llm.install || 'ollama pull ' + (cfg().llm_model || 'qwen3:8b')}\nThe grammar still works without it.`);
    if (on() && st.stalling) x.push('Microphone stream keeps stalling: it has been restarted ' + (st.restarts || 0) + ' times. Check the input device and the audio route (System Settings > Sound).');
    if (st.tts && !st.tts.ok) x.push(`Speech output unavailable: ${st.tts.detail || ''}`);
    return x.join('\n');
  }
  function fillBottom(b) {
    mergeSnapshot();
    b.innerHTML = `<div class="vwrap"><div class="vtool" id="vtool">
        <span class="vgrp" id="vg-out"><button data-vo="readouts" id="vo-readouts" role="switch" class="vsw" title="Voice readouts: Polybench reads results and prompts aloud">Voice readouts</button>
          <span class="vkids" id="vk-out" hidden><span class="vvoice faint" id="vo-voice" title="Readout voice in use"></span></span></span>
        <span class="vgrp" id="vg-in"><button data-vo="power" id="vo-power" role="switch" class="vsw" title="Voice input: speak to Polybench (needs the microphone)">Voice input</button>
          <span class="vkids" id="vk-in" hidden>
            <button data-vo="talk" id="vo-talk" class="vtalk" title="Hold to talk (or hold the configured key)">Hold to talk</button>
            <button data-vo="hf" id="vo-hf" title="Hands-free: listen continuously, speech segments only">Hands-free</button>
            <span class="vmeter" id="vo-meter" title="Microphone level"><b></b></span><span class="vstate faint" id="vo-state"></span>
            <button data-vo="ack" id="vo-ack" role="switch" class="vsw" title="Voice acknowledgements: Polybench says Accepted, Rejected and the like after your command">Voice acknowledgements</button></span></span>
        <button data-vo="settings" id="vo-settings" hidden>Settings</button>
        <form class="vtype" id="vo-form"><input id="vtext" autocomplete="off" placeholder="type an utterance, e.g. S1 0.2 volts" aria-label="Type an utterance"><button type="submit" class="sm">Send</button></form></div>
      <div class="vissue" id="vissue"></div><div class="vlist" id="vlist"></div></div>`;
    const t = el('vtext'); t.value = typed; t.oninput = () => { typed = t.value; };
    paintList(); paintTool(); fetchStatus();
  }
  const composerFocused = () => ui.bottomTab === 'voice' && document.activeElement && document.activeElement.id === 'vtext';
  function refreshKeep() { mergeSnapshot(); paintList(); paintTool(); }

  function paintTool() {
    const set = (id, fn) => { const x = el(id); if (x) fn(x); };
    const sw = (x, v) => { x.classList.toggle('on', v); x.setAttribute('aria-checked', v); x.setAttribute('aria-pressed', v); };
    const rd = cfg().tts_enabled !== false;
    set('vo-readouts', x => sw(x, rd));
    set('vo-power', x => sw(x, on()));
    set('vk-out', x => { x.hidden = !rd; });
    set('vk-in', x => { x.hidden = !on(); });
    set('vo-settings', x => { x.hidden = !(rd || on()); });          // Settings holds both groups' options: reachable while either switch is on
    set('vo-talk', x => { x.classList.toggle('rec', !!(st && st.recording) || held); x.disabled = !st; });
    set('vo-hf', x => { x.classList.toggle('on', !!(st && st.handsfree)); x.setAttribute('aria-pressed', !!(st && st.handsfree)); x.disabled = !st; });
    set('vo-ack', x => sw(x, cfg().tts_ack !== false));
    set('vo-meter', x => { const lv = Math.max(0, Math.min(1, +((st && st.level) || 0))); x.firstElementChild.style.width = Math.round(lv * 100) + '%'; x.classList.toggle('speech', !!(st && st.in_speech)); });
    set('vo-state', x => { x.textContent = !st ? 'unavailable' : !st.enabled ? 'off' : st.recording ? 'recording' : st.speaking ? 'speaking' : st.busy ? 'working' : (st.stt && !st.stt.ok) ? 'not installed' : st.listening ? (st.handsfree ? 'listening (hands-free)' : 'ready') : 'ready'; });
    set('vo-voice', x => { const t = st && st.tts; x.textContent = t ? 'Voice: ' + (t.label || 'System voice') + (t.warn ? ' - ' + t.warn : '') : ''; x.classList.toggle('warn', !!(t && t.warn)); });
    set('vissue', x => { x.textContent = issues(); x.style.display = x.textContent ? '' : 'none'; });
    paintStrip();
  }

  /* ---- status */
  async function fetchStatus() {
    const r = await api('/api/voice/status');
    if (r.ok) { st = r.data; stAt = Date.now(); } else if (!st) st = null;
    schedule(); if (ui.bottomTab === 'voice') paintTool(); else paintStrip();
  }
  function setStatus(s) { if (s && typeof s === 'object' && 'enabled' in s) { st = s; stAt = Date.now(); schedule(); paintTool(); } }
  function schedule() {                                   // ~4 Hz only while voice is on and the page is visible
    const want = on() && !document.hidden;
    if (want && !pollT) pollT = setInterval(fetchStatus, 250);
    if (!want && pollT) { clearInterval(pollT); pollT = null; }
  }
  const post = async (path, body) => { const r = await api(path, body || {}); if (r.ok) setStatus(r.data); else toast((r.data && r.data.error) || 'voice: request failed', true); return r; };

  /* ---- pending grace strip */
  /* bench commands (voice/benchcmd.py): a spoken start waits here for "confirm"; the countdown is the server's */
  function benchText() {
    const c = st && st.bench && st.bench.confirm; if (!c) return '';
    const left = Math.max(0, Math.ceil((+c.deadline_s || 0) - (Date.now() - stAt) / 1000));
    return `${c.text} (${left} s) \u2014 Esc or say anything else to cancel`;
  }
  /* guided section (voice/guide.py): the line under the push-to-talk bar and the panel at the top of the page */
  const guideLeft = g => !g || !g.hold ? null : Math.max(0, Math.ceil((+g.hold.left_s || 0) - (Date.now() - stAt) / 1000));
  function guideTag() {
    const g = st && st.guide; if (!g) return '';
    const l = guideLeft(g);
    return `Guide: ${g.done ? 'section done' : `step ${g.n} of ${g.total}`}${l !== null ? ` \u00b7 hold ${l} s` : ''}`;
  }
  const GSTATE = { pending: 'ready when you are', running: 'running', done: 'done', failed: 'failed, say redo', paused: 'paused, say ready', lapsed: 'hold lapsed, say redo', skipped: 'skipped' };
  async function guideSay(text) {
    const r = await api('/api/voice/text', { text, context: ctxObj() });
    if (r.ok) { upsert(r.data); paintList(); } else toast((r.data && r.data.error) || 'guide: request failed', true);
    fetchStatus();
  }
  function paintGuide() {                                 // the buttons are built once, so keyboard focus survives the countdown
    let p = el('gpanel'); const g = st && st.guide;
    if (!g) { if (p) p.remove(); return; }
    const host = $('#centre'); if (!host) return;
    if (!p) {
      p = document.createElement('section'); p.id = 'gpanel'; p.setAttribute('aria-label', 'Guided section');
      p.innerHTML = `<div class="gh"></div><div class="gbody" aria-live="polite"></div><div class="gb"><button class="primary" data-vo="gready" title="Run this step (the spoken word is: ready)">Ready</button><button data-vo="gnext" title="Next step">Next</button><button data-vo="grepeat" title="Say this step again">Repeat</button><button data-vo="gend" title="End the guide">End</button></div>`;
    }
    const stp = g.step || {}, l = guideLeft(g);
    const dots = (g.steps || []).map(x => `<i class="gd gd-${esc(x.state)}${x.n === g.n ? ' cur' : ''}" title="Step ${x.n}: ${esc(x.title)} (${esc(x.state)})"></i>`).join('');
    const head = `<b>${esc(g.title)}</b><span class="faint">board ${esc(g.board)}</span><span class="gn">${g.done ? 'Section done' : `Step ${g.n} of ${g.total}`}</span><span class="gdots" aria-hidden="true">${dots}</span>`;
    const body = `${g.done ? `<div class="gs">${esc(g.summary)}</div>` : `<div class="gs"><b>${esc(stp.title)}</b> <span class="gst">${esc(GSTATE[stp.state] || stp.state)}</span></div>${stp.text ? `<div class="gt">${esc(stp.text)}</div>` : ''}`}
        ${l !== null ? `<div class="gl gh-hold${l <= 15 ? ' low' : ''}">Hold window: ${l} s left</div>` : ''}${g.last ? `<div class="gl">Last reading: ${esc(g.last)}</div>` : ''}`;
    const gh = p.firstElementChild, gbody = gh.nextElementSibling;
    if (gh.innerHTML !== head) gh.innerHTML = head;
    if (gbody.innerHTML !== body) gbody.innerHTML = body;
    const rd = p.querySelector('[data-vo="gready"]'), nx = p.querySelector('[data-vo="gnext"]');
    rd.disabled = !!(g.done || stp.kind === 'manual'); nx.textContent = g.done ? 'Next section' : 'Next';
    if (p.parentNode !== host || host.firstChild !== p) host.prepend(p);
  }
  function benchLine() {      // the last bench command, or the running action: shown in the line under the push-to-talk bar
    const b = st && st.bench; if (!b) return '';
    const gt = guideTag();
    if (gt) return b.running ? `${gt} \u2014 running ${b.running.title}${b.running.state === 'cancelling' ? ' (stopping)' : ', say stop to abort'}` : gt;
    if (b.running) return `Running ${b.running.title} on ${b.running.board}${b.running.state === 'cancelling' ? ' (stopping)' : ' \u2014 say stop to abort'}`;
    if (b.last && Date.now() / 1000 - b.last.ts < 120) return `Last command: ${b.last.text}${b.last.reply ? ' \u2192 ' + b.last.reply : ''}`;
    return '';
  }
  function pendingText() {
    const bt = benchText(); if (bt) return bt;
    const p = st && st.pending; if (!p) return '';
    const e = log.find(x => x.utt === p.utt);
    const vs = (e && e.values) || [];
    const what = !vs.length ? 'the proposed values' : vs.length <= 2 ? vs.map(v => `${v.board} ${v.test_id} ${v.value}${v.unit ? ' ' + v.unit : ''}`).join(', ') : `${vs.length} values (${vs[0].board} to ${vs[vs.length - 1].board} ${vs[0].test_id})`;
    const left = Math.max(0, Math.ceil((+p.deadline_s || 0) - (Date.now() - stAt) / 1000));
    return `Accepting ${what} in ${left} s \u2014 Esc or say cancel`;
  }
  function paintStrip() {
    paintBar(); paintGuide();
    let s = el('vstrip'); const t = pendingText();
    if (!t) { if (s) s.remove(); return; }
    const host = $('#centre'); if (!host) return;
    if (!s) { s = document.createElement('div'); s.id = 'vstrip'; s.setAttribute('role', 'status'); }
    if (s.parentNode !== host || host.firstChild !== s) host.prepend(s);
    s.innerHTML = `<span>${esc(t)}</span><button class="sm" data-vo="cancel">Cancel</button>`;
  }
  setInterval(() => { if (st && (st.pending || (st.bench && st.bench.confirm) || (st.guide && st.guide.hold))) paintStrip(); }, 250);

  /* ---- push-to-talk bar: lives in index.html (#pttbar), so no re-render of the page removes it; shown whenever Voice is on */
  let pttPhase = 'idle', pttStopAt = 0, pttUttAt = 0, pttResult = '', pttResT = null;
  const lastUtt = () => log.reduce((m, e) => Math.max(m, e.utt || 0), 0);
  function paintBar() {
    const bar = el('pttbar'); if (!bar) return;
    bar.hidden = !on(); if (!on()) return;
    const hf = !!(st && st.handsfree), live = !hf && (held || !!(st && st.recording));
    if (pttPhase === 'trans' && st && !st.busy && !st.recording && Date.now() - pttStopAt > 600) {
      const e = log.filter(x => (x.utt || 0) > pttUttAt).pop();
      if (e || Date.now() - pttStopAt > 3000) {
        pttResult = e ? `${e.transcript ? '\u201c' + e.transcript + '\u201d' : 'Nothing heard'}${e.outcome ? ' \u2014 ' + e.outcome : ''}` : 'Nothing heard';
        pttPhase = 'result'; clearTimeout(pttTransT); clearTimeout(pttResT); pttResT = setTimeout(() => { if (pttPhase === 'result') { pttPhase = 'idle'; paintBar(); } }, 6000);
      }
    }
    const phase = hf ? 'hf' : live ? 'listen' : pttPhase;
    const label = { hf: 'Hands-free', listen: 'Listening\u2026', trans: 'Transcribing\u2026', result: pttResult, idle: `Hold ${keyName(cfg().ptt_key)} to talk` }[phase];
    const lv = Math.max(0, Math.min(1, +((st && st.level) || 0) * 3));
    const b = el('ptt-bar'); b.dataset.phase = phase; b.classList.toggle('rec', phase === 'listen'); b.classList.toggle('hf', phase === 'hf');
    b.setAttribute('aria-pressed', phase === 'listen');
    el('ptt-fill').style.width = (phase === 'listen' || phase === 'hf' ? Math.round(lv * 100) : 0) + '%';
    el('ptt-label').textContent = label;
    const k = keyName(cfg().ptt_key), bl = benchLine(), hint = el('ptt-hint'); b.title = `Press and hold to talk (or hold ${k})`;
    if (hint) { hint.dataset.dflt = `Hold ${k} to talk` + (k === 'Space' ? '' : ' (or Space, outside a text box)'); hint.textContent = bl || hint.dataset.dflt; hint.classList.toggle('vbench', !!bl); }
    const l = el('ptt-latch'); l.classList.toggle('on', hf); l.setAttribute('aria-pressed', hf); l.disabled = !st;
  }

  /* ---- push to talk */
  function pttStart(by) {
    if (held || (st && st.handsfree && by !== 'key')) return; held = true; heldBy = by; heldCode = ''; pttPhase = 'listen'; clearTimeout(pttResT); paintTool();
    api('/api/voice/ptt', { action: 'start' }).then(r => {
      if (!r.ok) { toast((r.data && r.data.error) || 'push to talk failed', true); pttGiveUp('Push to talk failed'); return; }
      setStatus(r.data);
      if (!r.data.recording && !r.data.busy) pttGiveUp('Nothing recorded' + (r.data.mic && !r.data.mic.ok ? ': ' + (r.data.mic.detail || 'microphone not available') : ''));   // the server did not start a clip (no microphone): nothing will ever transcribe
    });
    beatT = setInterval(() => api('/api/voice/ptt', { action: 'beat' }), 500);
  }
  /* the bar leaves 'Transcribing' on every path: a clip that never started, an answer that never comes (8 s), or the usual result */
  let pttTransT = null;
  function pttGiveUp(msg) {
    held = false; heldBy = ''; heldCode = ''; clearInterval(beatT); beatT = null; clearTimeout(pttTransT);
    pttResult = msg; pttPhase = 'result'; clearTimeout(pttResT); pttResT = setTimeout(() => { if (pttPhase === 'result') { pttPhase = 'idle'; paintBar(); } }, 6000); paintTool();
  }
  function pttStop() {
    if (!held) return; held = false; heldBy = ''; heldCode = ''; clearInterval(beatT); beatT = null;
    pttPhase = 'trans'; pttStopAt = Date.now(); pttUttAt = lastUtt(); paintTool();
    clearTimeout(pttTransT); pttTransT = setTimeout(() => { if (pttPhase === 'trans') pttGiveUp('Nothing heard'); }, 8000);
    api('/api/voice/ptt', { action: 'stop' }).then(r => { if (r.ok) setStatus(r.data); });
  }
  const typing = t => t && ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || (t && t.isContentEditable);
  document.addEventListener('keydown', e => {            // capture phase: runs before the page's own key handling
    if (!S) return;
    if ((e.key === 'Enter' || e.code === 'Space') && e.target.closest && e.target.closest('#gpanel')) { e.stopPropagation(); return; }      // a focused guide button is pressed by Enter or Space: not the page's accept, not push-to-talk
    if (e.key === 'Escape' && st && (st.pending || (st.bench && st.bench.confirm)) && !document.querySelector('.veil')) {
      e.preventDefault(); e.stopImmediatePropagation(); api('/api/voice/cancel', {}).then(r => { if (r.ok) setStatus(r.data); fetchStatus(); }); return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || document.querySelector('.veil')) return;
    const isVal = e.target && e.target.id === 'val';        // the big entry box: the PTT key works there, Space types
    if (e.code === (cfg().ptt_key || 'Backquote') && (!typing(e.target) || isVal)) {
      e.preventDefault(); e.stopImmediatePropagation(); if (!e.repeat && !held) { pttStart('key'); heldCode = e.code; }
    } else if (e.code === 'Space' && on() && !(st && st.handsfree) && !typing(e.target)) {
      e.preventDefault(); e.stopImmediatePropagation(); if (!e.repeat && !held) { pttStart('key'); heldCode = 'Space'; }
    }
  }, true);
  document.addEventListener('keyup', e => { if (held && heldBy === 'key' && heldCode && e.code === heldCode) { e.preventDefault(); e.stopImmediatePropagation(); pttStop(); } }, true);
  window.addEventListener('blur', () => { if (held) pttStop(); });
  document.addEventListener('pointerdown', e => { const b = e.target.closest && e.target.closest('[data-vo="talk"],[data-vo="pttbar"]'); if (b && !b.disabled) { e.preventDefault(); try { b.setPointerCapture(e.pointerId); } catch { /* ok */ } pttStart('button'); } });
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(ev => document.addEventListener(ev, () => { if (held && heldBy === 'button') pttStop(); }));
  document.addEventListener('mousedown', e => { if (e.target.closest && e.target.closest('[data-vo="pttbar"]')) e.preventDefault(); });   // keep focus (and the half-typed value) in #val

  /* ---- clicks, forms */
  document.addEventListener('click', async e => {
    const b = e.target.closest('[data-vo]'); if (!b) return;
    switch (b.dataset.vo) {
      case 'power': await post('/api/voice/enable', { on: !on() }); break;
      case 'hf': await post('/api/voice/handsfree', { on: !(st && st.handsfree) }); break;
      case 'voiceinfo': { const x = el('vs-infobox'); if (x) { const open = x.style.display === 'none'; x.style.display = open ? '' : 'none'; b.setAttribute('aria-expanded', open); } break; }
      case 'openspeech': { const r = await api('/api/voice/open-speech-settings', {}); if (!r.ok || !r.data.ok) toast('Could not open System Settings', true); break; }
      case 'testvoice': {                                                  // the voice selected in the dialog, saved or not
        const r = await api('/api/voice/test-voice', { voice: el('vs-voice') ? el('vs-voice').value : null });
        if (r.ok && r.data.muted) toast('Voice readouts are off: turn them on to hear the test voice');
        else if (!r.ok || !r.data.spoken) toast('Speech output unavailable', true);
        break; }
      case 'readouts': await post('/api/voice/config', { tts_enabled: cfg().tts_enabled === false }); break;
      case 'ack': await post('/api/voice/config', { tts_ack: cfg().tts_ack === false }); break;
      case 'cancel': await post('/api/voice/cancel'); break;
      case 'gready': await guideSay('ready'); break;
      case 'gnext': await guideSay(st && st.guide && st.guide.done ? 'next section' : 'next step'); break;
      case 'grepeat': await guideSay('repeat'); break;
      case 'gend': await guideSay('end guide'); break;
      case 'guide': { e.stopPropagation(); const r = await api('/api/voice/guide', { section: b.dataset.sec, context: ctxObj() }); if (r.ok) { upsert(r.data); paintList(); if (r.data.outcome === 'rejected') toast(r.data.reply, true); } else toast((r.data && r.data.error) || 'guide: request failed', true); fetchStatus(); break; }
      case 'settings': openSettings(); break;
      case 'vcancel': $('#dialog-root').innerHTML = ''; break;
      case 'copypull': { const t = b.dataset.cmd || ''; try { navigator.clipboard.writeText(t).then(() => toast('Copied: ' + t), () => toast(t)); } catch { toast(t); } break; }
    }
  });
  document.addEventListener('submit', async e => {
    if (e.target.id === 'vo-form') {
      e.preventDefault(); const t = el('vtext'); const text = t.value.trim(); if (!text) return;
      t.value = ''; typed = '';
      const r = await api('/api/voice/text', { text, context: ctxObj() });
      if (r.ok) { upsert(r.data); paintList(); } else toast((r.data && r.data.error) || 'voice text failed', true);
    } else if (e.target.id === 'vo-sform') {
      e.preventDefault(); await saveSettings();
    }
  });

  /* ---- settings */
  const SYSV = 'System voice (set in macOS)';
  const VOICE_NOTE = 'Readouts use macOS voices. For a natural voice, download a Premium voice (Jamie, Serena, Zoe or Ava \u2026) in System Settings \u2192 Accessibility \u2192 Spoken Content \u2192 System voice \u2192 Manage Voices, then pick it here. Siri voices are not available to other apps. Basic voices (Daniel, Samantha, Fred \u2026) sound robotic.';
  async function openSettings() {
    const root = $('#dialog-root'); await fetchStatus();
    const c = cfg();
    root.innerHTML = `<div class="veil"><div class="dlg vodlg" role="dialog" aria-label="Voice settings"><h2>Voice settings</h2><form id="vo-sform">
      <div class="vissue" id="vs-issue" style="display:${issues() ? '' : 'none'}">${esc(issues())}</div>
      <div class="vpriv" id="vs-priv">${esc((st && st.privacy) || 'Voice: local only')}</div>
      <div class="vfall" id="vs-fall" style="display:${st && st.llm && st.llm.fallback ? '' : 'none'}"><span id="vs-falltxt">${esc((st && st.llm && st.llm.detail) || '')}</span> <button type="button" class="sm" data-vo="copypull" data-cmd="${esc((st && st.llm && st.llm.install) || '')}">Copy</button></div>
      <div class="vgrid"><label for="vs-llm">Language model</label><span><input type="checkbox" id="vs-llm" ${c.llm_enabled !== false ? 'checked' : ''}> use for free phrasing (grammar always runs first)</span>
        <label for="vs-model">Model</label><span><select id="vs-model"><option value="${esc(c.llm_model || '')}">${esc(c.llm_model || '')}</option></select> <span class="faint" id="vs-mdet"></span></span>
        <label for="vs-voice">Readout voice</label><span><select id="vs-voice"><option value="${esc(c.tts_voice || '')}">${esc(c.tts_voice || SYSV)}</option></select>
          <button type="button" class="sm" id="vs-info" data-vo="voiceinfo" aria-expanded="false" aria-label="About readout voices" title="About readout voices">&#9432;</button>
          <button type="button" class="sm" data-vo="testvoice" id="vs-test">Test voice</button>
          <span class="vinfo faint" id="vs-infobox" role="note" style="display:none">${esc(VOICE_NOTE)}<br><button type="button" class="sm" data-vo="openspeech" id="vs-open">Open Spoken Content settings</button></span></span>
        <label for="vs-rate">Readout rate (words/min)</label><input type="number" id="vs-rate" min="80" max="400" step="10" value="${esc(c.tts_rate ?? 200)}">
        <label for="vs-grace">Grace before auto-accept (s)</label><span><input type="number" id="vs-grace" min="0" max="30" step="0.5" value="${esc(c.grace_s ?? 3)}"> <span class="faint">0 = only "accept"</span></span>
        <label for="vs-gap">Hands-free pause gap (s)</label><input type="number" id="vs-gap" min="0.3" max="3" step="0.1" value="${esc(c.hf_gap_s ?? 0.8)}">
        <label for="vs-key">Push-to-talk key</label><span><button type="button" id="vs-key" data-code="${esc(c.ptt_key || 'Backquote')}">${esc(c.ptt_key || 'Backquote')}</button> <span class="faint">click, then press a key</span></span>
        <label for="vs-conf">Confirm simulated starts</label><span><input type="checkbox" id="vs-conf" ${c.confirm_simulated !== false ? 'checked' : ''}> "start test" in simulate mode still asks for "confirm" (a real run always does)</span>
        <label for="vs-row">Row (batch) mode</label><span><input type="checkbox" id="vs-row" ${c.row_mode !== false ? 'checked' : ''}> a bare value goes to the current board, then on to the next open one</span></div>
      <div class="btns"><button type="button" data-vo="vcancel">Cancel</button><button type="submit" class="primary" id="vs-save">Save</button></div></form></div></div>`;
    root.onkeydown = e => { if (e.key === 'Escape' && !e.target.dataset.capturing) root.innerHTML = ''; };
    const kb = el('vs-key');
    kb.onclick = () => { kb.dataset.capturing = '1'; kb.textContent = 'press a key...'; kb.focus(); };
    kb.onkeydown = e => { if (kb.dataset.capturing) { e.preventDefault(); e.stopPropagation(); kb.dataset.code = e.code; kb.textContent = e.code; delete kb.dataset.capturing; } };
    api('/api/voice/models').then(r => {
      const s = el('vs-model'); if (!s) return; const d = r.data || {};
      const names = d.models || []; const cur = c.llm_model || d.default || '';
      const size = n => { const x = (d.details || []).find(y => y.name === n); return x && x.size ? ' ' + x.size : ''; };
      if (names.length || d.default) { s.innerHTML = [...new Set([cur, ...(d.default ? [d.default] : []), ...names])].filter(Boolean).map(n => `<option value="${esc(n)}" ${n === cur ? 'selected' : ''}>${esc(n)}${esc(size(n))}${n === d.default ? ' (default)' : ''}${names.includes(n) ? '' : ' (not installed)'}</option>`).join(''); }
      const m = el('vs-mdet'); if (m) m.textContent = d.ok === false ? (d.detail || 'Ollama not reachable') : 'Ollama on this Mac only (127.0.0.1); other hosts are refused';
    });
    api('/api/voice/voices').then(r => {
      const s = el('vs-voice'); if (!s || !r.ok) return; const vs = r.data.voices || []; const cur = c.tts_voice || '';
      s.innerHTML = `<option value="">${SYSV}</option>` + vs.map(v => { const n = typeof v === 'string' ? v : v.name; return `<option value="${esc(n)}" ${n === cur ? 'selected' : ''}>${esc(n)}</option>`; }).join('');
    });
  }
  async function saveSettings() {
    const patch = { llm_enabled: el('vs-llm').checked, llm_model: el('vs-model').value, tts_voice: el('vs-voice').value, tts_rate: +el('vs-rate').value,
      confirm_simulated: el('vs-conf').checked, grace_s: +el('vs-grace').value, hf_gap_s: +el('vs-gap').value, ptt_key: el('vs-key').dataset.code, row_mode: el('vs-row').checked };
    const r = await post('/api/voice/config', patch);
    if (r.ok) { $('#dialog-root').innerHTML = ''; toast('Voice settings saved'); }
  }

  /* ---- server events */
  function onEvent(m) {
    if (m.kind === 'voice') { const fresh = upsert(m.entry); if (ui.bottomTab === 'voice') paintList(); if (fresh || true) scheduleStatus(); }
    else if (m.kind === 'voice_nav') {
      if (m.step && S.steps.some(s => s.id === m.step)) ui.step = m.step;
      if (m.test_id !== undefined) ui.test = m.test_id || null;
      if (m.board) ui.board = m.board;
      ui.pv = null; ui.draft = ''; ui.armed = null; ctxSent = ctxKey(); render();
    } else if (m.kind === 'voice_status') { if ('enabled' in m) setStatus(m); scheduleStatus(); }
  }
  function scheduleStatus() { clearTimeout(stFetchT); stFetchT = setTimeout(fetchStatus, 60); }

  /* ---- context and visibility */
  const aoiReview = () => { const st = S && S.steps.find(x => x.id === ui.step); return !!(st && st.kind === 'visual' && ui.vmode === 'review'); };
  const CLIENT = 'c' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);       // one id per page load: the server mutes only if every client is hidden
  const ctxObj = () => ({ step: ui.step, test_id: ui.test || null, board: ui.board || null, aoi_review: aoiReview(), visible: !document.hidden, client: CLIENT });
  /* A page that is going away (reload, navigation, tab close) turns hidden first, and a fetch started then is cancelled by the browser:
     WebKit logs that as an access-control error in the console (UIR-36). A beacon is built for it and is delivered after the page is gone. */
  function postVisibility() {
    if (document.hidden) { try { if (navigator.sendBeacon('/api/voice/visibility', new Blob([JSON.stringify({ visible: false, client: CLIENT })], { type: 'application/json' }))) return Promise.resolve(); } catch { /* fall through to fetch */ } }
    return api('/api/voice/visibility', { visible: !document.hidden, client: CLIENT }).then(r => { if (r.ok) setStatus(r.data); schedule(); });
  }
  const ctxKey = () => [ui.step, ui.test, ui.board, aoiReview()].join('|');
  function sendContext() {                                // on every render, click and 400 ms tick: step, tab, test, board, AOI review
    if (!S) return; const key = ctxKey(); if (key === ctxSent) return;
    ctxSent = key; api('/api/voice/context', ctxObj());
  }
  setInterval(() => { if (!document.hidden) sendContext(); }, 400);
  document.addEventListener('click', () => setTimeout(sendContext, 0));                     // after app.js has handled the click   // test and board clicks re-render only parts of the page, not render()
  document.addEventListener('visibilitychange', () => { if (held && document.hidden) pttStop(); postVisibility(); });
  window.addEventListener('focus', postVisibility);
  window.addEventListener('pageshow', postVisibility);
  setInterval(postVisibility, 15000);                      // heartbeat: the server forgets a visible client that stops posting
  postVisibility();                                         // on load: a fresh server starts with every client unknown

  function afterRender() { mergeSnapshot(); sendContext(); paintStrip(); if (ui.bottomTab === 'voice') paintList(); if (!st && !afterRender.once) { afterRender.once = true; fetchStatus(); } }

  return { held: () => held, fillBottom, composerFocused, refreshKeep, afterRender, onEvent, _log: log };
})();
