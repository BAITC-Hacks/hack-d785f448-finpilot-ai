'use strict';

// Все числа приходят готовыми из graph.json (DATA_CONTRACT.md). Фронт только показывает их.
// gid — всегда строка: 18 цифр не помещаются в JS number.

const ROLE_RU = {
  coordinator: 'кандидат в координаторы',
  consolidator: 'точка консолидации',
  distributor: 'распределитель',
  transit: 'транзит',
  terminal: 'конечный получатель',
  peripheral: 'периферия',
};
const ROLE_COLOR = {
  coordinator: '#d62728',
  consolidator: '#ff7f0e',
  distributor: '#9467bd',
  transit: '#ffdd57',
  terminal: '#2ca02c',
  peripheral: '#bbbbbb',
};
const FLAG_RU = {
  payer_of_couriers: 'платит известным курьерам',
  courier_candidate: 'кандидат в курьеры',
  beneficiary: 'получатель денег курьеров',
  in_cycle: 'в замкнутом маршруте',
  layering: 'цепочка наслоения',
  split_in: 'дробление переводов',
};
const QUEUE_SIZE = 30;
const EDGE_COLOR = '#8a8f98';
const EDGE_OPACITY = 0.45;
const FADED = 0.12;

const state = {
  graph: null, byId: new Map(), ranked: [], selected: null,
  network: null, nodesDS: null, edgesDS: null,
};

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const fmtInt = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const fmtKzt = (x) => fmtInt.format(x) + ' ₸';
const fmtP = (x) => Number(x).toFixed(3);

function roleBadge(role) {
  return el('span', { class: `badge ${role}` }, ROLE_RU[role] || role);
}
function flagChips(flags) {
  return (flags || []).map((f) => el('span', { class: 'chip' }, FLAG_RU[f] || f));
}

// ---------------------------------------------------------------- экран 1: очередь проверки
function renderQueue() {
  const tbody = $('#queue tbody');
  tbody.replaceChildren(
    ...state.ranked.slice(0, QUEUE_SIZE).map((n, i) =>
      el('tr', { dataset: { gid: n.id } },
        el('td', { class: 'num' }, i + 1),
        el('td', { class: 'gid' }, n.id),
        el('td', {}, roleBadge(n.role)),
        el('td', { class: 'num' }, fmtP(n.priority)),
        el('td', { class: 'ev' }, n.evidence),
        el('td', {}, flagChips(n.flags)),
      ),
    ),
  );
  tbody.addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-gid]');
    if (tr) select(tr.dataset.gid);
  });
}

// ---------------------------------------------------------------- экран 2: карта
// Размер узла и толщина ребра — только визуальная шкала поверх готовых priority и sum_kzt.
function nodeView(n) {
  return {
    id: n.id,
    size: 8 + 22 * n.priority,
    color: {
      background: ROLE_COLOR[n.role],
      border: n.seed ? '#000' : n.truncated ? '#333' : '#777',
      highlight: { background: ROLE_COLOR[n.role], border: '#000' },
    },
    borderWidth: n.seed ? 3 : n.truncated ? 2 : 1,
    borderWidthSelected: 4,
    shapeProperties: { borderDashes: n.truncated && !n.seed ? [4, 3] : false },
    opacity: 1,
    title: `${n.id} · ${ROLE_RU[n.role]}`,
  };
}

function edgeWidthScale(edges) {
  const logs = edges.filter((e) => e.sum_kzt > 0).map((e) => Math.log10(e.sum_kzt));
  const lo = Math.min(...logs);
  const hi = Math.max(...logs);
  return (sum) => (sum > 0 && hi > lo ? 0.5 + (4 * (Math.log10(sum) - lo)) / (hi - lo) : 0.5);
}

function renderLegend() {
  $('#legend').replaceChildren(
    ...Object.keys(ROLE_RU).map((r) =>
      el('span', {}, el('i', { class: 'swatch', style: `background:${ROLE_COLOR[r]}` }), ROLE_RU[r])),
    el('span', {}, el('i', { class: 'swatch seed' }), 'seed'),
    el('span', {}, el('i', { class: 'swatch truncated' }), 'край выборки'),
  );
}

function setMapStatus(text) {
  $('#map-status').textContent = text;
}

