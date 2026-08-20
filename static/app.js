/* Pith offset estimator — client.
 *
 * World coordinates are pixels of the rendered transverse preview. The preview
 * may be a downsampled copy of the TIFF, so `step` converts world pixels back
 * to original image pixels and `mmPerWorld` converts them to millimetres.
 *
 * Ring geometry follows ri.view.RingGeometry: a boundary at zpos with tilt
 * theta is the line through (zpos, (H-1)/2) with direction (sin t, cos t), so
 * its endpoints are zpos -/+ (H-1)/2 * tan(theta) at the top and bottom of the
 * image. The distance from a candidate pith to that boundary is measured along
 * the boundary normal, which is the direction in which RingIndicator measures
 * its tilt-corrected ring widths.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

const S = {
  session: null,
  trees: [],
  index: 0,
  core: null,          // /api/core payload
  bitmap: null,
  imgW: 0, imgH: 0, step: 1,
  mmPerWorld: null,
  method: 'concentric',
  lastSpecies: null,   // sticky across trees; see applySaved
  onlyOpenedSection: false,  // case 3: sum a broken core's sections by default
  view: { k: 1, tx: 0, ty: 0 },
  pith: null,          // {x, y} world, placed
  hover: null,         // {x, y} world, live
  nCircles: 12,
  showRings: true,
  showLabels: false,
  clim: { lo: 200, hi: 1200 },
  diamMode: 'diameter',
  loading: false,
};

const canvas = $('canvas');
const ctx = canvas.getContext('2d');

/* ------------------------------------------------------------------ boot */

init().catch((e) => toast('Could not start: ' + e.message, 'err', 8000));

async function init() {
  await loadSession(false);
  wireUI();
  resizeCanvas();
  if (S.trees.length) await goto(S.session.start_index || 0);
  else showStageMessage('No cores found in this folder. The tool looks for ' +
    'RingIndicator sidecar files named <b>&lt;core&gt;_ring_and_fibre.txt</b>.');
}

async function loadSession(rescan) {
  const r = await fetch('/api/session' + (rescan ? '?rescan=1' : ''));
  if (!r.ok) throw new Error('the server did not answer /api/session');
  S.session = await r.json();
  S.trees = S.session.trees;
  const f = S.session.folder;
  const parts = f.split(/[\\/]/).filter(Boolean);
  $('folderPath').textContent = parts.length > 2
    ? '…/' + parts.slice(-2).join('/') : f;
  $('folderPath').title = f;
  buildSpeciesSelect();
  renderTreeList();
  renderProgress();
}

/* ------------------------------------------------------------------ list */

function renderTreeList() {
  const q = ($('treeFilter').value || '').toLowerCase();
  const ul = $('treeList');
  ul.innerHTML = '';
  S.trees.forEach((t, i) => {
    if (q && t.tree.toLowerCase().indexOf(q) < 0) return;
    const li = document.createElement('li');
    li.className = (t.done ? 'done ' : '') + (t.has_image ? '' : 'noimg ') +
                   (i === S.index ? 'current' : '');
    li.innerHTML = '<span class="dot"></span><span class="name"></span><span class="val"></span>';
    li.querySelector('.name').textContent = t.tree;
    li.querySelector('.val').textContent = t.done
      ? fmt(t.result.distance_mm, 1) + ' mm'
      : (t.has_image ? '' : 'no image');
    li.title = t.cores.join('  ·  ') + (t.done ? '\nsaved: ' + t.result.method : '');
    li.onclick = () => { if (isNarrow()) $('sidebar').classList.add('hidden'); goto(i); };
    ul.appendChild(li);
  });
  const n = S.trees.length, d = S.trees.filter((t) => t.done).length;
  $('sideSummary').textContent = n + ' trees · ' + d + ' done · ' + (n - d) + ' to go';
}

function renderProgress() {
  const n = S.trees.length, d = S.trees.filter((t) => t.done).length;
  $('progressFill').style.width = n ? (100 * d / n) + '%' : '0';
  $('progressText').textContent = d + ' / ' + n;
}

/* ------------------------------------------------------------------ tree */

async function goto(i) {
  if (!S.trees.length) return;
  S.index = clamp(i, 0, S.trees.length - 1);
  renderTreeList();
  await loadCore(S.trees[S.index].tree, null);
}

async function loadCore(tree, stem) {
  S.loading = true;
  S.pith = null; S.hover = null; S.bitmap = null; S.core = null;
  showStageMessage('Loading ' + tree + ' …');
  draw();

  const url = '/api/core?tree=' + encodeURIComponent(tree) +
              (stem ? '&stem=' + encodeURIComponent(stem) : '');
  const r = await fetch(url);
  if (!r.ok) { showStageMessage('Could not read ' + tree + '.'); S.loading = false; return; }
  S.core = await r.json();

  $('treeName').textContent = S.core.tree;
  renderChips();
  renderFacts();
  applySaved(S.core.saved);

  if (!S.core.has_image) {
    hideStageMessage();
    showStageMessage('No transverse preview for <b>' + S.core.stem + '</b>. ' +
      'Expected <b>' + S.core.stem + '_Tv.tif</b> beside the indication files. ' +
      'You can still use case 3 (diameter &amp; bark).');
    S.imgW = 0; S.imgH = 0;
    S.mmPerWorld = S.core.mm_per_px;
    S.loading = false;
    if (S.method === 'concentric') setMethod('geometric');
    updateSave(); draw();
    return;
  }

  await loadImage();
  hideStageMessage();
  S.loading = false;
  resetView();
  restoreSavedPith(S.core.saved);
  updateSave();
  draw();
}

