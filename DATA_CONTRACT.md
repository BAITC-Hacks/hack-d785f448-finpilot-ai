# Контракт данных для карты и карточки узла — один файл `out/graph.json`

Создаётся командой `python pipeline.py --data data --out out && python addons_v3.py --data data --out out`. Фронт читает только его.

## `nodes[]` — 2 248 узлов
| Поле | Тип | Смысл |
|---|---|---|
| `id` | string | gid (строкой — 18 цифр не влезают в JS number) |
| `role` | string | coordinator · consolidator · distributor · transit · terminal · peripheral |
| `rule_id` | string | сработавшее правило R0…R8c (см. RULES.md) |
| `role_score` | 0–1 | сила признаков правила (не вероятность) |
| `priority` | 0–1 | приоритет проверки; сортировать по нему |
| `priority_terms` | object | вклады: `money`, `turnover`, `betweenness`, `role`, `reach`, `multiplier` — для карточки «из чего сложился приоритет» |
| `evidence` | string ≤ 200 | объяснение роли с цифрами |
| `flags` | string[] | метки: payer_of_couriers · courier_candidate · beneficiary · in_cycle · layering · split_in |
| `flags_evidence` | string ≤ 200 | объяснение меток |
| `levels` | [{level, list}] | шорт-листы уровней 0–4, в которые входит узел |
| `plan_step` | int \| null | шаг в плане охвата (1–30) или null |
| `cluster` | int | кластер Louvain; 0 = без связей |
| `depth`, `seed`, `truncated` | int, bool, bool | колено 0–4; seed; край выборки (4-е колено без исходящих) |
| `in_deg`, `out_deg`, `in_kzt`, `out_kzt` | числа | плательщики, получатели, получено, отправлено |
| `pass_through`, `fast_share` | число \| null | доля переданного дальше; доля исходящих, покрытых входящими за ≤ 2 дня |
| `seed_money`, `seed_money_share` | ₸, доля | след денег курьеров и доля от 55,3 млн ₸ |
| `seed_reach`, `rep_in` | int | сколько seed имеют путь сюда; от скольких плательщиков 2+ перевода |

## `edges[]` — 3 119 рёбер
`source`, `target` (gid строкой), `sum_kzt`, `n_tx`. Направление = движение денег.

## `plan[]` и `plan_meta`
`plan`: 30 строк `step, gid, role, gain, cumulative, money_stopped_kzt`. `plan_meta.capture_30` = 0.3224 — «модельный охват выбранных счетов при фиксированных июльских потоках и пропорциональном смешивании». Не «заморозили бы».

## Карточка узла — порядок блоков
1. gid, роль, `rule_id`, `role_score`, кластер, колено, seed / край выборки
2. `evidence` — сработавшее правило с цифрами
3. `priority` и `priority_terms` — столбиками
4. `flags` + `flags_evidence`; `levels`
5. рёбра узла: входящие и исходящие с суммами
6. ограничения: «исходящие не наблюдаются в выборке» для out_deg = 0; «край выборки» для truncated; входящие извне не видны для seed

## Цвета ролей на карте
coordinator — красный · consolidator — оранжевый · distributor — фиолетовый · transit — жёлтый · terminal — зелёный · peripheral — серый · seed — обводка чёрным · truncated — пунктир.
