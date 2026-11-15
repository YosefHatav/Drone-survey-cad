/* Drone Survey CAD – client */
const ACI = {1:'#ff2020',2:'#ffd400',3:'#22c55e',4:'#00e5ff',5:'#2f6bff',6:'#ff3dff',7:'#ffffff',8:'#9aa0a6',
  9:'#c8ccd0',30:'#ff8c1a',34:'#c08040',82:'#7bd36a',94:'#2e8b3a',140:'#3aa0ff',150:'#3060ff',252:'#bbbbbb'};
const GEO_HE = {point:'נקודה', line:'קו', polygon:'פוליגון'};
const SHAPE = {point:'CircleMarker', line:'Line', polygon:'Polygon'};

let codes = [], codeMap = {}, proj = null, map = null;
let orthoLayer, demLayer, featLayer, genLayer, ctrlLayer;
let activeCode = null, selected = null, measuring = null;
const featLayers = {};      // id -> leaflet layer
const undoStack = [];

const $ = s => document.querySelector(s);
const api = async (url, opts = {}) => {
  const r = await fetch(url, opts);
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r;
};
const jsonOpts = (method, body) => ({method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
const status = (t, ms = 4000) => { $('#status').textContent = t; if (ms) setTimeout(() => { if ($('#status').textContent === t) $('#status').textContent = ''; }, ms); };
const color = c => ACI[c] || '#ff00ff';
const fmt = (v, d = 2) => v === null || v === undefined ? '—' : Number(v).toFixed(d);

/* ------------------------------------------------------------ init */
async function init() {
  codes = await api('/api/codes');
  codes.forEach(c => codeMap[c.code] = c);
  renderCodes();
  await refreshProjects();
  const last = localStorageGet('dsc.project');
  if (last && [...$('#projSelect').options].some(o => o.value === last)) { $('#projSelect').value = last; openProject(last); }
  bindUI();
}
function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

async function refreshProjects() {
  const list = await api('/api/projects');
  const sel = $('#projSelect');
  sel.innerHTML = '<option value="">— בחר פרויקט —</option>' + list.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  if (proj) sel.value = proj.id;
}
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function renderCodes() {
  const q = $('#codeSearch').value.trim().toLowerCase();
  $('#codeList').innerHTML = codes.filter(c => !q || c.label.toLowerCase().includes(q) || c.code.toLowerCase().includes(q) || c.layer.toLowerCase().includes(q))
    .map(c => { const i = codes.indexOf(c);
      return `<div class="code ${activeCode === c.code ? 'active' : ''}" data-code="${c.code}">
        <span class="sw" style="background:${color(c.color)}"></span><span>${esc(c.label)}</span>
        ${i < 9 ? `<kbd>${i + 1}</kbd>` : ''}<span class="geo">${c.code} · ${GEO_HE[c.geom]}</span></div>`; }).join('');
}

/* ------------------------------------------------------------ project / map */
async function openProject(pid) {
  proj = await api(`/api/projects/${pid}`);
  localStorageSet('dsc.project', pid);
  buildMap();
  fillMeta();
  renderRasterInfo();
  renderControl(await api(`/api/projects/${pid}/accuracy`));
  renderGenerated(await api(`/api/projects/${pid}/generated`));
  ['#btnDxf', '#btnZip'].forEach(s => $(s).disabled = false);
  codes = proj.codes; codeMap = {}; codes.forEach(c => codeMap[c.code] = c); renderCodes();
  renderCad();
}

function buildMap() {
  if (map) { map.remove(); map = null; }
  Object.keys(featLayers).forEach(k => delete featLayers[k]);
  const b = proj.bounds;
  $('#empty').style.display = b ? 'none' : 'block';
  const left = b ? b[0] : 0, top = b ? b[3] : 0;
  const crs = L.extend({}, L.CRS.Simple, { transformation: new L.Transformation(1, -left, -1, top) });
  map = L.map('map', { crs, minZoom: -8, maxZoom: 8, zoomSnap: 0.25, attributionControl: false, doubleClickZoom: false });
  const res = proj.rasters.ortho ? proj.rasters.ortho.res[0] : 0.05;
  const nativeMax = Math.ceil(Math.log2(1 / res));
  const tileOpts = { minZoom: -8, maxZoom: 8, maxNativeZoom: nativeMax, minNativeZoom: -8, tileSize: 256, noWrap: true,
    bounds: b ? [[b[1], b[0]], [b[3], b[2]]] : undefined };
  orthoLayer = L.tileLayer(`/api/projects/${proj.id}/tiles/ortho/{z}/{x}/{y}.png?v=${Date.now()}`, tileOpts);
  const demKind = proj.rasters.dtm ? 'dtm' : 'dsm';
  demLayer = L.tileLayer(`/api/projects/${proj.id}/tiles/${demKind}/{z}/{x}/{y}.png?v=${Date.now()}`, {...tileOpts, opacity: 0.6});
  if (proj.rasters.ortho && $('#lyOrtho').checked) orthoLayer.addTo(map);
  if (proj.rasters[demKind] && $('#lyDem').checked) demLayer.addTo(map);
  genLayer = L.layerGroup(); if ($('#lyGen').checked) genLayer.addTo(map);
  ctrlLayer = L.layerGroup(); if ($('#lyCtrl').checked) ctrlLayer.addTo(map);
  featLayer = L.featureGroup().addTo(map);
  if (b) map.fitBounds([[b[1], b[0]], [b[3], b[2]]]); else map.setView([0, 0], 0);

  map.pm.setGlobalOptions({ snappable: true, snapDistance: 15, allowSelfIntersection: true, finishOn: 'dblclick',
    templineStyle: { color: '#00e5ff' }, hintlineStyle: { color: '#00e5ff', dashArray: '4 4' } });
  map.on('pm:create', onCreate);
  map.on('mousemove', onMouseMove);
  map.on('click', onMapClick);
  map.on('zoomend', updateLabels);
  proj.features.forEach(addFeatureLayer);
  updateSummary();
}

let elevTimer = null;
function onMouseMove(e) {
  const E = e.latlng.lng, N = e.latlng.lat;
  $('#coords').textContent = `E: ${E.toFixed(3)}  N: ${N.toFixed(3)}  Z: …`;
  clearTimeout(elevTimer);
  elevTimer = setTimeout(async () => {
    try { const z = await api(`/api/projects/${proj.id}/elev?e=${E}&n=${N}`);
      $('#coords').textContent = `E: ${E.toFixed(3)}  N: ${N.toFixed(3)}  Z: ${fmt(z.ground, 3)}` + (z.top !== null && z.ground !== null && Math.abs(z.top - z.ground) > 0.05 ? `  (DSM ${fmt(z.top, 3)})` : '');
    } catch {}
  }, 150);
}

/* ------------------------------------------------------------ drawing */
function selectCode(code) {
  if (!map || !proj.bounds) { status('יש לטעון אורתופוטו קודם'); return; }
  map.pm.disableDraw();
  if (activeCode === code) { activeCode = null; renderCodes(); return; }
  activeCode = code;
  const c = codeMap[code];
  const style = { color: color(c.color), weight: 2, fillOpacity: 0.1 };
  map.pm.enableDraw(SHAPE[c.geom], { continueDrawing: true, pathOptions: style, templineStyle: style,
    ...(c.geom === 'point' ? { pathOptions: { ...style, radius: 5, fillOpacity: 0.9 } } : {}) });
  renderCodes();
  status(`משרטט: ${c.label} (${c.layer})`, 0);
}

async function onCreate(e) {
  const c = codeMap[activeCode];
  if (!c) { e.layer.remove(); return; }
  let coords;
  if (c.geom === 'point') { const ll = e.layer.getLatLng(); coords = [[ll.lng, ll.lat]]; }
  else { let lls = e.layer.getLatLngs(); if (c.geom === 'polygon') lls = lls[0]; coords = lls.map(ll => [ll.lng, ll.lat]); }
  e.layer.remove();
  try {
    const f = await api(`/api/projects/${proj.id}/features`, jsonOpts('POST', { code: c.code, geom: c.geom, coords }));
    proj.features.push(f); addFeatureLayer(f); undoStack.push(f.id); updateSummary(); refreshAreas();
    const zs = f.coords.map(p => p[2]).filter(z => z !== null);
    status(`נשמר ${c.label}` + (zs.length ? `  Z=${fmt(zs[0])}` : '  (ללא גובה – אין DSM)'));
  } catch (err) { status('שגיאה: ' + err.message); }
}

function addFeatureLayer(f) {
  const c = codeMap[f.code] || { color: 7, label: f.code };
  const style = { color: color(c.color), weight: 2.5, fillOpacity: 0.08 };
  let lyr;
  const lls = f.coords.map(p => [p[1], p[0]]);
  if (f.geom === 'point') lyr = L.circleMarker(lls[0], { ...style, radius: 5, fillOpacity: 0.9, fillColor: color(c.color) });
  else if (f.geom === 'polygon') lyr = L.polygon(lls, f.code === 'AREA' ? { ...style, fill: false, dashArray: '8 6' } : style);
  else lyr = L.polyline(lls, style);
  lyr._fid = f.id;
  const zs = f.coords.map(p => p[2]).filter(z => z !== null);
  lyr.bindTooltip(`${esc(c.label)} · ${f.code}<br>Z: ${zs.length ? fmt(Math.min(...zs)) + (zs.length > 1 ? ' – ' + fmt(Math.max(...zs)) : '') : '—'}`, { className: 'feat-tip', sticky: true });
  lyr.on('click', ev => { if (activeCode || measuring) return; L.DomEvent.stopPropagation(ev); selectFeature(f.id); });
  lyr.on('pm:edit', () => saveGeometry(f.id));
  lyr.addTo(featLayer);
  featLayers[f.id] = lyr;
}

function selectFeature(fid) {
  if (activeCode) return;
  if (selected && featLayers[selected]) { featLayers[selected].setStyle({ dashArray: null }); featLayers[selected].pm.disable(); }
  selected = fid;
  const f = proj.features.find(x => x.id === fid); if (!f) return;
  const c = codeMap[f.code];
  featLayers[fid].setStyle({ dashArray: '6 4' });
  const len = f.geom === 'point' ? '' : `<br>אורך: ${fmt(planLength(f.coords), 2)} מ'` + (f.geom === 'polygon' ? ` · שטח: ${fmt(area(f.coords), 2)} מ"ר` : '');
  $('#selInfo').classList.remove('hidden');
  $('#selInfo').innerHTML = `<b>${esc(c.label)}</b> (${f.code} / ${c.layer})${len}<br>
    <label>קוד <select id="selCode">${codes.filter(x => x.geom === f.geom).map(x => `<option value="${x.code}" ${x.code === f.code ? 'selected' : ''}>${esc(x.label)}</option>`).join('')}</select></label>
    <label>כיתוב <input id="selText" value="${esc(f.text || '')}"></label>
    <button id="selEdit">עריכת קודקודים (E)</button> <button id="selSave">שמור</button> <button id="selDel">מחק</button>`;
  $('#selEdit').onclick = () => featLayers[fid].pm.toggleEdit({ snappable: true });
  $('#selDel').onclick = () => deleteFeature(fid);
  $('#selSave').onclick = async () => { f.code = $('#selCode').value; f.text = $('#selText').value || null; await putFeature(f); };
}

async function putFeature(f) {
  const nf = await api(`/api/projects/${proj.id}/features`, jsonOpts('POST', f));
  Object.assign(f, nf);
  featLayers[f.id].remove(); addFeatureLayer(f); updateSummary(); status('עודכן');
}

async function saveGeometry(fid) {
  const f = proj.features.find(x => x.id === fid), lyr = featLayers[fid];
  if (f.geom === 'point') { const ll = lyr.getLatLng(); f.coords = [[ll.lng, ll.lat]]; }
  else { let lls = lyr.getLatLngs(); if (f.geom === 'polygon') lls = lls[0]; f.coords = lls.map(ll => [ll.lng, ll.lat]); }
  const nf = await api(`/api/projects/${proj.id}/features`, jsonOpts('POST', f));
  Object.assign(f, nf);
  status('הגיאומטריה עודכנה, גבהים נדגמו מחדש');
}

async function deleteFeature(fid) {
  await api(`/api/projects/${proj.id}/features/${fid}`, { method: 'DELETE' });
  proj.features = proj.features.filter(x => x.id !== fid);
  if (featLayers[fid]) { featLayers[fid].remove(); delete featLayers[fid]; }
  if (selected === fid) { selected = null; $('#selInfo').classList.add('hidden'); }
  updateSummary(); refreshAreas();
}

const planLength = cs => cs.slice(1).reduce((s, p, i) => s + Math.hypot(p[0] - cs[i][0], p[1] - cs[i][1]), 0);
const area = cs => Math.abs(cs.reduce((s, p, i) => { const q = cs[(i + 1) % cs.length]; return s + p[0] * q[1] - q[0] * p[1]; }, 0) / 2);

function updateSummary() {
  const counts = {};
  proj.features.forEach(f => counts[f.code] = (counts[f.code] || 0) + 1);
  $('#featCount').textContent = `(${proj.features.length})`;
  $('#featSummary').innerHTML = Object.entries(counts).map(([k, n]) => `${esc(codeMap[k]?.label || k)}: ${n}`).join(' · ') || 'עדיין לא שורטטו אובייקטים';
  refreshAreas();
}
function refreshAreas() {
  const sel = $('#genArea'), cur = sel.value;
  sel.innerHTML = '<option value="">כל השטח</option>' + proj.features.filter(f => f.geom === 'polygon')
    .map(f => `<option value="${f.id}">${esc(codeMap[f.code]?.label)} · ${fmt(area(f.coords), 0)} מ"ר</option>`).join('');
  sel.value = cur;
  const firstArea = proj.features.find(f => f.code === 'AREA');
  if (!cur && firstArea) sel.value = firstArea.id;
}

/* ------------------------------------------------------------ control points */
function renderControl(acc) {
  ctrlLayer && ctrlLayer.clearLayers();
  const fmtA = (a, t) => `<b>${t}</b>: ${a.count} נק' · RMSE מישורי ${fmt(a.rmse_xy, 3)} · RMSE גובה ${fmt(a.rmse_h, 3)} · ממוצע dH ${fmt(a.mean_dh, 3)}`;
  $('#accSummary').innerHTML = acc.points.length ? `${fmtA(acc.gcp, 'בקרה')}<br>${fmtA(acc.check, 'ביקורת')}<br>תיקון גובה פעיל: ${fmt(acc.vertical_offset, 3)} מ'` : 'לא נטענו נקודות';
  const cls = v => v === null || v === undefined ? '' : Math.abs(v) > 0.05 ? 'bad' : 'good';
  $('#ctrlTable').innerHTML = acc.points.length ? `<tr><th>שם</th><th>סוג</th><th>dE</th><th>dN</th><th>dH</th><th></th></tr>` + acc.points.map(p =>
    `<tr><td>${esc(p.name)}</td><td><select data-kind="${esc(p.name)}"><option value="gcp" ${p.kind === 'gcp' ? 'selected' : ''}>GCP</option><option value="check" ${p.kind === 'check' ? 'selected' : ''}>CHK</option></select></td>
     <td class="${cls(p.dE)}">${fmt(p.dE, 3)}</td><td class="${cls(p.dN)}">${fmt(p.dN, 3)}</td><td class="${cls(p.dH)}">${fmt(p.dH, 3)}</td>
     <td><button data-measure="${esc(p.name)}">סמן</button> <button data-goto="${esc(p.name)}">⌖</button></td></tr>`).join('') : '';
  if (!ctrlLayer) return;
  acc.points.forEach(p => {
    const icon = L.divIcon({ className: '', html: `<div style="color:${p.kind === 'check' ? '#22c55e' : '#ff2020'};font-size:18px;line-height:18px;text-shadow:0 0 3px #000">▲</div>`, iconSize: [18, 18], iconAnchor: [9, 12] });
    L.marker([p.N, p.E], { icon, pmIgnore: true }).bindTooltip(`${esc(p.name)} H=${p.H}`).addTo(ctrlLayer);
    if (p.measured) L.circleMarker([p.measured[1], p.measured[0]], { radius: 3, color: '#ffd400', pmIgnore: true }).addTo(ctrlLayer);
  });
  $('#ctrlTable').querySelectorAll('[data-measure]').forEach(b => b.onclick = () => { measuring = b.dataset.measure; map.pm.disableDraw(); activeCode = null; renderCodes(); status(`לחץ על מרכז המטרה של ${measuring}`, 0); goTo(measuring); });
  $('#ctrlTable').querySelectorAll('[data-goto]').forEach(b => b.onclick = () => goTo(b.dataset.goto));
  $('#ctrlTable').querySelectorAll('[data-kind]').forEach(s => s.onchange = async () => {
    const p = acc.points.find(x => x.name === s.dataset.kind);
    renderControl(await api(`/api/projects/${proj.id}/control/measure`, jsonOpts('POST', { name: p.name, kind: s.value, e: p.measured?.[0] ?? null, n: p.measured?.[1] ?? null })));
  });
}
function goTo(name) {
  api(`/api/projects/${proj.id}/accuracy`).then(acc => { const q = acc.points.find(x => x.name === name); if (q) map.setView([q.N, q.E], 5); });
}
async function onMapClick(e) {
  if (measuring) {
    const name = measuring; measuring = null;
    renderControl(await api(`/api/projects/${proj.id}/control/measure`, jsonOpts('POST', { name, e: e.latlng.lng, n: e.latlng.lat })));
    status(`${name} נמדד`);
    return;
  }
  if (!activeCode && selected) { featLayers[selected]?.setStyle({ dashArray: null }); featLayers[selected]?.pm.disable(); selected = null; $('#selInfo').classList.add('hidden'); }
}

/* ------------------------------------------------------------ generated spots / contours */
function renderGenerated(g) {
  if (!genLayer) return;
  genLayer.clearLayers();
  g.contours.forEach(c => L.polyline(c.coords.map(p => [p[1], p[0]]), { color: c.major ? '#c08040' : '#e0c090', weight: c.major ? 1.6 : 0.8, interactive: false, pmIgnore: true }).addTo(genLayer));
  genLayer._spots = g.spots.map(([e, n, z]) => {
    const m = L.circleMarker([n, e], { radius: 2, color: '#fff', weight: 1, interactive: false, pmIgnore: true });
    m._label = L.tooltip({ permanent: true, direction: 'right', className: 'elev-label', offset: [3, 0] }).setContent(z.toFixed(2)).setLatLng([n, e]);
    m.addTo(genLayer); return m; });
  updateLabels();
}
function updateLabels() {
  if (!genLayer || !genLayer._spots) return;
  const show = map.getZoom() >= 2 && genLayer._spots.length < 4000;
  genLayer._spots.forEach(m => { if (show && map.hasLayer(genLayer)) m._label.addTo(map); else m._label.remove(); });
}

/* ------------------------------------------------------------ meta / data tabs */
function fillMeta() {
  $('#mName').value = proj.name; $('#mScale').value = String(proj.scale);
  document.querySelectorAll('[data-meta]').forEach(i => i.value = proj.meta[i.dataset.meta] || '');
}
function renderRasterInfo() {
  const r = proj.rasters, lines = [];
  for (const [k, t] of [['ortho', 'אורתופוטו'], ['dsm', 'DSM'], ['dtm', 'DTM']])
    lines.push(r[k] ? `✓ ${t}: ${r[k].width}×${r[k].height} פיקסלים, רזולוציה ${(r[k].res[0] * 100).toFixed(1)} ס"מ${r[k].zmin !== undefined ? `, גבהים ${r[k].zmin.toFixed(1)}–${r[k].zmax.toFixed(1)}` : ''}` : `✗ ${t}: לא נטען`);
  $('#rasterInfo').innerHTML = lines.join('<br>');
}

function bindUI() {
  document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
    document.querySelectorAll('.tabs button, .tab').forEach(x => x.classList.remove('active'));
    b.classList.add('active'); $('#tab-' + b.dataset.tab).classList.add('active');
  });
  $('#codeSearch').oninput = renderCodes;
  $('#codeList').onclick = e => { const el = e.target.closest('.code'); if (el) selectCode(el.dataset.code); };
  $('#projSelect').onchange = e => e.target.value && openProject(e.target.value);
  $('#btnNew').onclick = async () => {
    const name = prompt('שם הפרויקט:'); if (!name) return;
    const p = await api('/api/projects', jsonOpts('POST', { name, epsg: 2039, scale: 250, meta: {} }));
    await refreshProjects(); $('#projSelect').value = p.id; await openProject(p.id);
    document.querySelector('[data-tab=data]').click();
  };
  $('#btnDxf').onclick = () => location.href = `/api/projects/${proj.id}/export.dxf`;
  $('#btnZip').onclick = () => { status('מכין חבילה…', 0); location.href = `/api/projects/${proj.id}/export.zip`; setTimeout(() => status(''), 3000); };
  document.querySelectorAll('input[data-kind]').forEach(inp => inp.onchange = async () => {
    if (!proj || !inp.files[0]) return;
    const fd = new FormData(); fd.append('file', inp.files[0]);
    status(`מעלה ומעבד ${inp.dataset.kind}… (בונה פירמידות תצוגה)`, 0);
    try { await api(`/api/projects/${proj.id}/upload/${inp.dataset.kind}`, { method: 'POST', body: fd }); status('נטען בהצלחה'); await openProject(proj.id); }
    catch (err) { status('שגיאה: ' + err.message, 8000); }
    inp.value = '';
  });
  $('#ctrlFile').onchange = async () => {
    const fd = new FormData(); fd.append('file', $('#ctrlFile').files[0]); fd.append('swap_en', $('#swapEN').checked);
    try { await api(`/api/projects/${proj.id}/control`, { method: 'POST', body: fd }); renderControl(await api(`/api/projects/${proj.id}/accuracy`)); }
    catch (err) { status('שגיאה: ' + err.message, 8000); }
    $('#ctrlFile').value = '';
  };
  $('#btnApplyOffset').onclick = async () => {
    const acc = await api(`/api/projects/${proj.id}/accuracy`);
    if (acc.gcp.mean_dh === null) { status('אין נקודות בקרה עם גובה'); return; }
    const off = (acc.vertical_offset || 0) - acc.gcp.mean_dh;
    if (!confirm(`להחיל תיקון גובה של ${off.toFixed(3)} מ' על כל הגבהים?`)) return;
    await setOffset(off);
  };
  $('#btnClearOffset').onclick = () => setOffset(0);
  $('#btnGen').onclick = async () => {
    $('#genStatus').textContent = 'מחשב…';
    try {
      const r = await api(`/api/projects/${proj.id}/generate`, jsonOpts('POST', { spot_spacing: +$('#genSpot').value || null,
        contour_interval: +$('#genInt').value || null, major_every: +$('#genMajor').value || 5, smooth: +$('#genSmooth').value, boundary_id: $('#genArea').value || null }));
      $('#genStatus').textContent = `נוצרו ${r.spots} רומים ו־${r.contours} קווי גובה`;
      renderGenerated(await api(`/api/projects/${proj.id}/generated`));
    } catch (err) { $('#genStatus').textContent = 'שגיאה: ' + err.message; }
  };
  $('#btnSaveMeta').onclick = async () => {
    const meta = {}; document.querySelectorAll('[data-meta]').forEach(i => meta[i.dataset.meta] = i.value);
    proj = { ...proj, ...(await api(`/api/projects/${proj.id}`, jsonOpts('PATCH', { name: $('#mName').value, scale: +$('#mScale').value, meta }))) };
    await refreshProjects(); status('נשמר');
  };
  $('#tplFile').onchange = async () => {
    const fd = new FormData(); fd.append('file', $('#tplFile').files[0]);
    status('קורא תבנית…', 0);
    try { await api(`/api/projects/${proj.id}/template`, { method: 'POST', body: fd }); status('התבנית נטענה, הקודים מופו אוטומטית. בדוק את הטבלה.', 6000); await openProject(proj.id); }
    catch (err) { status('שגיאה: ' + err.message, 10000); }
    $('#tplFile').value = '';
  };
  $('#cadStrict').onchange = $('#cadIgnore').onchange = () => api(`/api/projects/${proj.id}/cad`, jsonOpts('PATCH', { strict: $('#cadStrict').checked, ignore_layer: $('#cadIgnore').value }));
  $('#btnSaveMap').onclick = async () => {
    const items = [...document.querySelectorAll('#mapTable tr[data-code]')].map(tr => ({ code: tr.dataset.code, layer: tr.querySelector('.mLayer').value,
      ...(tr.querySelector('.mBlock') ? { block: tr.querySelector('.mBlock').value } : {}) }));
    proj.codes = await api(`/api/projects/${proj.id}/codes`, jsonOpts('PATCH', items));
    codes = proj.codes; codeMap = {}; codes.forEach(c => codeMap[c.code] = c); renderCodes(); status('המיפוי נשמר');
  };
  $('#btnOdm').onclick = async () => {
    const imgs = $('#odmImages').files; if (!imgs.length) { status('בחר תמונות'); return; }
    const fd = new FormData(); [...imgs].forEach(f => fd.append('images', f));
    if ($('#odmGcp').files[0]) fd.append('gcp', $('#odmGcp').files[0]);
    fd.append('quality', $('#odmQuality').value);
    $('#odmStatus').textContent = `מעלה ${imgs.length} תמונות…`;
    try { await api(`/api/projects/${proj.id}/odm`, { method: 'POST', body: fd }); pollOdm(); }
    catch (err) { $('#odmStatus').textContent = 'שגיאה: ' + err.message; }
  };
  [['#lyOrtho', () => orthoLayer], ['#lyDem', () => demLayer], ['#lyGen', () => genLayer], ['#lyCtrl', () => ctrlLayer]].forEach(([s, get]) =>
    $(s).onchange = e => { if (!map) return; e.target.checked ? get().addTo(map) : get().remove(); updateLabels(); });

  document.addEventListener('keydown', async e => {
    if (e.target.matches('input, select, textarea')) return;
    if (e.key === 'Escape') { map?.pm.disableDraw(); activeCode = null; measuring = null; renderCodes(); status(''); }
    else if (e.key === 'Enter' && map) { const d = map.pm.Draw; for (const s of ['Line', 'Polygon']) if (d[s]?._enabled && d[s]._layer.getLatLngs().length > 1) d[s]._finishShape(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { const id = undoStack.pop(); if (id) { await deleteFeature(id); status('בוטל'); } }
    else if (e.key === 'Delete' && selected) deleteFeature(selected);
    else if (e.key.toLowerCase() === 'e' && selected) featLayers[selected].pm.toggleEdit({ snappable: true });
    else if (/^[1-9]$/.test(e.key) && codes[+e.key - 1]) selectCode(codes[+e.key - 1].code);
  });
}
/* ------------------------------------------------------------ CAD template / standard */
async function renderCad() {
  const cad = await api(`/api/projects/${proj.id}/cad`);
  const tpl = await api(`/api/projects/${proj.id}/template`);
  $('#cadStrict').checked = !!cad.strict; $('#cadIgnore').value = cad.ignore_layer || '1201';
  $('#tplInfo').innerHTML = cad.template ? `✓ תבנית: ${esc(cad.template.name)} · ${tpl.layers.length} שכבות · ${tpl.blocks.length} בלוקים <button id="tplDel">הסר</button>` :
    'לא נטענה תבנית: נעשה שימוש בשכבות ברירת המחדל (TOPO-*/SURV-*).' + (cad.dwg_available ? '' : '<br>ייצוא DWG ישיר: לא זמין (ODA File Converter לא מותקן), הייצוא הוא DXF.');
  if ($('#tplDel')) $('#tplDel').onclick = async () => { await api(`/api/projects/${proj.id}/template`, { method: 'DELETE' }); openProject(proj.id); };
  const layerNames = [...new Set([...tpl.layers.map(l => l.name), ...codes.map(c => c.layer)])].sort();
  const blockNames = ['', ...tpl.blocks.map(b => b.name), 'SYM_SPOT', 'SYM_POLE', 'SYM_TREE', 'SYM_MH', 'SYM_HYD', 'SYM_SIGN', 'SYM_GCP'];
  const opt = (arr, cur) => arr.map(n => `<option ${n === cur ? 'selected' : ''} value="${esc(n)}">${esc(n || '—')}</option>`).join('');
  $('#mapTable').innerHTML = '<tr><th>קוד</th><th>שכבה</th><th>בלוק</th></tr>' + codes.map(c => `<tr data-code="${c.code}">
    <td title="${esc(c.label)}">${c.code}</td><td><select class="mLayer">${opt(layerNames, c.layer)}</select></td>
    <td>${c.geom === 'point' ? `<select class="mBlock">${opt([...new Set(blockNames)], c.block || '')}</select>` : ''}</td></tr>`).join('');
}
async function setOffset(off) {
  proj = await api(`/api/projects/${proj.id}`, jsonOpts('PATCH', { vertical_offset: off }));
  renderControl(await api(`/api/projects/${proj.id}/accuracy`));
  featLayer.clearLayers(); proj.features.forEach(addFeatureLayer);
  status(`תיקון גובה ${off.toFixed(3)} מ' הוחל. יש ליצור מחדש רומים וקווי גובה.`, 6000);
}
async function pollOdm() {
  const s = await api(`/api/projects/${proj.id}/odm`);
  $('#odmStatus').textContent = `סטטוס: ${s.status} · ${Math.round(s.progress || 0)}%` + (s.error ? ` · ${s.error}` : '');
  if (s.status === 'done') { await openProject(proj.id); return; }
  if (s.status !== 'failed') setTimeout(pollOdm, 5000);
}

init();