async function loadImage() {
  const u = '/api/image?tree=' + encodeURIComponent(S.core.tree) +
            '&stem=' + encodeURIComponent(S.core.stem) +
            '&lo=' + S.clim.lo + '&hi=' + S.clim.hi;
  const r = await fetch(u);
  if (!r.ok) throw new Error('preview could not be rendered');
  S.step = parseFloat(r.headers.get('X-Image-Step') || '1') || 1;
  const blob = await r.blob();
  S.bitmap = await createImageBitmap(blob);
  S.imgW = S.bitmap.width;
  S.imgH = S.bitmap.height;
  S.mmPerWorld = S.core.mm_per_px ? S.core.mm_per_px * S.step : null;
}

function renderChips() {
  const box = $('coreChips');
  box.innerHTML = '';
  S.core.siblings.forEach((s) => {
    const b = document.createElement('button');
    b.className = 'chip' + (s.selected ? ' active' : '') + (s.has_image ? '' : ' noimg');
    b.innerHTML = '';
    b.appendChild(document.createTextNode(s.stem));
    const sm = document.createElement('small');
    sm.textContent = (s.oldest_year === null ? '· ' + s.n_rings + ' rings'
                                             : '· oldest ' + s.oldest_year);
    b.appendChild(sm);
    b.title = s.selected
      ? 'Open core — chosen automatically because it reaches furthest back'
      : 'Use this core instead';
    if (!s.has_image) b.title += ' (no transverse preview)';
    b.onclick = () => { if (!s.selected) loadCore(S.core.tree, s.stem); };
    box.appendChild(b);
  });
}

function renderFacts() {
  const c = S.core;
  const bits = [];
  bits.push('<b>' + c.n_boundaries + '</b> rings');
  if (c.oldest_year !== null) bits.push('oldest <b>' + c.oldest_year + '</b>');
  if (c.accum_rw_mm !== null) bits.push('ΣRW <b>' + fmt(c.accum_rw_mm, 1) + '</b> mm');
  if (c.sections) bits.push('<b>' + c.sections.length + '</b> sections of this core');
  bits.push(c.res_um ? '<b>' + fmt(c.res_um, 2) + '</b> µm/px' : '<b>no pixel size</b>');
  if (c.n_missing) bits.push(c.n_missing + ' missing');
  if (c.n_broken) bits.push(c.n_broken + ' fractures');
  $('coreFacts').innerHTML = bits.join(' · ');
}

/* --------------------------------------------------------- saved results */

function applySaved(saved) {
  // Blank the case-3 fields first: silently carrying the previous tree's
  // diameter into this one would produce a plausible, wrong number.
  ['bark', 'diameter'].forEach((id) => { $(id).value = ''; });
  $('outerGap').value = '0';
  setDiamMode('diameter');
  // Sections belong to this core, not the last one visited, so default back
  // to summing them unless this tree's own saved row used only one.
  S.onlyOpenedSection = !!(saved && saved.accum_files &&
    saved.accum_files.indexOf('+') < 0 && saved.accum_files === S.core.stem);
  $('onlyOpenedSection').checked = S.onlyOpenedSection;
  $('notes').value = saved ? (saved.notes || '') : '';
  // Precedence: what this tree was saved with, else the last species picked in
  // this session, else the placeholder. Sticky is what replaces the removed
  // name-based guess; unlike a sticky diameter it is visible in the dropdown
  // and only reaches the output through case 3.
  const sel = $('species');
  const want = (saved && saved.species) || S.lastSpecies || '';
  ensureSpeciesOption(want);
  sel.value = [...sel.options].some((o) => o.value === want) ? want : '';
  syncSrFromSpecies();

  if (saved) {
    if (saved.method.startsWith('pith indicated')) setMethod('indicated');
    else if (saved.method.startsWith('diameter')) {
      setMethod('geometric');
      if (saved.bark_mm != null) $('bark').value = saved.bark_mm;
      if (saved.diameter_mm != null) { $('diameter').value = saved.diameter_mm;
                                       setDiamMode('diameter'); }
      if (saved.outer_gap_mm != null) $('outerGap').value = saved.outer_gap_mm;
      if (saved.sr != null) $('sr').value = saved.sr;
    } else setMethod('concentric');
    toast('This tree already has a result (' + fmt(saved.distance_mm, 2) +
          ' mm). Saving again overwrites it.', 'warn', 4200);
  } else {
    setMethod('concentric');   // the common case, whatever the last tree used
  }
  updateGeoWork();
}

function restoreSavedPith(saved) {
  if (!saved || saved.pith_x_px == null || !S.bitmap) return;
  S.pith = { x: (saved.pith_x_px - 1) / S.step, y: (saved.pith_y_px - 1) / S.step };
  frameBoth(S.pith);
}

