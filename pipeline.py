#!/usr/bin/env python3
"""
Граф денег — пайплайн: сырые parquet → метрики → роли → кластеры → приоритет.
Выход: out/nodes_roles.csv, out/clusters.csv, out/top_nodes.csv (схема ТЗ) + out/graph.json для карты.

Все роли — объяснимые правила с порогами из CONFIG. Никаких списков gid в коде.
Запуск:  python pipeline.py --data data --out out
"""
import argparse
import json
import math
import time
from collections import Counter
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

# ------------------------------------------------------------------ пороги
CONFIG = {
    # консолидация: «много плательщиков» = 3+ (95-й перцентиль входящей степени не-seed узлов)
    "many_payers": 3,
    # координатор: узел-посредник, и собирает, и рассылает, betweenness в топ-1%
    "coord_min_in": 5,
    "coord_min_out": 5,
    "coord_betweenness_quantile": 0.99,
    "coord_max_out_to_in": 10,        # если рассылка в 10+ раз шире сбора — это распределитель
    # распределитель: веерная рассылка
    "fan_out": 10,                    # 10+ получателей (~97-й перцентиль)
    "fan_out_to_in": 3,               # получателей в 3+ раза больше, чем плательщиков
    # транзит
    "transit_pass": (0.8, 1.2),       # ушло дальше 80–120% полученного
    "fast_days": 2,                   # «пришло и ушло за 1–2 дня»
    "fast_share": 0.7,                # 70%+ исходящих — в течение 2 дней после входа
    # конечный получатель: деньги пришли и остались (только колена 0–3 — там исходящие выгружены)
    "terminal_min_kzt": 100_000,
    # консолидация по деньгам: сюда дошло ≥ 500 тыс. ₸ денег курьеров, и дальше уходит ≤ 50% (или ничего)
    "money_sink_kzt": 500_000,        # ≈ 99-й перцентиль M(v) у не-seed узлов при сохранении массы
    "money_sink_max_pass": 0.5,
    # повторные связи: пара с 2+ переводами
    "repeat_min_tx": 2,
    # след денег курьеров: итерации до сходимости
    "seed_money_max_iter": 100,
    "seed_money_tol_kzt": 1.0,
    # приоритет
    "priority_weights": {"seed_money": 0.35, "turnover": 0.15, "betweenness": 0.10, "role": 0.30, "seed_reach": 0.10},
    "role_weight": {"coordinator": 1.0, "consolidator": 0.85, "distributor": 0.75,
                    "transit": 0.40, "terminal": 0.30, "peripheral": 0.10},
    "seed_multiplier": 0.85,          # seed уже известны — фокус выше по цепочке
    "truncated_multiplier": 0.8,      # 4-е колено: исходящие не выгружены, картина неполная
    "louvain_seed": 42,
    "top_n": 30,
}

ROLE_RU = {"coordinator": "кандидат в координаторы", "consolidator": "точка консолидации",
           "distributor": "распределитель", "transit": "транзит",
           "terminal": "конечный получатель", "peripheral": "периферия"}


def kzt(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.1f} млн ₸".replace(".", ",")
    if x >= 1e3:
        return f"{x / 1e3:.0f} тыс ₸"
    return f"{x:.0f} ₸"


def pl(n: int, one: str, many: str) -> str:
    """Родительный падеж после «от N» / дательный после «N»: 1, 21, 31… — единственное число."""
    n = int(n)
    return one if n % 10 == 1 and n % 100 != 11 else many


