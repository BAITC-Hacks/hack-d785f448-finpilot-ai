"""Проверка результата (после pipeline.py и addons_v3.py):
    python check.py --data data --out out [--prev out_prev]
Каждая проверка — ОК / ОШИБКА / ПРОПУЩЕНО. Выход 0 только если ошибок нет; пропуски перечислены явно."""
import argparse, json, sys
import numpy as np, pandas as pd
from pathlib import Path

REQUIRED = ["nodes_roles.csv", "clusters.csv", "top_nodes.csv", "graph.json", "run_meta.json",
            "nodes_flags.csv", "block_plan.csv", "levels.csv", "plan_meta.json"]
RULE_ROLE = {"R0": "peripheral", "R1": "coordinator", "R2": "distributor", "R3": "consolidator", "R3b": "consolidator",
             "R4x": "peripheral", "R4": "transit", "R5": "transit", "R6": "terminal", "R7": "peripheral",
             "R8a": "peripheral", "R8b": "peripheral", "R8c": "peripheral"}

def rule_holds(r, c, betw_cut):
    """Повторная проверка условия сработавшего правила по метрикам из выгрузки (не по тексту evidence)."""
    i, o = int(r.in_deg), int(r.out_deg); pas = r.pass_through; fast = 0.0 if pd.isna(r.fast_share) else r.fast_share
    lo, hi = c["transit_pass"]
    transit_cond = (not r.is_seed) and i >= 1 and o >= 1 and ((not pd.isna(pas) and lo <= pas <= hi) or fast >= c["fast_share"])
    return {
        "R0": i == 0 and o == 0,
        "R1": i >= c["coord_min_in"] and o >= c["coord_min_out"] and r.betweenness >= betw_cut and o <= c["coord_max_out_to_in"] * i,
        "R2": o >= c["fan_out"] and o >= c["fan_out_to_in"] * max(i, 1),
        "R3": i >= c["many_payers"] and 2 * o <= i,
        "R3b": (not r.is_seed) and r.seed_money >= c["money_sink_kzt"] and (o == 0 or (not pd.isna(pas) and pas <= c["money_sink_max_pass"])),
        "R4x": transit_cond and not r.seq_ok,
        "R4": transit_cond and bool(r.seq_ok),
        "R5": bool(r.is_seed) and o >= 1,
        "R6": (not r.truncated) and o == 0 and i >= 1 and (r.in_kzt >= c["terminal_min_kzt"] or i >= 2),
        "R7": bool(r.truncated),
        "R8a": o == 0 and i >= 1, "R8b": i >= 1 and o >= 1 and not pd.isna(pas) and pas > hi, "R8c": True,
    }[r.rule_id]