function buildMap() {
  renderLegend();
  if (!window.vis) {
    setMapStatus('Библиотека карты не загрузилась (нет сети?) — положите vis-network.min.js в web/vendor/. Очередь работает.');
    return;
  }
  const g = state.graph;
  const width = edgeWidthScale(g.edges);
  state.nodesDS = new vis.DataSet(g.nodes.map(nodeView));
  state.edgesDS = new vis.DataSet(g.edges.map((e, i) => ({
    id: i,
    from: e.source,
    to: e.target,
    width: width(e.sum_kzt),
    color: { color: EDGE_COLOR, opacity: EDGE_OPACITY },
    title: `${fmtKzt(e.sum_kzt)} · переводов: ${e.n_tx}`,
  })));
  const net = new vis.Network($('#map'), { nodes: state.nodesDS, edges: state.edgesDS }, {
    layout: { improvedLayout: false },
    nodes: { shape: 'dot' },
    edges: { arrows: { to: { enabled: true, scaleFactor: 0.4 } }, smooth: false, selectionWidth: 2 },
    physics: {
      solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -40, springLength: 60, avoidOverlap: 0.2 },
      stabilization: { enabled: true, iterations: 200, updateInterval: 20 },
    },
    interaction: { hover: false, tooltipDelay: 150, hideEdgesOnDrag: true },
  });
  net.on('stabilizationProgress', (p) => setMapStatus(`карта строится… ${Math.round((100 * p.iterations) / p.total)}%`));
  net.once('stabilizationIterationsDone', () => {
    net.setOptions({ physics: false });
    setMapStatus('');
    if (state.selected) highlight(state.selected);
  });
  net.on('click', (p) => {
    if (p.nodes.length) select(String(p.nodes[0]));
    else clearHighlight();
  });
  state.network = net;
}

function highlight(gid) {
  const net = state.network;
  if (!net) return;
  const keep = new Set([gid, ...net.getConnectedNodes(gid).map(String)]);
  state.nodesDS.update(state.graph.nodes.map((n) => ({ id: n.id, opacity: keep.has(n.id) ? 1 : FADED })));
  state.edgesDS.update(state.graph.edges.map((e, i) => ({
    id: i,
    color: { color: EDGE_COLOR, opacity: e.source === gid || e.target === gid ? 0.95 : 0.04 },
  })));
  net.selectNodes([gid]);
  net.focus(gid, { scale: 1.2, animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
}

function clearHighlight() {
  const net = state.network;
  if (!net) return;
  state.nodesDS.update(state.graph.nodes.map((n) => ({ id: n.id, opacity: 1 })));
  state.edgesDS.update(state.graph.edges.map((e, i) => ({ id: i, color: { color: EDGE_COLOR, opacity: EDGE_OPACITY } })));
  net.unselectAll();
}

function setupSearch() {
  const input = $('#search');
  const msg = $('#search-msg');
  $('#search-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = input.value.replace(/\s+/g, '');
    msg.textContent = '';
    if (!q) {
      clearHighlight();
      return;
    }
    if (select(q)) return;
    const hits = q.length >= 4 ? state.graph.nodes.filter((n) => n.id.includes(q)) : [];
    if (hits.length === 1) {
      select(hits[0].id);
      msg.textContent = `найден ${hits[0].id}`;
    } else {
      msg.textContent = hits.length ? `совпадений: ${hits.length} — уточните gid` : 'gid не найден';
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') clearHighlight();
  });
}

// ---------------------------------------------------------------- выбор узла
function select(gid) {
  if (!state.byId.has(gid)) return false;
  state.selected = gid;
  for (const tr of document.querySelectorAll('#queue tbody tr')) {
    tr.classList.toggle('selected', tr.dataset.gid === gid);
  }
  highlight(gid);
  return true;
}

// ---------------------------------------------------------------- загрузка
async function load() {
  const res = await fetch('graph.json');
  if (!res.ok) throw new Error(`graph.json: HTTP ${res.status}`);
  const g = await res.json();
  state.graph = g;
  for (const n of g.nodes) state.byId.set(n.id, n);
  state.ranked = [...g.nodes].sort((a, b) => b.priority - a.priority);
  $('#meta').textContent = `узлов: ${fmtInt.format(g.nodes.length)} · рёбер: ${fmtInt.format(g.edges.length)}`;
  renderQueue();
  setupSearch();
  buildMap();
}

load().catch((err) => {
  $('#meta').textContent = '';
  $('.left').prepend(el('div', { class: 'error' }, `Не удалось загрузить данные: ${err.message}. Запуск: python -m http.server 8000 --directory web`));
});
