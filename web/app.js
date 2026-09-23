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
const TERM_RU = {
  money: 'деньги курьеров',
  turnover: 'оборот',
  betweenness: 'посредничество',
  role: 'роль',
  reach: 'охват seed',
};
const QUEUE_SIZE = 30;
const EDGE_COLOR = '#8a8f98';
const EDGE_OPACITY = 0.45;
const FADED = 0.12;

const state = {
  graph: null, byId: new Map(), ranked: [], rank: new Map(), inc: new Map(), out: new Map(), selected: null,
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
const fmtPct = new Intl.NumberFormat('ru-RU', { style: 'percent', maximumFractionDigits: 2 }).format;

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
  net.setSize('100%', '100%');
  net.redraw();
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

// ---------------------------------------------------------------- экран 3: карточка узла
function block(title, ...children) {
  return el('section', { class: 'cb' }, el('h3', {}, title), ...children);
}
function kv(label, value) {
  return el('div', { class: 'kv' }, el('span', { class: 'k' }, label), el('span', { class: 'v' }, value));
}

function cardHead(n) {
  return el('section', { class: 'cb' },
    el('div', { class: 'card-top' },
      el('span', { class: 'gid big' }, n.id),
      el('button', { type: 'button', class: 'close', title: 'Закрыть', 'data-action': 'close' }, '×')),
    el('div', {}, roleBadge(n.role),
      n.seed ? el('span', { class: 'tag seed' }, 'seed') : null,
      n.truncated ? el('span', { class: 'tag truncated' }, 'край выборки') : null),
    el('div', { class: 'kvs' },
      kv('правило', n.rule_id),
      kv('сила признаков', String(n.role_score)),
      kv('кластер', n.cluster === 0 ? '0 — без связей' : String(n.cluster)),
      kv('колено', String(n.depth)),
      kv('место в очереди', `${state.rank.get(n.id)} из ${fmtInt.format(state.ranked.length)}`)),
    el('div', { class: 'facts' },
      `получено ${fmtKzt(n.in_kzt)} · плательщиков: ${n.in_deg} · отправлено ${fmtKzt(n.out_kzt)} · получателей: ${n.out_deg}`),
    n.seed_money > 0
      ? el('div', { class: 'facts' },
        `след денег курьеров: ${fmtKzt(n.seed_money)} (${fmtPct(n.seed_money_share)} от всех) · путь есть у ${n.seed_reach} seed`)
      : null,
  );
}

function cardPriority(n) {
  const t = n.priority_terms || {};
  const keys = Object.keys(TERM_RU);
  const top = Math.max(...keys.map((k) => t[k] || 0));
  const rows = keys.map((k) => {
    const v = t[k] || 0;
    const w = top > 0 ? (100 * v) / top : 0;
    return el('div', { class: 'bar-row' },
      el('span', {}, `${TERM_RU[k]} `, el('span', { class: 'muted' }, k)),
      el('span', { class: 'bar-bg' }, el('span', { class: 'bar', style: `width:${w}%` })),
      el('span', { class: 'num' }, fmtP(v)));
  });
  const mult = t.multiplier !== undefined && t.multiplier !== 1
    ? el('div', { class: 'facts' }, `множитель: ×${t.multiplier}`)
    : null;
  return block('Приоритет проверки', el('div', { class: 'prio' }, fmtP(n.priority)), rows, mult);
}

function cardFlags(n) {
  const levels = (n.levels || []).map((l) => el('li', {}, `уровень ${l.level} — ${l.list}`));
  return block('Метки и уровни',
    n.flags && n.flags.length ? el('div', {}, flagChips(n.flags)) : el('div', { class: 'muted' }, 'меток нет'),
    n.flags_evidence ? el('div', { class: 'facts' }, n.flags_evidence) : null,
    levels.length ? el('ul', { class: 'limits' }, levels) : null,
    n.plan_step ? el('div', { class: 'facts' }, `шаг ${n.plan_step} из ${state.graph.plan.length} в плане охвата`) : null,
  );
}

function edgeList(edges, otherEnd) {
  if (!edges.length) return el('div', { class: 'muted' }, 'нет');
  const rows = [...edges].sort((a, b) => b.sum_kzt - a.sum_kzt).map((e) => {
    const c = state.byId.get(e[otherEnd]);
    return el('li', { dataset: { gid: c.id }, title: 'Перейти к узлу' },
      el('span', { class: 'gid' }, c.id), roleBadge(c.role),
      el('span', { class: 'num' }, fmtKzt(e.sum_kzt)),
      el('span', { class: 'muted' }, `${e.n_tx} пер.`));
  });
  return el('ul', { class: 'edges' }, rows);
}

function cardEdges(n) {
  const inc = state.inc.get(n.id) || [];
  const out = state.out.get(n.id) || [];
  return block('Переводы узла',
    el('div', { class: 'sub' }, `← входящие: ${inc.length}`), edgeList(inc, 'source'),
    el('div', { class: 'sub' }, `→ исходящие: ${out.length}`), edgeList(out, 'target'));
}

function cardLimits(n) {
  const items = [];
  if (n.out_deg === 0) items.push('исходящие от 5 000 ₸ внутри банка не наблюдаются в выборке');
  if (n.truncated) items.push('край выборки: исходящие не выгружены');
  if (n.seed) items.push('входящие извне выборки не видны');
  return block('Ограничения данных',
    items.length ? el('ul', { class: 'limits' }, items.map((t) => el('li', {}, t))) : el('div', { class: 'muted' }, '—'));
}

function renderCard(gid) {
  const n = state.byId.get(gid);
  const card = $('#card');
  card.replaceChildren(
    cardHead(n),
    block('Почему в очереди', el('div', { class: 'evidence' }, n.evidence)),
    cardPriority(n),
    cardFlags(n),
    cardEdges(n),
    cardLimits(n),
  );
  card.hidden = false;
  card.scrollTop = 0;
  $('.layout').classList.add('with-card');
}

function closeCard() {
  $('#card').hidden = true;
  $('.layout').classList.remove('with-card');
  state.selected = null;
  for (const tr of document.querySelectorAll('#queue tbody tr.selected')) tr.classList.remove('selected');
  clearHighlight();
}

function setupCard() {
  $('#card').addEventListener('click', (e) => {
    if (e.target.closest('[data-action="close"]')) return closeCard();
    const li = e.target.closest('li[data-gid]');
    if (li) select(li.dataset.gid);
  });
}

// ---------------------------------------------------------------- выбор узла
function select(gid) {
  if (!state.byId.has(gid)) return false;
  state.selected = gid;
  for (const tr of document.querySelectorAll('#queue tbody tr')) {
    tr.classList.toggle('selected', tr.dataset.gid === gid);
  }
  renderCard(gid);
  highlight(gid); // setSize() внутри читает новую ширину контейнера после открытия карточки
  return true;
}

// ---------------------------------------------------------------- загрузка
async function load() {
  const res = await fetch('graph.json');
  if (!res.ok) throw new Error(`graph.json: HTTP ${res.status}`);
  const g = await res.json();
  state.graph = g;
  for (const n of g.nodes) state.byId.set(n.id, n);
  for (const e of g.edges) {
    if (!state.out.has(e.source)) state.out.set(e.source, []);
    if (!state.inc.has(e.target)) state.inc.set(e.target, []);
    state.out.get(e.source).push(e);
    state.inc.get(e.target).push(e);
  }
  state.ranked = [...g.nodes].sort((a, b) => b.priority - a.priority);
  state.ranked.forEach((n, i) => state.rank.set(n.id, i + 1));
  $('#meta').textContent = `узлов: ${fmtInt.format(g.nodes.length)} · рёбер: ${fmtInt.format(g.edges.length)}`;
  renderQueue();
  setupSearch();
  setupCard();
  buildMap();
}

load().catch((err) => {
  $('#meta').textContent = '';
  $('.left').prepend(el('div', { class: 'error' }, `Не удалось загрузить данные: ${err.message}. Запуск: python -m http.server 8000 --directory web`));
});
