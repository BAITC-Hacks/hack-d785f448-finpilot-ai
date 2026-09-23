"""Надстройка версии 3 над pipeline.py (v3): метки поверх ролей, план перехвата, шорт-листы по уровням.

Запуск после pipeline.py:
    python addons_v3.py --data data --out out
Читает data/*.parquet и out/nodes_roles.csv, пишет out/nodes_flags.csv, out/block_plan.csv, out/levels.csv.
Все пороги — в CONFIG. Нет списков gid, нет внешних данных.
"""
import argparse, json, numpy as np, pandas as pd, networkx as nx

CONFIG = {
    "cashier_min_seeds": 2,         # F1: платит ≥ 2 известным курьерам
    "courier_L": 0.5,               # F2: доля исходящих в точки сбора
    "collector_min_direct": 2,      # точка сбора = получает напрямую от ≥ 2 seed
    "beneficiary_min_M": 500_000,   # F3: терминал с деньгами курьеров
    "cycle_max_len": 6,             # F4
    "split_min_days": 2,            # F6: пар (плательщик, день) с 2+ переводами
    "plan_size": 30, "plan_candidates": 40, "max_iter": 100, "tol_kzt": 1.0,
    "chain_max_edges": 3,           # F5: длина последовательной цепочки транзитов ищется на глубину ≤ 3 рёбер
}

def kzt(x):
    return f"{x/1e6:.1f} млн ₸" if x >= 1e6 else f"{x/1e3:.0f} тыс. ₸"