/** Fit a view that holds both the candidate pith and the innermost boundary. */
function frameBoth(p) {
  if (!S.bitmap || !S.core.rings.length) return;
  const L = ringLine(0);
  const pad = 0.12;
  let x0 = Math.min(p.x, L.x1, L.x2), x1 = Math.max(p.x, L.x1, L.x2, S.imgW * 0.12);
  let y0 = Math.min(p.y, 0), y1 = Math.max(p.y, S.imgH);
  const w = Math.max(x1 - x0, 1), h = Math.max(y1 - y0, 1);
  x0 -= w * pad; x1 += w * pad; y0 -= h * pad; y1 += h * pad;
  const k = clamp(Math.min(S.cssW / (x1 - x0), S.cssH / (y1 - y0)), 0.004, 80);
  centreOn((x0 + x1) / 2, (y0 + y1) / 2, k);
}

/* ------------------------------------------------------------- geometry */

function ringLine(i) {
  const r = S.core.rings[i];
  const xc = (r.zpos - 1) / S.step;
  const half = (S.imgH - 1) / 2;
  const dx = half * Math.tan(r.theta);
  return { xc: xc, yc: half, theta: r.theta,
           x1: xc - dx, y1: 0, x2: xc + dx, y2: S.imgH - 1 };
}

/** Signed distance from world point p to boundary i, positive towards +x
 *  (the bark side). The pith offset is the negative of it. */
function signedTo(i, p) {
  const L = ringLine(i);
  const nx = Math.cos(L.theta), ny = -Math.sin(L.theta);
  return (p.x - L.xc) * nx + (p.y - L.yc) * ny;
}

function footOn(i, p) {
  const L = ringLine(i);
  const nx = Math.cos(L.theta), ny = -Math.sin(L.theta);
  const s = signedTo(i, p);
  return { x: p.x - s * nx, y: p.y - s * ny };
}

/** Everything the readout and the save payload need for a candidate pith. */
function measure(p) {
  if (!S.core || !S.core.rings.length || !S.bitmap) return null;
  const L = ringLine(0);
  const offWorld = -signedTo(0, p);                       // + = inward of ring 0
  const dx = p.x - L.xc, dy = p.y - L.yc;
  const euclidWorld = Math.hypot(dx, dy) * (offWorld < 0 ? -1 : 1);
  const mm = S.mmPerWorld;
  return {
    perpWorld: offWorld,
    perp_mm: mm ? offWorld * mm : null,
    euclid_mm: mm ? euclidWorld * mm : null,
    foot: footOn(0, p),
    pith_x_px: p.x * S.step + 1,
    pith_y_px: p.y * S.step + 1,
  };
}

