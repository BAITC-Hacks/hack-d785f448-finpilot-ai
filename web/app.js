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

// ---------------------------------------------------------------- экран 4: план охвата
// Процент — формат готовой доли из данных (plan_meta.capture_30, cumulative), не пересчёт.
const pct = (x) => fmtPct(x).replace(/\s/g, '');

function renderPlan() {
  const g = state.graph;
  const meta = g.plan_meta || {};
  $('#plan-title').textContent =
    `Модельный охват выбранных счетов — ${pct(meta.capture_30)} при фиксированных июльских потоках`;
  $('#plan-note').textContent = meta.note || '';
  $('#plan tbody').replaceChildren(
    ...g.plan.map((p) =>
      el('tr', { dataset: { gid: p.gid } },
        el('td', { class: 'num' }, p.step),
        el('td', { class: 'gid' }, p.gid),
        el('td', {}, roleBadge(p.role)),
        el('td', { class: 'num' }, `+${pct(p.gain)}`),
        el('td', { class: 'num' }, pct(p.cumulative)),
      ),
    ),
  );
}

// ---------------------------------------------------------------- экран 5: уровни
const LEVEL_TITLE = {
  0: 'Уровень 0 — курьеры',
  1: 'Уровень 1 — точки сбора',
  2: 'Уровень 2 — цепочки наслоения',
  3: 'Уровень 3 — координаторы',
  4: 'Уровень 4 — получатели',
};

function renderLevels() {
  const groups = new Map(Object.keys(LEVEL_TITLE).map((k) => [Number(k), []]));
  for (const n of state.ranked) {
    for (const l of n.levels || []) {
      if (groups.has(l.level)) groups.get(l.level).push({ n, list: l.list });
    }
  }
  $('#tab-levels').replaceChildren(
    ...[...groups].map(([level, items]) =>
      el('section', { class: 'level' },
        el('h3', {}, LEVEL_TITLE[level], ' ', el('span', { class: 'muted' }, `· ${items.length}`)),
        items.length
          ? el('ul', {}, items.map(({ n, list }) =>
            el('li', { dataset: { gid: n.id } },
              el('span', { class: 'gid' }, n.id), roleBadge(n.role),
              el('span', { class: 'chip' }, list),
              el('span', { class: 'num' }, fmtP(n.priority)))))
          : el('div', { class: 'muted', style: 'padding:6px 10px' }, 'нет узлов'),
      ),
    ),
  );
}

// ---------------------------------------------------------------- вкладки
function setupTabs() {
  $('#tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-tab]');
    if (!btn) return;
    for (const b of document.querySelectorAll('#tabs button')) b.classList.toggle('active', b === btn);
    for (const t of document.querySelectorAll('.left .tab')) t.hidden = t.id !== `tab-${btn.dataset.tab}`;
  });
  // строки плана и уровней открывают карточку узла
  for (const id of ['#plan', '#tab-levels']) {
    $(id).addEventListener('click', (e) => {
      const row = e.target.closest('[data-gid]');
      if (row) select(row.dataset.gid);
    });
  }
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
    state.mapFailed = true;
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

// ---------------------------------------------------------------- ассистент в карточке (python assistant.py serve)
// Страница не зависит от ассистента: сервер не отвечает — подсказка, остальное работает.
const API = 'http://127.0.0.1:8765';
const NEXT_STEP_RU = {
  check_neighbors: 'проверить контрагентов',
  request_data: 'запросить данные',
  mark_legit: 'отметить как легальную деятельность',
  escalate: 'передать на углублённую проверку',
  none: 'действий не требуется',
};
const assistant = { up: null, mode: null, model: null };
const NOT_RUNNING = 'ассистент не запущен: python assistant.py serve';

async function api(path, timeoutMs = 60000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    let res;
    try {
      res = await fetch(API + path, { signal: ctrl.signal });
    } catch (err) {
      throw Object.assign(new Error(err.name === 'AbortError' ? 'ассистент не ответил вовремя' : NOT_RUNNING), { offline: true });
    }
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    return body;
  } finally {
    clearTimeout(timer);
  }
}