def main(data, out):
    c = CONFIG
    nodes = pd.read_parquet(f"{data}/nodes.parquet"); edges = pd.read_parquet(f"{data}/edges.parquet"); tx = pd.read_parquet(f"{data}/transactions.parquet")
    R = pd.read_csv(f"{out}/nodes_roles.csv", dtype={"gid": str})
    for df in (nodes, edges, tx):
        for col in ("gid", "src", "dst"):
            if col in df: df[col] = df[col].astype(str)
    seeds = set(nodes.loc[nodes.is_seed, "gid"])
    G = nx.DiGraph(); G.add_nodes_from(nodes.gid)
    for r in edges.itertuples(): G.add_edge(r.src, r.dst, w=float(r.sum_kzt))
    gids = list(G.nodes); idx = {g: i for i, g in enumerate(gids)}; N = len(gids)
    role = dict(zip(R.gid, R.role)); M = dict(zip(R.gid, R.seed_money)); prio = dict(zip(R.gid, R.priority_score)); trunc = dict(zip(R.gid, R.truncated))

    # ---------- распространение с блокировкой (§3б)
    src = np.array([idx[u] for u, v in G.edges]); dst = np.array([idx[v] for u, v in G.edges]); W = np.array([d["w"] for _, _, d in G.edges(data=True)])
    is_seed = np.zeros(N, bool); is_seed[[idx[s] for s in seeds]] = True
    in_kzt = np.zeros(N); np.add.at(in_kzt, dst, W); out_kzt = np.zeros(N); np.add.at(out_kzt, src, W)
    base_kzt = np.maximum(in_kzt, out_kzt)   # доля курьерских денег считается от max(I, O): сохранение массы
    total = W[is_seed[src]].sum()

    conv = {"runs": 0, "not_converged": 0, "max_iterations": 0, "max_last_delta_kzt": 0.0}

    def money_blocked(B):
        """M_B(v): след денег курьеров, если узлы B дальше не передают. Итерации до сходимости (как в pipeline.py);
        несошедшиеся расчёты считаются и попадают в plan_meta.json — план тогда не окончательный."""
        ab = np.zeros(N, bool); ab[list(B)] = True
        frac = is_seed.astype(float); money = np.zeros(N); done = False
        for it in range(c["max_iter"]):
            money = np.zeros(N); np.add.at(money, dst, W * frac[src])
            with np.errstate(divide="ignore", invalid="ignore"):
                new = np.where(is_seed, 1.0, np.where(base_kzt > 0, np.minimum(1.0, money / base_kzt), 0.0))
            new[ab] = 0.0
            delta = float(np.max(np.abs(new - frac) * base_kzt)); done = delta < c["tol_kzt"]
            frac = new
            if done: break
        conv["runs"] += 1; conv["max_iterations"] = max(conv["max_iterations"], it + 1)
        conv["max_last_delta_kzt"] = max(conv["max_last_delta_kzt"], delta)
        if not done: conv["not_converged"] += 1
        return money

    def capture(B):
        B = list(B)
        return money_blocked(B)[B].sum() / total if B else 0.0

    # ---------- метки
    pay = {g: sum(1 for x in G.successors(g) if x in seeds) for g in gids}
    pay_kzt = {g: sum(G[g][x]["w"] for x in G.successors(g) if x in seeds) for g in gids}
    direct = {g: sum(1 for u in G.predecessors(g) if u in seeds) for g in gids}
    collectors = {g for g in gids if direct[g] >= c["collector_min_direct"]}
    tx_col = tx[tx.dst.isin(collectors)]
    seed_days = set(zip(tx_col[tx_col.src.isin(seeds)].dst, tx_col[tx_col.src.isin(seeds)].date))
    L, sameday = {}, {}
    for g in gids:
        if g in seeds or G.out_degree(g) == 0: continue
        to_col = sum(G[g][x]["w"] for x in G.successors(g) if x in collectors)
        if to_col > 0:
            L[g] = to_col / G.out_degree(g, weight="w")
            t = tx_col[tx_col.src == g]
            sameday[g] = len(set(zip(t.dst, t.date)) & seed_days)
    cyc = set()
    for cy in nx.simple_cycles(G, length_bound=c["cycle_max_len"]): cyc.update(cy)
    transit = {g for g in gids if role[g] == "transit"}; Ht = G.subgraph(transit)
    def longest(g, nbrs, depth):
        """самый длинный простой путь по транзитным узлам от g, не длиннее depth рёбер"""
        best = 0
        stack = [(g, 0, {g})]
        while stack:
            v, d, seen = stack.pop(); best = max(best, d)
            if d < depth:
                for x in nbrs(v):
                    if x not in seen: stack.append((x, d + 1, seen | {x}))
        return best
    chain = {g: longest(g, Ht.successors, c["chain_max_edges"]) + longest(g, Ht.predecessors, c["chain_max_edges"]) for g in transit}
    tt = tx.groupby(["src", "dst", "date"]).size().reset_index(name="n")
    split = tt[tt.n >= 2].groupby("dst").size().to_dict()
    first_in = tx.groupby("dst").date.min().to_dict(); last_out = tx.groupby("src").date.max().to_dict()

    rows = []
    for g in gids:
        f1 = g not in seeds and pay[g] >= c["cashier_min_seeds"]
        f1w = g not in seeds and pay[g] == 1 and M[g] > 0
        f2 = g not in seeds and (L.get(g, 0) >= c["courier_L"] or sameday.get(g, 0) >= 1)
        f3 = G.out_degree(g) == 0 and not trunc[g] and M[g] >= c["beneficiary_min_M"]   # независимо от роли
        f4 = g in cyc
        f5 = role[g] == "transit" and chain.get(g, 0) >= 2   # v и ещё два транзита подряд
        f6 = split.get(g, 0) >= c["split_min_days"]
        dates_ok = (g in first_in and g in last_out and last_out[g] >= first_in[g])
        ev = []
        if f1: ev.append(f"переводит {pay[g]} известным курьерам {kzt(pay_kzt[g])} — признаки выплат курьерам")
        elif f1w: ev.append("переводит известному курьеру и получает деньги курьеров — возвратный поток")
        if f2: ev.append(f"платит в те же точки сбора, что известные курьеры ({L.get(g,0):.0%} исходящих" + (f", {sameday[g]} раз в тот же день)" if sameday.get(g, 0) else ")") + " — кандидат в курьеры")
        if f3: ev.append(f"конечный получатель: осело {kzt(M[g])} денег курьеров ({M[g]/total:.0%})")
        if f4: ev.append("стоит в замкнутом маршруте ≤ 6 шагов")
        if f5: ev.append(f"последовательная цепочка транзитов длиной {chain[g] + 1} узлов")
        if f6: ev.append(f"дробление: {split[g]} раз получал несколько переводов от одного плательщика за день")
        rows.append(dict(gid=g, payer_of_couriers=f1, payer_weak=f1w, courier_candidate=f2, beneficiary=f3, in_cycle=f4,
                         layering=f5, split_in=f6, dates_ok=dates_ok, pay_seeds=pay[g], pay_seeds_kzt=round(pay_kzt[g]),
                         L=round(L.get(g, 0), 2), sameday=sameday.get(g, 0), chain=chain.get(g, 0), split=split.get(g, 0),
                         flags_evidence="; ".join(ev)[:200]))
    F = pd.DataFrame(rows); F.to_csv(f"{out}/nodes_flags.csv", index=False)

    # ---------- план перехвата (§6)
    B, cur, plan, prev = [], money_blocked([]), [], 0.0
    for k in range(c["plan_size"]):
        cand = [int(j) for j in np.argsort(-cur) if not is_seed[j] and j not in B][:c["plan_candidates"]]
        best = max(cand, key=lambda j: capture(B + [j]))
        B.append(best); cum = capture(B)
        plan.append(dict(step=k + 1, gid=gids[best], role=role[gids[best]], gain=round(cum - prev, 4), cumulative=round(cum, 4),
                         money_stopped_kzt=round((cum - prev) * total)))   # именно предельный прирост, не M_B до блокировки
        prev = cum; cur = money_blocked(B)
    P = pd.DataFrame(plan); P.to_csv(f"{out}/block_plan.csv", index=False)
    meta = {"converged": conv["not_converged"] == 0, "propagations": conv["runs"], "not_converged": conv["not_converged"],
            "max_iterations": conv["max_iterations"], "max_last_delta_kzt": round(conv["max_last_delta_kzt"], 3), "tol_kzt": c["tol_kzt"],
            "capture_30": round(float(P.cumulative.iloc[-1]), 5), "seed_out_kzt": round(float(total)),
            "note": "модельный охват при фиксированных июльских потоках и пропорциональном смешивании; tol — порог изменения между итерациями, не погрешность результата"}
    json.dump(meta, open(f"{out}/plan_meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---------- шорт-листы по уровням (§7)
    lv = []
    def add(level, name, sel, key):
        for g in sorted(sel, key=key, reverse=True): lv.append(dict(level=level, list=name, gid=g, role=role[g], priority=prio[g], seed_money=round(M[g])))
    Fi = F.set_index("gid")
    add(0, "кассир", [g for g in gids if Fi.at[g, "payer_of_couriers"]], lambda g: pay[g] * 1e9 + pay_kzt[g])
    add(0, "кандидат в курьеры", [g for g in gids if Fi.at[g, "courier_candidate"]], lambda g: (L.get(g, 0), sameday.get(g, 0)))
    add(1, "точка сбора", [g for g in gids if role[g] == "consolidator"], lambda g: M[g])
    add(2, "цепочка наслоения", [g for g in gids if Fi.at[g, "layering"]], lambda g: (chain[g], M[g]))
    add(1, "точка сбора", [], lambda g: 0)  # (роль consolidator уже выше)
    add(3, "координатор", [g for g in gids if role[g] == "coordinator"], lambda g: prio[g])
    add(4, "получатель", [g for g in gids if Fi.at[g, "beneficiary"]], lambda g: M[g])
    pd.DataFrame(lv).to_csv(f"{out}/levels.csv", index=False)

    # ---------- метки и план — в graph.json, чтобы у карты был один источник данных
    gpath = f"{out}/graph.json"
    try:
        graph = json.load(open(gpath, encoding="utf-8"))
        Fi2 = F.set_index("gid"); plan_step = {str(r.gid): int(r.step) for r in P.itertuples()}
        level_of = {}
        for r in pd.DataFrame(lv).itertuples(): level_of.setdefault(str(r.gid), []).append({"level": int(r.level), "list": r.list})
        for n in graph["nodes"]:
            f = Fi2.loc[n["id"]]
            n["flags"] = [k for k in ("payer_of_couriers", "courier_candidate", "beneficiary", "in_cycle", "layering", "split_in") if bool(f[k])]
            n["flags_evidence"] = f["flags_evidence"] if isinstance(f["flags_evidence"], str) else ""
            n["plan_step"] = plan_step.get(n["id"]); n["levels"] = level_of.get(n["id"], [])
        graph["plan"] = P.to_dict(orient="records"); graph["plan_meta"] = meta
        json.dump(graph, open(gpath, "w", encoding="utf-8"), ensure_ascii=False)
    except FileNotFoundError:
        print("graph.json не найден — карта получит метки только из nodes_flags.csv")

    print(f"метки: кассир {int(F.payer_of_couriers.sum())} · кандидат в курьеры {int(F.courier_candidate.sum())} · получатель {int(F.beneficiary.sum())} · "
          f"в цикле {int(F.in_cycle.sum())} · цепочка {int(F.layering.sum())} · дробление {int(F.split_in.sum())}")
    print(f"план охвата: {len(P)} узлов → {P.cumulative.iloc[-1]:.2%} наблюдаемого потока денег курьеров; "
          f"сходимость: {'все ' + str(conv['runs']) + ' расчётов сошлись' if conv['not_converged'] == 0 else str(conv['not_converged']) + ' расчётов НЕ сошлись'}")
    top = [idx[g] for g in R.sort_values('priority_score', ascending=False).head(30).gid if g not in seeds]
    print(f"для сравнения: не-seed из топ-30 приоритета перехватывают {capture(top):.0%}")

if __name__ == "__main__":
    a = argparse.ArgumentParser(); a.add_argument("--data", default="data"); a.add_argument("--out", default="out")
    ns = a.parse_args(); main(ns.data, ns.out)