/* ---------------------------------------------------------------- canvas */

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(r.width * dpr));
  canvas.height = Math.max(1, Math.round(r.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  S.cssW = r.width; S.cssH = r.height; S.dpr = dpr;
  draw();
}

const w2sx = (x) => x * S.view.k + S.view.tx;
const w2sy = (y) => y * S.view.k + S.view.ty;
const s2wx = (x) => (x - S.view.tx) / S.view.k;
const s2wy = (y) => (y - S.view.ty) / S.view.k;

function resetView() {
  if (!S.bitmap) return;
  const inner = S.core.rings.length ? ringLine(0).xc : 0;
  const span = Math.max(S.imgH * 4.5, 400);
  const k = Math.min(S.cssW / span, S.cssH / (S.imgH * 1.3));
  S.view.k = clamp(k, 0.005, 60);
  centreOnX(inner, 0.68);
  S.view.ty = S.cssH / 2 - (S.imgH / 2) * S.view.k;
  draw();
}

function centreOnX(wx, frac) {
  S.view.tx = S.cssW * frac - wx * S.view.k;
}

function centreOn(wx, wy, k) {
  S.view.k = k;
  S.view.tx = S.cssW / 2 - wx * k;
  S.view.ty = S.cssH / 2 - wy * k;
}

function zoomAt(sx, sy, factor) {
  const k0 = S.view.k;
  const k1 = clamp(k0 * factor, 0.004, 80);
  if (k1 === k0) return;
  const wx = s2wx(sx), wy = s2wy(sy);
  S.view.k = k1;
  S.view.tx = sx - wx * k1;
  S.view.ty = sy - wy * k1;
  draw();
}

function draw() {
  if (!S.cssW) return;
  ctx.save();
  ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  ctx.fillStyle = '#0a0d12';
  ctx.fillRect(0, 0, S.cssW, S.cssH);

  if (S.bitmap) {
    drawImageLayer();
    if (S.showRings) drawRings();
    drawEstimate();
  }
  ctx.restore();
  updateHud();
}

function drawImageLayer() {
  const k = S.view.k;
  ctx.save();
  ctx.imageSmoothingEnabled = k < 1.5;
  ctx.setTransform(S.dpr * k, 0, 0, S.dpr * k, S.dpr * S.view.tx, S.dpr * S.view.ty);
  ctx.drawImage(S.bitmap, 0, 0);
  ctx.restore();

  // the wood ends here; make that boundary explicit
  ctx.strokeStyle = 'rgba(255,255,255,.14)';
  ctx.lineWidth = 1;
  ctx.strokeRect(w2sx(0), w2sy(0), S.imgW * k, S.imgH * k);
}

function drawRings() {
  const n = S.core.rings.length;
  if (!n) return;
  const k = S.view.k;
  const left = s2wx(-40), right = s2wx(S.cssW + 40);

  ctx.save();
  // A steeply tilted boundary reaches past the edge of the wood; RingIndicator
  // clips it to the image and so does this.
  ctx.beginPath();
  ctx.rect(w2sx(0), w2sy(0), S.imgW * k, S.imgH * k);
  ctx.clip();
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(76,201,240,.55)';
  ctx.beginPath();
  let shown = [];
  for (let i = 1; i < n; i++) {
    const L = ringLine(i);
    if (Math.max(L.x1, L.x2) < left || Math.min(L.x1, L.x2) > right) continue;
    ctx.moveTo(w2sx(L.x1), w2sy(L.y1));
    ctx.lineTo(w2sx(L.x2), w2sy(L.y2));
    shown.push(i);
  }
  ctx.stroke();

  // the innermost boundary — the one every distance is measured from
  const L0 = ringLine(0);
  ctx.strokeStyle = '#ffd166';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(w2sx(L0.x1), w2sy(L0.y1));
  ctx.lineTo(w2sx(L0.x2), w2sy(L0.y2));
  ctx.stroke();

  ctx.restore();
  if (S.showLabels && S.core.rings.length > 1) {
    ctx.save();
    drawYearLabels(shown, k);
    ctx.restore();
  }
}

function drawYearLabels(shown, k) {
  const spacing = S.core.n_boundaries > 1
    ? Math.abs(ringLine(1).xc - ringLine(0).xc) * k : 0;
  if (spacing < 26) return;
  ctx.font = '10px ui-monospace, monospace';
  ctx.fillStyle = 'rgba(230,235,242,.75)';
  ctx.textAlign = 'center';
  const y = w2sy(0) - 4;
  shown.concat([0]).forEach((i) => {
    const yr = S.core.rings[i].year;
    if (yr === null || yr === undefined) return;
    ctx.fillText(String(yr), w2sx(ringLine(i).xc), y);
  });
}

function drawEstimate() {
  if (S.method !== 'concentric') return;
  const p = S.pith || S.hover;
  if (!p) return;
  const placed = !!S.pith;
  const m = measure(p);
  if (!m) return;

  const nC = Math.min(S.nCircles, S.core.rings.length);
  const cx = w2sx(p.x), cy = w2sy(p.y), k = S.view.k;

  ctx.save();
  // concentric circles, one through each of the innermost boundaries
  for (let i = 0; i < nC; i++) {
    const r = Math.abs(signedTo(i, p)) * k;
    if (r < 1 || r > 40000) continue;
    const t = 1 - i / Math.max(nC, 2);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = i === 0
      ? (placed ? 'rgba(74,217,145,.95)' : 'rgba(255,209,102,.95)')
      : 'rgba(76,201,240,' + (0.18 + 0.42 * t).toFixed(3) + ')';
    ctx.lineWidth = i === 0 ? 1.8 : 1;
    ctx.stroke();
  }

  // radius from the estimated pith to the innermost boundary
  const f = m.foot;
  ctx.setLineDash([6, 5]);
  ctx.strokeStyle = placed ? 'rgba(74,217,145,.95)' : 'rgba(255,209,102,.9)';
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(w2sx(f.x), w2sy(f.y));
  ctx.stroke();
  ctx.setLineDash([]);

  // centre cross
  const col = placed ? '#4ad991' : '#ffd166';
  ctx.strokeStyle = col; ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.moveTo(cx - 9, cy); ctx.lineTo(cx + 9, cy);
  ctx.moveTo(cx, cy - 9); ctx.lineTo(cx, cy + 9);
  ctx.stroke();
  if (placed) {
    ctx.fillStyle = 'rgba(74,217,145,.22)';
    ctx.beginPath(); ctx.arc(cx, cy, 5, 0, Math.PI * 2); ctx.fill();
  }

  // distance label on the radius
  if (m.perp_mm !== null) {
    const mx = (cx + w2sx(f.x)) / 2, my = (cy + w2sy(f.y)) / 2;
    const txt = fmt(m.perp_mm, 2) + ' mm';
    ctx.font = '600 12px ui-monospace, monospace';
    const w = ctx.measureText(txt).width;
    ctx.fillStyle = 'rgba(10,13,18,.82)';
    roundRect(mx - w / 2 - 6, my - 18, w + 12, 17, 5);
    ctx.fill();
    ctx.fillStyle = col;
    ctx.textAlign = 'center';
    ctx.fillText(txt, mx, my - 5.5);
  }
  ctx.restore();
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* -------------------------------------------------------------- hud text */

function updateHud() {
  $('zoomLabel').textContent = S.view.k ? Math.round(S.view.k * 100) + '%' : '—';
  updateScalebar();
  updateOffscreenHint();

  const ro = $('readout'), rv = $('readoutValue'), rs = $('readoutSub');
  ro.classList.remove('placed', 'bad');

  if (S.method === 'indicated') {
    rv.textContent = '0.00 mm';
    rs.textContent = 'pith indicated on the core';
    ro.classList.add('placed');
    return;
  }
  if (S.method === 'geometric') {
    const g = computeGeometric();
    rv.textContent = g.ok ? fmt(g.dist, 2) + ' mm' : '— mm';
    rs.textContent = g.ok ? 'diameter & bark' : (g.why || 'fill in the fields below');
    if (g.ok) ro.classList.add(g.dist < 0 ? 'bad' : 'placed');
    return;
  }
  const p = S.pith || S.hover;
  const m = p ? measure(p) : null;
  if (!m || m.perp_mm === null) {
    rv.textContent = '— mm';
    rs.textContent = S.mmPerWorld ? 'move the cursor into the wood'
                                  : 'no pixel size in _ringwidth.txt';
    setMeasures(null);
    return;
  }
  rv.textContent = fmt(m.perp_mm, 2) + ' mm';
  rs.textContent = (S.pith ? 'placed' : 'hovering') + ' · ' +
                   fmt(m.euclid_mm, 2) + ' mm centre-to-centre';
  const cls = m.perp_mm < 0 ? 'bad' : (S.pith ? 'placed' : '');
  if (cls) ro.classList.add(cls);
  setMeasures(m);
}

/** Panning far into the empty space where the pith sits is easy, and an all
 *  black stage looks like a broken tool rather than a scrolled one. */
function updateOffscreenHint() {
  const hint = $('offscreenHint');
  if (!S.bitmap) { hint.classList.add('hidden'); return; }
  const x0 = w2sx(0), x1 = w2sx(S.imgW), y0 = w2sy(0), y1 = w2sy(S.imgH);
  const off = x1 < 0 || x0 > S.cssW || y1 < 0 || y0 > S.cssH;
  hint.classList.toggle('hidden', !off);
}

function setMeasures(m) {
  $('mPerp').textContent = m ? fmt(m.perp_mm, 2) + ' mm' : '—';
  $('mEuclid').textContent = m ? fmt(m.euclid_mm, 2) + ' mm' : '—';
  $('mPos').textContent = m ? Math.round(m.pith_x_px) + ', ' + Math.round(m.pith_y_px) + ' px' : '—';
}

function updateScalebar() {
  const el = $('scalebar');
  if (!S.mmPerWorld || !S.bitmap) { el.style.display = 'none'; return; }
  el.style.display = '';
  const pxPerMm = S.view.k / S.mmPerWorld;
  const nice = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200];
  let mm = nice[nice.length - 1];
  for (const c of nice) { if (c * pxPerMm >= 70) { mm = c; break; } }
  el.style.width = (mm * pxPerMm) + 'px';
  $('scalebarLabel').textContent = (mm < 1 ? mm.toFixed(1) : mm) + ' mm';
}

/* ------------------------------------------------------------ geometric */

/** ΣRW to use for case 3: summed across the opened core's sections unless the
 *  operator asked to use only the one that is open. A core with no sections
 *  (S.core.sections is null) just uses its own accum_rw_mm either way. */
function sectionsAccum() {
  const core = S.core;
  if (!core || !core.sections) return { value: core ? core.accum_rw_mm : null, summed: false };
  if (S.onlyOpenedSection) return { value: core.accum_rw_mm, summed: false };
  return { value: core.accum_rw_mm_sum, summed: true };
}

function computeGeometric() {
  const sr = parseFloat($('sr').value);
  const bark = parseFloat($('bark').value);
  const raw = parseFloat($('diameter').value);
  const gap = parseFloat($('outerGap').value) || 0;
  const acc = sectionsAccum();
  const accum = acc.value;

  if (accum === null || accum === undefined)
    return { ok: false, why: 'no pixel size, so ΣRW cannot be converted to mm' };
  if (!isFinite(sr) || sr < 0 || sr >= 0.5)
    return { ok: false,
             why: $('species').value ? 'set a radial shrinkage'
                                     : 'choose a species' };
  if (!isFinite(bark) || bark < 0) return { ok: false, why: 'enter the bark thickness' };
  if (!isFinite(raw) || raw <= 0) return { ok: false, why: 'enter the tree ' + S.diamMode };

  const diameter = S.diamMode === 'circumference' ? raw / Math.PI : raw;
  const green = accum / (1 - sr);
  const barkless = diameter / 2 - bark;
  return { ok: true, sr, bark, diameter, gap, accum, green, barkless, summed: acc.summed,
           dist: barkless - green - gap };
}

function updateGeoWork() {
  const box = $('geoWork');
  const sections = S.core && S.core.sections;
  $('sectionsRow').classList.toggle('hidden', !sections);

  const g = computeGeometric();
  if (!g.ok) {
    box.innerHTML = '<span class="err">' + esc(g.why) + '</span>';
    updateHud(); updateSave();
    return;
  }
  const srcRow = $('species').selectedOptions[0];
  const src = srcRow ? srcRow.dataset.source : '';
  const manual = srcRow && Math.abs(parseFloat(srcRow.dataset.sr) - g.sr) > 1e-9;

  // A core scanned in sections has its ring width spread across several
  // files; show which ones went into ΣRW rather than a bare number, since
  // that composition is easy to get wrong silently.
  let accumLine = 'ΣRW oven-dry     = ' + fmt(g.accum, 2) + ' mm';
  if (sections && g.summed) {
    const parts = sections.map((s) =>
      s.accum_rw_mm === null ? '? (' + s.stem + ')' : fmt(s.accum_rw_mm, 2));
    accumLine = 'ΣRW oven-dry     = ' + parts.join(' + ') + ' = ' + fmt(g.accum, 2) +
      ' mm   (' + sections.map((s) => s.stem).join(' + ') + ')';
  } else if (sections) {
    accumLine += '   (' + S.core.stem + ' only — other sections of this core excluded)';
  }

  box.innerHTML =
    'barkless radius  = ' + fmt(g.diameter, 1) + ' / 2 − ' + fmt(g.bark, 1) +
      '  = ' + fmt(g.barkless, 2) + ' mm\n' +
    accumLine + '\n' +
    'ΣRW green        = ' + fmt(g.accum, 2) + ' / (1 − ' + fmt(g.sr, 3) + ')' +
      '  = ' + fmt(g.green, 2) + ' mm\n' +
    (g.gap ? 'unmeasured wood  = ' + fmt(g.gap, 2) + ' mm\n' : '') +
    'distance to pith = <span class="res">' + fmt(g.dist, 2) + ' mm</span>' +
    (g.dist < 0 ? '   <span class="err">← negative: check diameter and bark</span>' : '') +
    '\nS' + 'ᵣ' + ' source       = ' + esc(manual ? 'manual override' : (src || '—'));
  updateHud(); updateSave();
}

/* ----------------------------------------------------------- species UI */

function buildSpeciesSelect() {
  const sel = $('species');
  sel.innerHTML = '';
  // No species is guessed from the core name any more, so the list opens on a
  // placeholder that carries no Sr. computeGeometric() gates on Sr, which is
  // what stops case 3 being saved against whatever happened to be first.
  const ph = document.createElement('option');
  ph.value = '';
  ph.textContent = '— select species —';
  sel.appendChild(ph);
  S.session.species.forEach((sp) => {
    const o = document.createElement('option');
    o.value = sp.name;
    o.textContent = sp.name + '  (Sr ' + sp.sr.toFixed(3) + ')' +
                    (/verify/i.test(sp.source) ? '  ⚠' : '');
    o.dataset.sr = sp.sr;
    o.dataset.source = sp.source;
    sel.appendChild(o);
  });
}

/** Show a species that is no longer in the table, e.g. one written by an
 *  earlier version, so revisiting a tree reports what was actually used. */
function ensureSpeciesOption(name) {
  const sel = $('species');
  if (!name || [...sel.options].some((o) => o.value === name)) return;
  const o = document.createElement('option');
  o.value = name;
  o.textContent = name + '  (not in the current table)';
  o.dataset.source = 'not in the current species table';
  sel.appendChild(o);
}

function syncSrFromSpecies() {
  const o = $('species').selectedOptions[0];
  const sr = o && o.dataset.sr;
  $('sr').value = sr === undefined || sr === null || sr === ''
    ? '' : parseFloat(sr).toFixed(3);
}

function renderSpeciesTable() {
  const t = $('speciesTable');
  t.innerHTML = '<tr><th>Species (Latin name)</th><th>S' + 'ᵣ' +
                '</th><th>Source</th><th></th></tr>';
  S.session.species.forEach((sp, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td><input class="nm" value=""></td>' +
      '<td><input class="sr" type="number" step="0.001" min="0" max="0.499"></td>' +
      '<td><input class="src" value=""></td>' +
      '<td class="rm"><button title="Remove">✕</button></td>';
    tr.querySelector('.nm').value = sp.name;
    tr.querySelector('.sr').value = sp.sr.toFixed(3);
    tr.querySelector('.src').value = sp.source || '';
    tr.querySelector('.rm button').onclick = () => tr.remove();
    t.appendChild(tr);
  });
}

async function saveSpeciesTable() {
  const rows = [...$('speciesTable').querySelectorAll('tr')].slice(1).map((tr) => ({
    name: tr.querySelector('.nm').value.trim(),
    sr: parseFloat(tr.querySelector('.sr').value),
    source: tr.querySelector('.src').value.trim() || 'user supplied',
  })).filter((r) => r.name && isFinite(r.sr));
  const r = await fetch('/api/species', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ species: rows }),
  });
  const j = await r.json();
  if (!r.ok) return toast(j.error || 'could not save the species table', 'err');
  const keep = $('species').value;
  S.session.species = j.species;
  buildSpeciesSelect();
  ensureSpeciesOption(keep);
  $('species').value = [...$('species').options].some((o) => o.value === keep)
    ? keep : '';
  syncSrFromSpecies();
  updateGeoWork();
  closeModal('speciesModal');
  toast('Species table saved to pith_species_shrinkage.csv');
}

