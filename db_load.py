"""Загрузка результатов прогона в PostgreSQL (таблицы из db/init/001_schema.sql).

    python db_load.py --out out            # DATABASE_URL берётся из окружения
Идемпотентно: run_id = sha256(nodes_roles.csv); повторная загрузка того же результата ничего не дублирует.
Без DATABASE_URL — ничего не делает и выходит с кодом 0: локальный запуск без базы остаётся рабочим.
"""
import argparse, hashlib, json, os, sys, time
from pathlib import Path


def connect(url, attempts=30):
    import psycopg2
    for i in range(attempts):
        try:
            return psycopg2.connect(url)
        except psycopg2.OperationalError as ex:
            if i == attempts - 1:
                raise
            time.sleep(2)


def run_id_of(out):
    return hashlib.sha256((out / "nodes_roles.csv").read_bytes()).hexdigest()[:16]


def load(out, url):
    import pandas as pd
    from psycopg2.extras import execute_values
    out = Path(out); rid = run_id_of(out)
    meta = json.load(open(out / "run_meta.json", encoding="utf-8")); pmeta = json.load(open(out / "plan_meta.json", encoding="utf-8"))
    R = pd.read_csv(out / "nodes_roles.csv", dtype={"gid": str}); F = pd.read_csv(out / "nodes_flags.csv", dtype={"gid": str}); P = pd.read_csv(out / "block_plan.csv", dtype={"gid": str})
    con = connect(url); cur = con.cursor()
    cur.execute("select 1 from runs where run_id = %s", (rid,))
    if cur.fetchone():
        print(f"db_load: результат {rid} уже в базе, пропускаю"); con.close(); return rid
    cur.execute("insert into runs (run_id, nodes, edges, converged, iterations, capture_30) values (%s,%s,%s,%s,%s,%s)",
                (rid, int(meta["nodes"]), int(meta["edges"]), bool(meta["seed_money"]["converged"]), int(meta["seed_money"]["iterations"]), float(pmeta["capture_30"])))
    cols = ["gid", "role", "rule_id", "role_score", "cluster_id", "priority_score", "evidence", "depth", "is_seed", "truncated", "in_deg", "out_deg", "in_kzt", "out_kzt", "seed_money"]
    rows = [(rid, r.gid, r.role, r.rule_id, float(r.role_score), int(r.cluster_id), float(r.priority_score), r.evidence, int(r.depth), bool(r.is_seed), bool(r.truncated),
             int(r.in_deg), int(r.out_deg), float(r.in_kzt), float(r.out_kzt), float(r.seed_money)) for r in R[cols].itertuples(index=False)]
    execute_values(cur, "insert into nodes_roles (run_id," + ",".join(cols) + ") values %s", rows, page_size=500)
    fcols = ["payer_of_couriers", "courier_candidate", "beneficiary", "in_cycle", "layering", "split_in"]
    rows = [(rid, r.gid, *[bool(getattr(r, c)) for c in fcols], r.flags_evidence if isinstance(r.flags_evidence, str) else None) for r in F.itertuples(index=False)]
    execute_values(cur, "insert into node_flags (run_id, gid," + ",".join(fcols) + ", flags_evidence) values %s", rows, page_size=500)
    rows = [(rid, int(r.step), r.gid, r.role, float(r.gain), float(r.cumulative), float(r.money_stopped_kzt)) for r in P.itertuples(index=False)]
    execute_values(cur, "insert into block_plan (run_id, step, gid, role, gain, cumulative, money_stopped_kzt) values %s", rows)
    con.commit(); con.close()
    print(f"db_load: загружен результат {rid}: {len(R)} узлов, {len(F)} строк меток, {len(P)} шагов плана")
    return rid


def main():
    a = argparse.ArgumentParser(); a.add_argument("--out", default="out"); n = a.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("db_load: DATABASE_URL не задан — база не используется"); return 0
    load(n.out, url); return 0


if __name__ == "__main__":
    sys.exit(main())