function assistantStatus() {
  let text = 'ассистент: проверка связи…';
  if (assistant.up === true) text = `ассистент: режим ${assistant.mode}${assistant.model ? ` · ${assistant.model}` : ''}`;
  if (assistant.up === false) text = NOT_RUNNING;
  return el('div', { id: 'assistant-status', class: assistant.up === false ? 'warn' : 'muted' }, text);
}

async function checkAssistant() {
  try {
    const h = await api('/api/health', 2500);
    Object.assign(assistant, { up: true, mode: h.mode, model: h.model });
  } catch {
    assistant.up = false;
  }
  const s = document.getElementById('assistant-status');
  if (s) s.replaceWith(assistantStatus());
}

function cardAssistant() {
  return block('Ассистент аналитика',
    assistantStatus(),
    el('div', { class: 'actions' },
      el('button', { type: 'button', 'data-ai': 'explain' }, 'Объяснить'),
      el('button', { type: 'button', 'data-ai': 'whatif' }, 'Что если исключить'),
      el('button', { type: 'button', 'data-ai': 'request' }, 'Сформировать запрос')),
    el('div', { id: 'ai-out' }));
}

function aiList(title, items) {
  return items && items.length ? [el('div', { class: 'sub' }, title), el('ul', { class: 'limits' }, items.map((t) => el('li', {}, t)))] : [];
}
function aiGids(title, gids) {
  return [el('div', { class: 'sub' }, `${title}: ${gids.length}`),
    gids.length ? el('ul', { class: 'edges' }, gids.map((g) => el('li', { dataset: { gid: g } },
      el('span', { class: 'gid' }, g), state.byId.has(g) ? roleBadge(state.byId.get(g).role) : null))) : null];
}
function aiSource(ans) {
  return ans && ans._source ? el('div', { class: 'muted src' }, `источник ответа: ${ans._source}`) : null;
}

const AI_VIEW = {
  explain: (b) => {
    const a = b.answer || {};
    return [
      el('p', { class: 'ai-summary' }, a.summary),
      ...aiList('Гипотезы', a.hypotheses),
      ...aiList('Альтернативные объяснения', a.alternative_explanations),
      ...aiList('Каких данных не хватает', a.missing_data),
      el('div', { class: 'facts' }, el('b', {}, 'Следующий шаг: '), NEXT_STEP_RU[a.next_step] || a.next_step || '—',
        a.next_step_reason ? ` — ${a.next_step_reason}` : ''),
      aiSource(a),
    ];
  },
  whatif: (b) => {
    const k = (b.top_after || []).length;
    return [
      el('p', { class: 'ai-summary' }, (b.answer && b.answer.summary) || ''),
      ...aiGids(`Вошли в топ-${k}`, b.entered || []),
      ...aiGids(`Вышли из топ-${k}`, b.left || []),
      ...aiList('Сдвиги в очереди', (b.moved || []).map((m) => `${m.gid}: было ${m.was ?? '—'} → стало ${m.now}`)),
      el('div', { class: 'muted' }, b.note || ''),
      aiSource(b.answer),
    ];
  },
  request: (b) => [
    el('div', { class: 'approval' },
      el('b', {}, 'Требуется подтверждение человека'),
      el('p', {}, 'Черновик запроса подготовлен. Ничего не отправлено и не сохранено, пока аналитик не подтвердит.'),
      el('pre', { class: 'draft' }, b.draft),
      el('div', { class: 'actions' },
        el('button', { type: 'button', class: 'primary', 'data-ai': 'confirm' }, 'Подтвердить'),
        el('button', { type: 'button', 'data-ai': 'reject' }, 'Отклонить'))),
  ],
  confirm: (b) => [
    el('div', { class: 'ok' }, `Запрос подтверждён аналитиком и сохранён: ${b.path}`),
    el('pre', { class: 'draft' }, b.draft),
  ],
};