/* ---------------------------------------------------------------- saving */

function updateSave() {
  let ok = false;
  if (!S.core) ok = false;
  else if (S.method === 'indicated') ok = true;
  else if (S.method === 'geometric') ok = computeGeometric().ok;
  else ok = !!(S.pith && S.mmPerWorld);
  $('btnSave').disabled = !ok;
}

async function save(advance) {
  if (!S.core || $('btnSave').disabled) return;
  const body = {
    tree: S.core.tree, stem: S.core.stem, method: S.method,
    notes: $('notes').value,
  };
  if (S.method === 'concentric') {
    const m = measure(S.pith);
    Object.assign(body, { perp_mm: m.perp_mm, euclid_mm: m.euclid_mm,
                          pith_x_px: m.pith_x_px, pith_y_px: m.pith_y_px });
  } else if (S.method === 'geometric') {
    // Species is only meaningful -- and only looked at by the operator -- for
    // this case, so it is the only one that writes it into the saved row.
    const g = computeGeometric();
    const o = $('species').selectedOptions[0];
    const manual = o && Math.abs(parseFloat(o.dataset.sr) - g.sr) > 1e-9;
    Object.assign(body, {
      species: $('species').value,
      sr: g.sr, bark_mm: g.bark, diameter_mm: g.diameter,
      diameter_input: S.diamMode, outer_gap_mm: g.gap,
      sr_source: manual ? 'manual override' : (o ? o.dataset.source : ''),
      sum_sections: !S.onlyOpenedSection,
    });
  }
  $('btnSave').disabled = true;
  let j;
  try {
    const r = await fetch('/api/result', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    j = await r.json();
    if (!r.ok) throw new Error(j.error || 'the server refused the result');
  } catch (e) {
    updateSave();
    return toast('Not saved: ' + e.message, 'err', 6000);
  }

  const t = S.trees[S.index];
  t.done = true;
  t.result = { method: j.row.method, distance_mm: j.row.distance_mm };
  renderTreeList(); renderProgress();
  if (j.warning) toast(j.warning, 'warn', 8000);
  else if (j.row.flag) toast('Saved with a flag: ' + j.row.flag, 'warn', 7000);
  else toast('Saved ' + t.tree + ' — ' + fmt(j.row.distance_mm, 2) + ' mm');

  if (advance) {
    if (S.index < S.trees.length - 1) await goto(S.index + 1);
    else { updateSave(); toast('That was the last tree. ' +
      S.trees.filter((x) => x.done).length + ' of ' + S.trees.length + ' done.', 'warn', 6000); }
  } else updateSave();
}

/* ------------------------------------------------------------- methods */

function setMethod(m) {
  S.method = m;
  document.querySelectorAll('.tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.method === m));
  document.querySelectorAll('.method-pane').forEach((p) =>
    p.classList.toggle('hidden', p.dataset.pane !== m));
  canvas.style.cursor = m === 'concentric' ? 'crosshair' : 'grab';
  updateSave(); draw();
}

function setDiamMode(mode) {
  S.diamMode = mode;
  $('diamLabel').textContent = mode === 'circumference'
    ? 'Circumference over bark (mm)' : 'Diameter over bark (mm)';
  $('diameter').placeholder = mode === 'circumference' ? 'e.g. 1320' : 'e.g. 420';
  updateGeoWork();
}

/* -------------------------------------------------------------- pointer */

const NARROW = 900;
const isNarrow = () => window.innerWidth <= NARROW;

function syncSidebar() {
  $('sidebar').classList.toggle('hidden', isNarrow());
}

function wireUI() {
  syncSidebar();
  let wasNarrow = isNarrow();
  window.addEventListener('resize', () => {
    if (isNarrow() !== wasNarrow) { wasNarrow = isNarrow(); syncSidebar(); }
    resizeCanvas();
  });

  // ---- canvas interaction
  const pointers = new Map();
  let panning = false, moved = 0, last = null, pinch = null;

  canvas.addEventListener('pointerdown', (e) => {
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y),
                cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2, k: S.view.k };
    }
    panning = false; moved = 0; last = { x: e.offsetX, y: e.offsetY };
  });

  canvas.addEventListener('pointermove', (e) => {
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.offsetX, y: e.offsetY });

    if (pointers.size === 2 && pinch) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinch.d > 4) zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, (d / pinch.d) * (pinch.k / S.view.k));
      pinch.d = d; pinch.k = S.view.k;
      return;
    }

    if (last && pointers.size === 1) {
      const dx = e.offsetX - last.x, dy = e.offsetY - last.y;
      moved += Math.abs(dx) + Math.abs(dy);
      if (moved > 4) {
        panning = true;
        canvas.classList.add('panning');
        S.view.tx += dx; S.view.ty += dy;
        last = { x: e.offsetX, y: e.offsetY };
        draw();
        return;
      }
    }
    if (!pointers.size) {
      S.hover = { x: s2wx(e.offsetX), y: s2wy(e.offsetY) };
      if (!S.pith && S.method === 'concentric') draw(); else updateHud();
    }
  });

  const endPointer = (e) => {
    const wasPanning = panning;
    pointers.delete(e.pointerId);
    if (!pointers.size) { panning = false; pinch = null; last = null;
                          canvas.classList.remove('panning'); }
    if (!wasPanning && moved <= 4 && e.button === 0 && S.method === 'concentric' && S.bitmap) {
      S.pith = { x: s2wx(e.offsetX), y: s2wy(e.offsetY) };
      updateSave(); draw();
    }
  };
  canvas.addEventListener('pointerup', endPointer);
  canvas.addEventListener('pointercancel', (e) => { pointers.delete(e.pointerId);
    panning = false; pinch = null; last = null; canvas.classList.remove('panning'); });
  canvas.addEventListener('pointerleave', () => { S.hover = null; draw(); });

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    zoomAt(e.offsetX, e.offsetY, Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0016)));
  }, { passive: false });

  canvas.addEventListener('contextmenu', (e) => e.preventDefault());

  // ---- hud controls
  $('nCircles').addEventListener('input', (e) => {
    S.nCircles = parseInt(e.target.value, 10);
    $('nCirclesOut').value = S.nCircles;
    draw();
  });
  $('showRings').addEventListener('change', (e) => { S.showRings = e.target.checked; draw(); });
  $('showLabels').addEventListener('change', (e) => { S.showLabels = e.target.checked; draw(); });

  let climTimer = null;
  const climChanged = () => {
    clearTimeout(climTimer);
    climTimer = setTimeout(async () => {
      const lo = parseFloat($('climLo').value), hi = parseFloat($('climHi').value);
      if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return;
      S.clim = { lo, hi };
      if (!S.core || !S.core.has_image) return;
      try { await loadImage(); draw(); }
      catch (err) { toast('Could not re-render the preview: ' + err.message, 'err'); }
    }, 350);
  };
  $('climLo').addEventListener('input', climChanged);
  $('climHi').addEventListener('input', climChanged);

  $('btnZoomIn').onclick = () => zoomAt(S.cssW / 2, S.cssH / 2, 1.35);
  $('btnZoomOut').onclick = () => zoomAt(S.cssW / 2, S.cssH / 2, 1 / 1.35);
  $('btnFit').onclick = () => resetView();
  $('btnHintReset').onclick = () => resetView();

  // ---- panel
  document.querySelectorAll('.tab').forEach((b) => {
    b.onclick = () => setMethod(b.dataset.method);
  });
  ['sr', 'bark', 'diameter', 'outerGap'].forEach((id) =>
    $(id).addEventListener('input', updateGeoWork));
  $('onlyOpenedSection').addEventListener('change', (e) => {
    S.onlyOpenedSection = e.target.checked;
    updateGeoWork();
  });
  $('species').addEventListener('change', () => {
    S.lastSpecies = $('species').value || null;
    syncSrFromSpecies();
    updateGeoWork();
  });
  $('btnDiamMode').onclick = () =>
    setDiamMode(S.diamMode === 'diameter' ? 'circumference' : 'diameter');

  $('btnSave').onclick = () => save(true);
  $('btnSkip').onclick = () => goto(S.index + 1);
  $('btnPrev').onclick = () => goto(S.index - 1);

  // ---- chrome
  $('btnSidebar').onclick = () => $('sidebar').classList.toggle('hidden');
  $('treeFilter').addEventListener('input', renderTreeList);
  $('btnRescan').onclick = async () => {
    const tree = S.core && S.core.tree;
    await loadSession(true);
    const i = S.trees.findIndex((t) => t.tree === tree);
    await goto(i >= 0 ? i : 0);
    toast('Folder re-read: ' + S.trees.length + ' trees');
  };
  $('btnManual').onclick = () => openModal('manualModal');
  $('btnSpecies').onclick = () => { renderSpeciesTable(); openModal('speciesModal'); };
  $('btnAddSpecies').onclick = () => {
    S.session.species.push({ name: '', sr: 0.05, source: 'user supplied' });
    renderSpeciesTable();
  };
  $('btnSaveSpecies').onclick = saveSpeciesTable;
  document.querySelectorAll('[data-close]').forEach((b) =>
    b.onclick = () => closeModal(b.dataset.close));
  document.querySelectorAll('.modal').forEach((m) =>
    m.addEventListener('click', (e) => { if (e.target === m) closeModal(m.id); }));

  // ---- keys
  document.addEventListener('keydown', onKey);
}