def main(data, out, prev):
    data, out = Path(data), Path(out); results = []
    def add(name, status, detail=""): results.append((name, status, detail))
    def chk(name, cond, detail=""): add(name, "ОК" if cond else "ОШИБКА", "" if cond else detail)
    missing = [f for f in REQUIRED if not (out / f).exists()]
    chk("заявленные файлы на месте", not missing, f"нет: {missing}")
    if missing:
        report(results); return
    nodes = pd.read_parquet(data / "nodes.parquet"); edges = pd.read_parquet(data / "edges.parquet"); tx = pd.read_parquet(data / "transactions.parquet")
    R = pd.read_csv(out / "nodes_roles.csv"); T = pd.read_csv(out / "top_nodes.csv"); C = pd.read_csv(out / "clusters.csv")
    F = pd.read_csv(out / "nodes_flags.csv"); P = pd.read_csv(out / "block_plan.csv")
    meta = json.load(open(out / "run_meta.json", encoding="utf-8")); pmeta = json.load(open(out / "plan_meta.json", encoding="utf-8"))
    c = meta["config"]; c["transit_pass"] = tuple(c["transit_pass"])
    # 1. узлы
    chk("все gid на месте, без дублей", len(R) == len(nodes) and R.gid.is_unique and set(R.gid) == set(nodes.gid), f"{len(R)} строк, уникальных {R.gid.nunique()}, в данных {len(nodes)}")
    chk("метки на те же gid", len(F) == len(R) and set(F.gid) == set(R.gid))
    # 2. транзакции ↔ рёбра
    agg = tx.groupby(["src", "dst"]).sum_kzt.sum().reset_index().merge(edges, on=["src", "dst"], how="outer", suffixes=("_tx", "_edge"))
    chk("транзакции сходятся с рёбрами", agg.isna().sum().sum() == 0 and np.allclose(agg.sum_kzt_tx, agg.sum_kzt_edge), "пары или суммы не совпадают")
    # 3. поля и диапазоны
    for col in ("role", "rule_id", "role_score", "cluster_id", "priority_score", "evidence"):
        chk(f"поле {col} заполнено", R[col].notna().all())
    chk("скоры в [0, 1]", R.role_score.between(0, 1).all() and R.priority_score.between(0, 1).all())
    chk("evidence ≤ 200 символов", R.evidence.str.len().le(200).all())
    # 4. правило ↔ роль, и условие правила выполняется по метрикам
    chk("rule_id известен", R.rule_id.isin(RULE_ROLE).all(), f"неизвестные: {sorted(set(R.rule_id) - set(RULE_ROLE))}")
    chk("роль соответствует rule_id", (R.rule_id.map(RULE_ROLE) == R.role).all())
    betw_cut = R.betweenness.quantile(c["coord_betweenness_quantile"])
    bad = [int(r.gid) for r in R.itertuples() if r.rule_id in RULE_ROLE and not rule_holds(r, c, betw_cut)]
    chk("условие сработавшего правила выполняется по метрикам", not bad, f"{len(bad)} узлов, например {bad[:3]}")
    # 5. приоритет = сумма вкладов, топ = сортировка основной таблицы
    raw = (R.p_money + R.p_turnover + R.p_betweenness + R.p_role + R.p_reach) * R.p_multiplier
    chk("приоритет складывается из вкладов", np.allclose(raw, R.priority_raw, atol=2e-3) and np.allclose((raw - raw.min()) / (raw.max() - raw.min()), R.priority_score, atol=2e-3))
    chk("топ-лист: 30 строк, ранги 1..30, gid уникальны", len(T) == c["top_n"] and T["rank"].tolist() == list(range(1, len(T) + 1)) and T.gid.is_unique)
    Rs = R.sort_values(["priority_score", "gid"], ascending=[False, True])
    top_scores = R.set_index("gid").priority_score
    chk("топ-лист = верх основной таблицы", set(T.gid) <= set(R.gid) and (T.gid.map(top_scores).values == T.priority_score.values).all()
        and T.priority_score.is_monotonic_decreasing and T.priority_score.min() >= Rs.priority_score.iloc[len(T) - 1] - 1e-9, "ранги не соответствуют сортировке по priority_score")
    chk("why заполнен", T.why.str.len().gt(0).all())
    chk("кластеры узлов есть в clusters.csv", set(R.cluster_id) <= set(C.cluster_id))
    # 6. край выборки
    tr = R[(R.depth == 4) & (R.out_deg == 0)]
    chk("4-е колено без исходящих отмечено как край выборки и не terminal", tr.truncated.all() and not (tr.role == "terminal").any())
    # 7. сходимость
    sm = meta["seed_money"]
    chk("след денег сошёлся", sm["converged"], f"{sm['iterations']} итераций, последнее изменение {sm['last_delta_kzt']} ₸")
    chk("расчёты плана сошлись", pmeta["converged"], f"не сошлись {pmeta['not_converged']} из {pmeta['propagations']}")
    # 8. план
    seeds = set(nodes.gid[nodes.is_seed]); seed_out = R[R.is_seed].out_kzt.sum()
    chk("план: capture в [0, 1], приросты ≥ 0, накопление построчно", P.cumulative.between(0, 1).all() and (P.gain >= -1e-9).all()
        and np.allclose(P.gain.cumsum(), P.cumulative, atol=0.002))
    chk("план: gid уникальны, без seed, есть в основной таблице", P.gid.is_unique and not P.gid.isin(seeds).any() and P.gid.isin(R.gid).all())
    chk("план: money_stopped = прирост × отток seed", np.allclose(P.money_stopped_kzt, P.gain * seed_out, atol=seed_out * 1e-3))
    chk("план: итог совпадает с plan_meta", abs(P.cumulative.iloc[-1] - pmeta["capture_30"]) < 1e-4)
    # 9. воспроизводимость
    if prev and (Path(prev) / "nodes_roles.csv").exists():
        Rp = pd.read_csv(Path(prev) / "nodes_roles.csv"); Pp = pd.read_csv(Path(prev) / "block_plan.csv") if (Path(prev) / "block_plan.csv").exists() else None
        same = Rp[["gid", "role", "rule_id", "priority_score"]].equals(R[["gid", "role", "rule_id", "priority_score"]]) and (Pp is None or Pp.equals(P))
        chk("повторный запуск воспроизводим", same, "роли, приоритеты или план отличаются от предыдущего запуска")
    else:
        add("повторный запуск воспроизводим", "ПРОПУЩЕНО", f"нет папки предыдущего запуска ({prev or '--prev не задан'})")
    report(results)

def report(results):
    err = [r for r in results if r[1] == "ОШИБКА"]; skip = [r for r in results if r[1] == "ПРОПУЩЕНО"]
    for name, st, det in results: print(f"[{st}] {name}" + (f" — {det}" if det else ""))
    print(f"итого: ошибок {len(err)}, пропущено {len(skip)}, проверок {len(results)}")
    sys.exit(1 if err else 0)

if __name__ == "__main__":
    a = argparse.ArgumentParser(); a.add_argument("--data", default="data"); a.add_argument("--out", default="out"); a.add_argument("--prev", default=None)
    n = a.parse_args(); main(n.data, n.out, n.prev)
