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
const FLAG_RU = {
  payer_of_couriers: 'платит известным курьерам',
  courier_candidate: 'кандидат в курьеры',
  beneficiary: 'получатель денег курьеров',
  in_cycle: 'в замкнутом маршруте',
  layering: 'цепочка наслоения',
  split_in: 'дробление переводов',
};
const QUEUE_SIZE = 30;

const state = { graph: null, byId: new Map(), ranked: [], selected: null };

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

function select(gid) {
  if (!state.byId.has(gid)) return;
  state.selected = gid;
  for (const tr of document.querySelectorAll('#queue tbody tr')) {
    tr.classList.toggle('selected', tr.dataset.gid === gid);
  }
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
}

load().catch((err) => {
  $('#meta').textContent = '';
  $('.left').prepend(el('div', { class: 'error' }, `Не удалось загрузить данные: ${err.message}. Запуск: python -m http.server 8000 --directory web`));
});