const TYPING = /^(text|search|number|email|password|tel|url)$/;

function onKey(e) {
  const t = e.target;
  const typing = t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' ||
                 (t.tagName === 'INPUT' && TYPING.test(t.type));
  if (typing) {
    if (e.key === 'Enter') t.blur();
    return;
  }
  const open = document.querySelector('.modal:not(.hidden)');
  if (e.key === 'Escape') {
    if (open) return closeModal(open.id);
    S.pith = null; updateSave(); draw(); return;
  }
  if (open) return;

  switch (e.key) {
    case '1': setMethod('concentric'); break;
    case '2': setMethod('indicated'); break;
    case '3': setMethod('geometric'); break;
    case 'Enter': save(true); break;
    case 'r': case 'R': resetView(); break;
    case '+': case '=': zoomAt(S.cssW / 2, S.cssH / 2, 1.35); break;
    case '-': case '_': zoomAt(S.cssW / 2, S.cssH / 2, 1 / 1.35); break;
    case '[': setCircles(S.nCircles - 1); break;
    case ']': setCircles(S.nCircles + 1); break;
    case 'ArrowLeft':  S.view.tx += 60; draw(); break;
    case 'ArrowRight': S.view.tx -= 60; draw(); break;
    case 'ArrowUp':    S.view.ty += 60; draw(); break;
    case 'ArrowDown':  S.view.ty -= 60; draw(); break;
    case 'n': case 'N': goto(S.index + 1); break;
    case 'p': case 'P': goto(S.index - 1); break;
    case '?': openModal('manualModal'); break;
    default: return;
  }
  e.preventDefault();
}

function setCircles(n) {
  S.nCircles = clamp(n, 1, 40);
  $('nCircles').value = S.nCircles;
  $('nCirclesOut').value = S.nCircles;
  draw();
}

/* --------------------------------------------------------------- chrome */

function openModal(id) { $(id).classList.remove('hidden'); }
function closeModal(id) { $(id).classList.add('hidden'); }

function showStageMessage(html) {
  const el = $('stageMessage');
  el.innerHTML = html;
  el.classList.remove('hidden');
}
function hideStageMessage() { $('stageMessage').classList.add('hidden'); }

let toastTimer = null;
function toast(msg, kind, ms) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast' + (kind ? ' ' + kind : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), ms || 3000);
}

function fmt(v, n) {
  return (v === null || v === undefined || !isFinite(v)) ? '—' : Number(v).toFixed(n);
}
function esc(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}