async function runAssistant(action, gid) {
  const out = $('#ai-out');
  if (action === 'reject') {
    out.replaceChildren(el('div', { class: 'facts' }, 'Запрос отклонён. Ничего не отправлено и не сохранено.'));
    return;
  }
  const q = encodeURIComponent(gid);
  const path = {
    explain: `/api/explain?gid=${q}`,
    whatif: `/api/whatif?exclude=${q}`,
    request: `/api/request?gid=${q}`,
    confirm: `/api/request?gid=${q}&confirm=1`,
  }[action];
  const buttons = document.querySelectorAll('#card [data-ai]');
  buttons.forEach((b) => { b.disabled = true; });
  out.replaceChildren(el('div', { class: 'muted' }, 'ассистент работает…'));
  try {
    const body = await api(path);
    if (state.selected !== gid) return;
    out.replaceChildren(...AI_VIEW[action](body));
  } catch (err) {
    if (state.selected !== gid) return;
    if (err.offline) {
      assistant.up = false;
      const s = document.getElementById('assistant-status');
      if (s) s.replaceWith(assistantStatus());
    }
    out.replaceChildren(el('div', { class: 'warn' }, err.message));
  } finally {
    document.querySelectorAll('#card [data-ai]').forEach((b) => { b.disabled = false; });
  }
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
    cardAssistant(),
  );
  card.hidden = false;
  card.scrollTop = 0;
  $('.layout').classList.add('with-card');
  if (assistant.up !== true) checkAssistant();
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
    const ai = e.target.closest('button[data-ai]');
    if (ai) return runAssistant(ai.dataset.ai, state.selected);
    const li = e.target.closest('li[data-gid]');
    if (li) select(li.dataset.gid);
  });
}

// ---------------------------------------------------------------- выбор узла
function select(gid) {
  if (!state.byId.has(gid)) return false;
  state.selected = gid;
  for (const tr of document.querySelectorAll('#queue tbody tr, #plan tbody tr')) {
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
  renderQueue();
  renderPlan();
  renderLevels();
  setupTabs();
  setupSearch();
  setupCard();
  initShell();
}

// ---------------------------------------------------------------- оболочка: проекты, стартовый экран, загрузка файла
// Проекты и выбранный проект хранятся в localStorage только для удобства показа — это не база данных.
// Загрузка файла — сценарий демо: расчёт делает pipeline.py заранее, страница показывает его результат (graph.json).
const STORE_PROJECTS = 'money-graph.projects';
const STORE_CURRENT = 'money-graph.current';
const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* приватный режим или запрет хранилища — работаем в памяти */
    }
  },
};
const shell = { projects: [], currentId: null, busy: false };

const STAGES = (file) => [
  `Чтение файла ${file}`,
  'Построение графа переводов',
  'Роли узлов по правилам R0–R8',
  'След денег известных курьеров',
  'Приоритет проверки и план охвата',
  'Готово: результат пайплайна — out/graph.json',
];
const STAGE_MS = 550;

const currentProject = () => shell.projects.find((p) => p.id === shell.currentId) || null;

function saveProjects() {
  store.set(STORE_PROJECTS, shell.projects);
  store.set(STORE_CURRENT, shell.currentId);
}

function renderProjects() {
  const g = state.graph;
  $('#projects').replaceChildren(
    ...shell.projects.map((p) =>
      el('li', { class: p.id === shell.currentId ? 'active' : '', dataset: { id: p.id } },
        el('span', { class: 'name', title: p.name }, p.name),
        el('span', { class: 'status' },
          p.status === 'ready' ? `${p.file} · узлов: ${fmtInt.format(g.nodes.length)}`
            : p.status === 'processing' ? 'идёт расчёт…' : 'нет данных'))),
  );
}

function showHome() {
  const p = currentProject();
  const composer = $('#composer');
  const text = $('#composer-text');
  $('#home').hidden = false;
  $('#analysis').hidden = true;
  $('#home-title').textContent = p ? p.name : 'Граф денег';
  $('#home-hint').textContent = p
    ? 'Прикрепите выгрузку транзакций — очередь подозрительных узлов появится после расчёта.'
    : 'Создайте проект слева, затем прикрепите выгрузку транзакций — очередь подозрительных узлов появится после расчёта.';
  composer.classList.toggle('disabled', !p || shell.busy);
  composer.classList.toggle('busy', shell.busy);
  $('#file').disabled = !p || shell.busy;
  if (!shell.busy) {
    text.textContent = 'Прикрепите файл со списком транзакций';
    text.classList.remove('filled');
    $('#stages').hidden = true;
  }
}