def cut(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ загрузка и граф
def load(data: Path):
    edges = pd.read_parquet(data / "edges.parquet")
    nodes = pd.read_parquet(data / "nodes.parquet")
    tx = pd.read_parquet(data / "transactions.parquet")
    tx["date"] = pd.to_datetime(tx["date"])
    return edges, nodes, tx


def build_graph(edges: pd.DataFrame) -> nx.DiGraph:
    G = nx.DiGraph()
    for r in edges.itertuples(index=False):
        G.add_edge(int(r.src), int(r.dst), w=float(r.sum_kzt), n=int(r.n_tx))
    return G


# ------------------------------------------------------------------ метрики
def seed_money(G: nx.DiGraph, seeds: set, blocked: set | None = None) -> dict:
    """След денег курьеров: сколько ₸ из исходящих seed дошло до узла.
    Допущение — пропорциональное смешивание (haircut): узел передаёт дальше ту же долю денег
    курьеров, что и в своих деньгах. Доля считается от max(I, O): если узел отправил больше,
    чем получил внутри графа, у него были невидимые поступления, и они разбавляют долю.
    Так узел никогда не передаёт дальше больше денег курьеров, чем получил (сохранение массы).
    Итерации — до сходимости (в сети есть циклы); blocked — узлы, которые дальше не передают."""
    blocked = blocked or set()
    in_kzt = dict(G.in_degree(weight="w")); out_kzt = dict(G.out_degree(weight="w"))
    base = {v: max(in_kzt[v], out_kzt[v]) for v in G.nodes}
    frac = {v: (1.0 if v in seeds else 0.0) for v in G.nodes}
    money = {v: 0.0 for v in G.nodes}
    for it in range(CONFIG["seed_money_max_iter"]):
        money = {v: 0.0 for v in G.nodes}
        for u, v, d in G.edges(data=True):
            money[v] += d["w"] * frac[u]
        new = {v: 0.0 if v in blocked else 1.0 if v in seeds else (min(1.0, money[v] / base[v]) if base[v] > 0 else 0.0)
               for v in G.nodes}
        delta = max(abs(new[v] - frac[v]) * base[v] for v in G.nodes)
        frac = new
        if delta < CONFIG["seed_money_tol_kzt"]:
            break
    seed_money.iterations = it + 1
    seed_money.converged = bool(delta < CONFIG["seed_money_tol_kzt"])
    seed_money.last_delta_kzt = float(delta)
    if not seed_money.converged:
        print(f"ВНИМАНИЕ: след денег не сошёлся за {it + 1} итераций, последнее изменение {delta:.0f} ₸ — результат не окончательный")
    return money


def features(G: nx.DiGraph, nodes: pd.DataFrame, tx: pd.DataFrame) -> pd.DataFrame:
    seeds = set(nodes.gid[nodes.is_seed].astype(int))
    df = nodes[["gid", "depth", "is_seed"]].copy()
    df["gid"] = df.gid.astype("int64")
    for name, view in [("in_deg", G.in_degree()), ("out_deg", G.out_degree()),
                       ("in_kzt", G.in_degree(weight="w")), ("out_kzt", G.out_degree(weight="w")),
                       ("in_tx", G.in_degree(weight="n")), ("out_tx", G.out_degree(weight="n"))]:
        df[name] = df.gid.map(dict(view)).fillna(0)
    df["orphan"] = ~df.gid.isin(G.nodes)
    df["pass_through"] = np.where(df.in_kzt > 0, df.out_kzt / df.in_kzt.where(df.in_kzt > 0), np.nan)
    df["truncated"] = (df.depth == 4) & (df.out_deg == 0)
    # сколько разных seed-курьеров имеют путь до узла (деньги каких курьеров сюда доходят)
    df["seed_reach"] = df.gid.map(lambda g: len(nx.ancestors(G, g) & seeds) if g in G else 0)
    df["seed_direct"] = df.gid.map(lambda g: sum(p in seeds for p in G.predecessors(g)) if g in G else 0)
    # повторные связи: от скольких разных плательщиков пришло 2+ перевода
    rep = {}
    for u, v, d in G.edges(data=True):
        if d["n"] >= CONFIG["repeat_min_tx"]:
            rep[v] = rep.get(v, 0) + 1
    df["rep_in"] = df.gid.map(rep).fillna(0).astype(int)
    # след денег курьеров
    sm = seed_money(G, seeds)
    df["seed_money"] = df.gid.map(sm).fillna(0.0)
    seed_out = float(df.loc[df.is_seed, "out_kzt"].sum())
    df["seed_money_share"] = df.seed_money / seed_out if seed_out > 0 else 0.0
    # посредничество: доля кратчайших путей через узел (направленный граф)
    df["betweenness"] = df.gid.map(nx.betweenness_centrality(G)).fillna(0.0)
    df["pagerank"] = df.gid.map(nx.pagerank(G, weight="w")).fillna(0.0)
    # возвратные потоки: сколько контрагентов переводят и ему, и от него
    df["reciprocal"] = df.gid.map(
        lambda g: sum(G.has_edge(v, g) for v in G.successors(g)) if g in G else 0)
    # временные паттерны: доля исходящих денег, которую можно объяснить входящими за ≤ N дней до них.
    # Сопоставление по суммам: входящий перевод «покрывает» исходящие не больше своей суммы
    # (один входящий на 20 000 ₸ не подтверждает исходящие на 600 000 ₸).
    lag = np.timedelta64(CONFIG["fast_days"], "D")
    tin = {g: s.sort_values("date")[["date", "sum_kzt"]].values for g, s in tx.groupby("dst")}
    fast = {}
    for g, o in tx.groupby("src"):
        if g not in tin:
            continue
        ins = [[d, s] for d, s in tin[g]]          # остаток каждого входящего
        ok = 0.0
        for d, s in sorted(zip(o.date.values, o.sum_kzt.values)):
            need = s
            for rec in ins:
                if rec[1] <= 0 or rec[0] > d:
                    continue
                if d - rec[0] > lag:
                    continue
                take = min(need, rec[1]); rec[1] -= take; need -= take
                if need <= 0:
                    break
            ok += s - need
        fast[g] = ok / o.sum_kzt.sum()
    df["fast_share"] = df.gid.map(fast)
    # временная последовательность: последний исходящий не раньше первого входящего (один день — совместимо).
    # Не доказательство транзита, а вето: если все исходящие раньше первого входящего, наблюдаемые поступления их не объясняют
    first_in = tx.groupby("dst").date.min(); last_out = tx.groupby("src").date.max()
    df["first_in_date"] = df.gid.map(first_in); df["last_out_date"] = df.gid.map(last_out)
    df["seq_ok"] = ~((df.first_in_date.notna()) & (df.last_out_date.notna()) & (df.last_out_date < df.first_in_date))
    # синхронность: максимум разных плательщиков за один день
    sync = tx.groupby(["dst", "date"]).src.nunique().groupby("dst").max()
    df["sync_payers_day"] = df.gid.map(sync).fillna(0).astype(int)
    return df


# ------------------------------------------------------------------ роли
def assign_roles(df: pd.DataFrame) -> pd.DataFrame:
    c = CONFIG
    betw_cut = df.betweenness.quantile(c["coord_betweenness_quantile"])
    lo, hi = c["transit_pass"]
    out = []
    for r in df.itertuples(index=False):
        i, o = int(r.in_deg), int(r.out_deg)
        pas = r.pass_through
        fast = r.fast_share if not pd.isna(r.fast_share) else 0.0
        seed_note = "; seed: входящие извне выборки не видны" if r.is_seed else ""
        if r.orphan:
            out.append(("R0", "peripheral", 0.5, "нет переводов от 5 000 ₸ внутри банка за июль — вне сети"))
        elif i >= c["coord_min_in"] and o >= c["coord_min_out"] and r.betweenness >= betw_cut \
                and o <= c["coord_max_out_to_in"] * i:
            score = min(0.95, 0.7 + 0.25 * min(1.0, (i + o) / 100))
            out.append(("R1", "coordinator", score,
                        f"получает от {i} {pl(i, 'плательщика', 'плательщиков')} и рассылает {o} {pl(o, 'получателю', 'получателям')} ({kzt(r.in_kzt)} → {kzt(r.out_kzt)}); "
                        f"узел-посредник, betweenness в топ-1% — признаки координатора{seed_note}"))
        elif o >= c["fan_out"] and o >= c["fan_out_to_in"] * max(i, 1):
            score = min(0.95, 0.6 + 0.35 * min(1.0, (o - c["fan_out"]) / 100))
            out.append(("R2", "distributor", score,
                        f"рассылает {kzt(r.out_kzt)} {o} {pl(o, 'получателю', 'получателям')} ({int(r.out_tx)} переводов), "
                        f"плательщиков {i} — признаки веерной раздачи{seed_note}"))
        elif i >= c["many_payers"] and 2 * o <= i:
            score = 0.55 + 0.4 * min(1.0, (i - c["many_payers"]) / 15)
            tail = ("; 4-е колено: исходящие не выгружены" if r.truncated
                    else seed_note if r.is_seed
                    else f", отдаёт дальше {pas:.0%} полученного")
            if r.truncated or r.is_seed:
                score *= 0.75
            out.append(("R3", "consolidator", min(0.95, score),
                        f"получает {kzt(r.in_kzt)} от {i} {pl(i, 'плательщика', 'плательщиков')}{tail} — признаки консолидации"))
        elif not r.is_seed and r.seed_money >= c["money_sink_kzt"] and (o == 0 or (not pd.isna(pas) and pas <= c["money_sink_max_pass"])):
            score = 0.6 + 0.35 * min(1.0, (r.seed_money - c["money_sink_kzt"]) / 5e6)
            if r.truncated or r.is_seed:
                score *= 0.75
            tail = ("; 4-е колено: исходящие не выгружены" if r.truncated
                    else f", дальше ушло {pas:.0%}" if not pd.isna(pas) and not r.is_seed else "")
            out.append(("R3b", "consolidator", min(0.95, score),
                        f"сюда дошло {kzt(r.seed_money)} денег курьеров ({r.seed_money_share:.0%} от всех) "
                        f"от {i} {pl(i, 'плательщика', 'плательщиков')}{tail} — признаки консолидации"))
        elif not r.is_seed and i >= 1 and o >= 1 and ((lo <= pas <= hi) or fast >= c["fast_share"]) and not r.seq_ok:
            out.append(("R4x", "peripheral", 0.4,
                        f"вход {kzt(r.in_kzt)} → выход {kzt(r.out_kzt)}, но все исходящие раньше первого входящего — "
                        f"временная последовательность не подтверждает транзит наблюдаемых поступлений"))
        elif not r.is_seed and i >= 1 and o >= 1 and ((lo <= pas <= hi) or fast >= c["fast_share"]):
            both = (lo <= pas <= hi) and fast >= c["fast_share"]
            out.append(("R4", "transit", 0.85 if both else 0.65,
                        f"пропускает дальше {pas:.0%} полученного ({kzt(r.in_kzt)} → {kzt(r.out_kzt)}); "
                        f"{fast:.0%} ушло за ≤{c['fast_days']} дн. — признаки транзита"))
        elif r.is_seed and o >= 1:
            out.append(("R5", "transit", 0.55,
                        f"seed-курьер, транзит: передаёт дальше {kzt(r.out_kzt)} {o} {pl(o, 'получателю', 'получателям')}; "
                        f"входящие извне выборки не видны"))
        elif not r.truncated and o == 0 and i >= 1 and (r.in_kzt >= c["terminal_min_kzt"] or i >= 2):
            score = 0.5 + 0.4 * min(1.0, math.log10(max(r.in_kzt, 1e5) / 1e5) / 2)
            out.append(("R6", "terminal", score,
                        f"конечный получатель в выборке: получил {kzt(r.in_kzt)} от {i} {pl(i, 'плательщика', 'плательщиков')}, "
                        f"исходящих от 5 000 ₸ в банке не наблюдается"))
        elif r.truncated:
            out.append(("R7", "peripheral", 0.3,
                        f"граница выборки (4-е колено): получил {kzt(r.in_kzt)} от {i}, "
                        f"исходящие не выгружены — роль не определить"))
        elif o == 0 and i >= 1:
            out.append(("R8a", "peripheral", 0.5,
                        f"разовый входящий {kzt(r.in_kzt)} от 1 плательщика, дальше не отправлял; "
                        f"меньше {kzt(c['terminal_min_kzt'])} — признаков роли нет"))
        elif i >= 1 and o >= 1 and pas > hi:
            out.append(("R8b", "peripheral", 0.4,
                        f"отдаёт {pas:.0%} от полученного в выборке ({kzt(r.in_kzt)} → {kzt(r.out_kzt)}): "
                        f"входящие извне не видны — признаков роли по данным недостаточно"))
        else:
            out.append(("R8c", "peripheral", 0.5,
                        f"вход {i} / выход {o}, оборот {kzt(r.in_kzt + r.out_kzt)} — признаков роли нет"))
    df = df.copy()
    df["rule_id"] = [x[0] for x in out]
    df["role"] = [x[1] for x in out]
    df["role_score"] = [round(float(x[2]), 3) for x in out]
    df["evidence"] = [cut(x[3]) for x in out]
    return df


# ------------------------------------------------------------------ кластеры
def clusters(G: nx.DiGraph, df: pd.DataFrame):
    # Louvain работает на неориентированной проекции — направление учитывается в ролях, не в кластерах
    UG = nx.Graph()
    for u, v, d in G.edges(data=True):
        w = UG[u][v]["w"] + d["w"] if UG.has_edge(u, v) else d["w"]
        UG.add_edge(u, v, w=w)
    comms = sorted(nx.community.louvain_communities(UG, weight="w", seed=CONFIG["louvain_seed"]),
                   key=len, reverse=True)
    cid = {g: k + 1 for k, c in enumerate(comms) for g in c}
    df = df.copy()
    df["cluster_id"] = df.gid.map(cid).fillna(0).astype(int)   # 0 — изолированные узлы без переводов
    return df


def cluster_table(G: nx.DiGraph, df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for k, g in df.groupby("cluster_id"):
        members = set(g.gid)
        internal = sum(d["w"] for u, v, d in G.edges(data=True) if u in members and v in members)
        top = g.sort_values("priority_score", ascending=False)
        core = top[top.role.isin(["coordinator", "consolidator", "distributor"])]
        lead = core.iloc[0] if len(core) else top.iloc[0]
        roles = Counter(g.role)
        n, n_seed = len(g), int(g.is_seed.sum())
        if k == 0:
            hyp = f"{n} seed без переводов от 5 000 ₸ внутри банка — вне сети, нужны данные других банков"
        elif n_seed >= 2 and (roles["consolidator"] or roles["coordinator"]):
            hyp = (f"сбор денег от {n_seed} курьеров: точка сбора {lead.gid} ({ROLE_RU[lead.role]}), "
                   f"внутренний оборот {kzt(internal)}")
        elif roles["distributor"]:
            hyp = f"веерная раздача: {roles['distributor']} распределитель(я), {n} узлов, оборот {kzt(internal)}"
        elif roles["transit"] >= 0.3 * n:
            hyp = f"транзитная цепочка: {roles['transit']} транзитных узлов из {n}"
        elif roles["terminal"] >= 0.5 * n:
            hyp = f"оседание средств: {roles['terminal']} конечных получателей из {n}"
        else:
            hyp = f"периферийная группа: {n} узлов со слабыми связями"
        rows.append({"cluster_id": int(k), "n_nodes": n, "n_seed": n_seed,
                     "sum_kzt_internal": round(internal, 2),
                     "top_gids": ";".join(str(x) for x in top.gid.head(3)),
                     "hypothesis": "гипотеза: " + hyp})
    return pd.DataFrame(rows).sort_values("cluster_id")


# ------------------------------------------------------------------ приоритет
def priority(df: pd.DataFrame) -> pd.DataFrame:
    c, w = CONFIG, CONFIG["priority_weights"]
    df = df.copy()
    pct = lambda s: s.rank(pct=True, method="average")
    # деньги курьеров — по абсолютной величине (корень сглаживает разброс), остальное — по рангу
    money_norm = np.sqrt(df.seed_money / df.seed_money.max()) if df.seed_money.max() > 0 else 0.0
    # вклад каждого слагаемого сохраняется в выгрузку — карточка узла показывает, из чего сложился приоритет
    df["p_money"] = (w["seed_money"] * money_norm).round(4)
    df["p_turnover"] = (w["turnover"] * pct(np.log1p(df.in_kzt + df.out_kzt))).round(4)
    df["p_betweenness"] = (w["betweenness"] * pct(df.betweenness)).round(4)
    df["p_role"] = (w["role"] * df.role.map(c["role_weight"])).round(4)
    df["p_reach"] = (w["seed_reach"] * pct(df.seed_reach)).round(4)
    df["p_multiplier"] = np.where(df.is_seed, c["seed_multiplier"], 1.0) * np.where(df.truncated, c["truncated_multiplier"], 1.0)
    raw = (df.p_money + df.p_turnover + df.p_betweenness + df.p_role + df.p_reach) * df.p_multiplier
    df["priority_raw"] = raw.round(4)
    df["priority_score"] = ((raw - raw.min()) / (raw.max() - raw.min())).round(4)
    df["betw_pct"] = pct(df.betweenness)
    return df


def why(r) -> str:
    k = int(r.seed_reach)
    s = (f"{ROLE_RU[r.role]}: {r.evidence}. Дошло {kzt(r.seed_money)} денег курьеров — {r.seed_money_share:.0%} от всех; "
         f"путь сюда есть у {k} из 81 seed-клиентов; оборот {kzt(r.in_kzt + r.out_kzt)}")
    if r.betw_pct >= 0.9:
        s += f"; посредник: betweenness выше, чем у {r.betw_pct:.0%} узлов"
    return s


# ------------------------------------------------------------------ выгрузки
def export(G, df, cl, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    extra = ["depth", "is_seed", "truncated", "in_deg", "out_deg", "in_kzt", "out_kzt", "in_tx", "out_tx",
             "pass_through", "seed_reach", "seed_direct", "seed_money", "seed_money_share", "rep_in",
             "betweenness", "pagerank", "fast_share", "seq_ok", "reciprocal", "sync_payers_day",
             "p_money", "p_turnover", "p_betweenness", "p_role", "p_reach", "p_multiplier", "priority_raw"]
    cols = ["gid", "role", "rule_id", "role_score", "cluster_id", "priority_score", "evidence"]
    df[cols + extra].to_csv(out / "nodes_roles.csv", index=False)
    meta = {"seed_money": {"converged": seed_money.converged, "iterations": seed_money.iterations,
                           "last_delta_kzt": round(seed_money.last_delta_kzt, 3), "tol_kzt": CONFIG["seed_money_tol_kzt"],
                           "note": "tol — порог изменения между итерациями, не гарантированная погрешность результата"},
            "config": {k: v for k, v in CONFIG.items()}, "nodes": int(len(df)), "edges": int(G.number_of_edges())}
    (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    cl.to_csv(out / "clusters.csv", index=False)
    top = df.sort_values("priority_score", ascending=False).head(CONFIG["top_n"]).copy()
    top.insert(0, "rank", range(1, len(top) + 1))
    top["why"] = [why(r) for r in top.itertuples(index=False)]
    top[["rank", "gid", "role", "priority_score", "why"]].to_csv(out / "top_nodes.csv", index=False)
    graph = {
        "nodes": [{"id": str(r.gid), "role": r.role, "rule_id": r.rule_id, "role_score": r.role_score, "cluster": int(r.cluster_id),
                   "priority": float(r.priority_score), "depth": int(r.depth), "seed": bool(r.is_seed),
                   "truncated": bool(r.truncated), "in_deg": int(r.in_deg), "out_deg": int(r.out_deg),
                   "in_kzt": float(r.in_kzt), "out_kzt": float(r.out_kzt), "seed_reach": int(r.seed_reach),
                   "seed_money": float(r.seed_money), "seed_money_share": round(float(r.seed_money_share), 4), "rep_in": int(r.rep_in),
                   "pass_through": None if pd.isna(r.pass_through) else round(float(r.pass_through), 3),
                   "fast_share": None if pd.isna(r.fast_share) else round(float(r.fast_share), 3),
                   "priority_terms": {"money": float(r.p_money), "turnover": float(r.p_turnover), "betweenness": float(r.p_betweenness),
                                      "role": float(r.p_role), "reach": float(r.p_reach), "multiplier": float(r.p_multiplier)},
                   "evidence": r.evidence} for r in df.itertuples(index=False)],
        "edges": [{"source": str(u), "target": str(v), "sum_kzt": d["w"], "n_tx": d["n"]}
                  for u, v, d in G.edges(data=True)],
    }
    (out / "graph.json").write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    return top


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    t0 = time.time()
    edges, nodes, tx = load(Path(a.data))
    G = build_graph(edges)
    df = features(G, nodes, tx)
    df = assign_roles(df)
    df = clusters(G, df)
    df = priority(df)
    cl = cluster_table(G, df)
    top = export(G, df, cl, Path(a.out))
    assert len(df) == len(nodes) == 2248 or len(df) == len(nodes), "каждый узел должен попасть в выгрузку"
    print(f"узлов: {len(df)}, рёбер: {G.number_of_edges()}, кластеров: {len(cl)}")
    print("роли:", df.role.value_counts().to_dict())
    print(f"выгрузки: {a.out}/nodes_roles.csv, clusters.csv, top_nodes.csv, graph.json, run_meta.json")
    print(f"след денег: {'сошёлся' if seed_money.converged else 'НЕ СОШЁЛСЯ'} за {seed_money.iterations} итераций, последнее изменение {seed_money.last_delta_kzt:.2f} ₸")
    print(f"время: {time.time() - t0:.1f} с")


if __name__ == "__main__":
    main()