function showAnalysis() {
  const p = currentProject();
  const g = state.graph;
  $('#home').hidden = true;
  $('#analysis').hidden = false;
  $('#analysis-title').textContent = p ? `Граф денег · ${p.name}` : 'Граф денег';
  $('#meta').textContent = `узлов: ${fmtInt.format(g.nodes.length)} · рёбер: ${fmtInt.format(g.edges.length)}${p && p.file ? ` · файл: ${p.file}` : ''}`;
  ensureMap();
}

function ensureMap() {
  if (!state.network) {
    if (!state.mapFailed) buildMap();
    return;
  }
  state.network.setSize('100%', '100%');
  state.network.redraw();
}

function openProject(id) {
  shell.currentId = id;
  saveProjects();
  renderProjects();
  const p = currentProject();
  if (p && p.status === 'ready') showAnalysis();
  else showHome();
}

function createProject(name) {
  const project = { id: `p${Date.now().toString(36)}`, name, file: null, status: 'empty' };
  shell.projects.unshift(project);
  openProject(project.id);
}

async function ingestFile(file) {
  const p = currentProject();
  if (!p || shell.busy) return;
  shell.busy = true;
  p.file = file.name;
  p.status = 'processing';
  saveProjects();
  renderProjects();
  showHome();
  const text = $('#composer-text');
  text.textContent = file.name;
  text.classList.add('filled');
  const stages = STAGES(file.name);
  const list = $('#stages');
  list.hidden = false;
  list.replaceChildren(...stages.map((s) => el('li', {}, el('span', { class: 'dot' }), s)));
  const items = list.children;
  for (let i = 0; i < items.length; i++) {
    items[i].classList.add('active');
    await new Promise((r) => setTimeout(r, i === items.length - 1 ? STAGE_MS * 0.6 : STAGE_MS));
    items[i].classList.remove('active');
    items[i].classList.add('done');
    items[i].firstChild.textContent = '✓';
  }
  p.status = 'ready';
  shell.busy = false;
  saveProjects();
  renderProjects();
  showAnalysis();
}

function initShell() {
  shell.projects = (store.get(STORE_PROJECTS, []) || []).filter((p) => p && p.id && p.name)
    .map((p) => ({ ...p, status: p.status === 'processing' ? 'empty' : p.status }));
  shell.currentId = store.get(STORE_CURRENT, null);
  if (!currentProject()) shell.currentId = null;

  const form = $('#new-project-form');
  const nameInput = $('#project-name');
  const err = $('#project-err');
  $('#new-project').addEventListener('click', () => {
    form.hidden = false;
    nameInput.value = '';
    err.textContent = '';
    nameInput.classList.remove('invalid');
    nameInput.focus();
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const name = nameInput.value.trim();
    if (!name) {
      err.textContent = 'Введите название проекта — без него проект не создаётся.';
      nameInput.classList.add('invalid');
      nameInput.focus();
      return;
    }
    form.hidden = true;
    createProject(name);
  });
  form.querySelector('[data-cancel]').addEventListener('click', () => { form.hidden = true; });
  nameInput.addEventListener('input', () => { nameInput.classList.remove('invalid'); err.textContent = ''; });

  $('#projects').addEventListener('click', (e) => {
    const li = e.target.closest('li[data-id]');
    if (li && !shell.busy) openProject(li.dataset.id);
  });
  $('#file').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) ingestFile(file);
    e.target.value = '';
  });

  renderProjects();
  const p = currentProject();
  if (p && p.status === 'ready') showAnalysis();
  else showHome();
}

load().catch((err) => {
  $('#home').hidden = false;
  $('#composer').classList.add('disabled');
  $('#home-hint').replaceChildren(el('span', { class: 'error' }, `Не удалось загрузить данные: ${err.message}. Запуск: python -m http.server 8000 --directory web`));
});
