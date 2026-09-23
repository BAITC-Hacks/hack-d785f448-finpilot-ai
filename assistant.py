#!/usr/bin/env python3
"""Ассистент AML-аналитика над результатами пайплайна «Граф денег».

Принцип: код считает факты (граф, роли, след денег, приоритет — из out/graph.json), модель только
объясняет факты, выбирает следующий шаг и формулирует запрос. Модель не видит датасет и не считает.

Команды:
  python assistant.py explain <gid>                 сцена 1 — факты по узлу и объяснение
  python assistant.py decide [--k 3]                сцена 2 — кого из топ-k проверять первым и почему
  python assistant.py whatif --exclude <gid>        сцена 3 — аналитик исключил узел (легальный бизнес) → пересчёт следа денег и приоритета
  python assistant.py whatif --add-seed <gid>       сцена 3 — добавили нового курьера → пересчёт
  python assistant.py request <gid> [--confirm]     сцена 4 — черновик запроса; без --confirm ничего не отправляется и не сохраняется
  python assistant.py demo                          все четыре сцены подряд
  python assistant.py serve [--port 8765]           HTTP для карточки узла: /api/explain?gid= /api/decide /api/whatif?exclude= /api/request?gid=

Режимы модели (выбирается автоматически):
  live    — OPENAI_API_KEY задан: OpenAI Responses API, модель из OPENAI_MODEL (по умолчанию gpt-6-luna), структурированный ответ.
  nvidia  — только NVIDIA_API_KEY: OpenAI-совместимый endpoint NVIDIA, модель из NVIDIA_MODEL (по умолчанию google/gemma-4-31b-it).
  replay  — ключей нет или задан --replay: ответы из fixtures/replay.json; если фикстуры нет — шаблонное объяснение из фактов.
--record сохраняет живые ответы в fixtures/replay.json, чтобы демо работало без сети.

Журнал: каждый вызов дописывается в out/agent_runs.jsonl; при DATABASE_URL (PostgreSQL из compose.yaml) — ещё и в таблицу agent_runs,
подтверждённые запросы — в approvals. Ключи в код и в Git не попадают: только переменные окружения (.env, см. .env.example).
"""
import argparse, hashlib, json, os, sys, time, urllib.request
from threading import Lock
from pathlib import Path

OUT = Path(os.environ.get("GRAPH_OUT", "out")); FIX = Path("fixtures/replay.json")
ROLE_RU = {"coordinator": "кандидат в координаторы", "consolidator": "точка консолидации", "distributor": "распределитель",
           "transit": "транзит", "terminal": "конечный получатель", "peripheral": "периферия"}
FLAG_RU = {"payer_of_couriers": "платит известным курьерам", "courier_candidate": "кандидат в курьеры", "beneficiary": "получатель денег курьеров",
           "in_cycle": "в замкнутом маршруте", "layering": "цепочка наслоения", "split_in": "дробление переводов"}
WEIGHTS = {"money": 0.35}

SYSTEM = ("Ты — ассистент AML-аналитика банка. Тебе дают рассчитанные кодом факты по узлам транзакционного графа: роль по явному правилу, "
          "цифры, метки, след денег известных курьеров, приоритет. Ты не считаешь ничего сам и не выдумываешь данных. "
          "Объясняй факты простым языком, формулируй гипотезы осторожно («признаки», «возможно»), называй альтернативные объяснения, "
          "указывай, каких данных не хватает, и предлагай следующий шаг. Никаких утверждений о виновности. "
          "seed_reach — число других seed, из которых существует направленный путь seed → … → выбранный клиент. "
          "Это НЕ число seed, которым выбранный клиент отправляет деньги. top_in — плательщики, top_out — получатели. "
          "Достижимость не доказывает происхождение конкретных денег; seed_money — оценка модели смешивания. Отвечай по-русски, кратко.")


def kzt(x):
    x = float(x or 0)
    return f"{x/1e6:.1f} млн ₸" if x >= 1e6 else f"{x/1e3:.0f} тыс. ₸" if x >= 1e3 else f"{x:.0f} ₸"


# ------------------------------------------------------------------ факты (детерминированный слой)
class Graph:
    def __init__(self, out=OUT):
        self.out = Path(out)
        g = json.load(open(out / "graph.json", encoding="utf-8"))
        self.nodes = {n["id"]: n for n in g["nodes"]}; self.edges = g["edges"]
        self.plan = g.get("plan", []); self.plan_meta = g.get("plan_meta", {})
        self.inc, self.outg = {}, {}
        for e in self.edges:
            self.outg.setdefault(e["source"], []).append(e); self.inc.setdefault(e["target"], []).append(e)
        self.seed_out = sum(e["sum_kzt"] for e in self.edges if self.nodes[e["source"]]["seed"])

    def node(self, gid):
        n = self.nodes.get(str(gid))
        if not n: raise KeyError(f"gid {gid} нет в графе")
        return n

    def facts(self, gid):
        n = self.node(gid)
        inc = sorted(self.inc.get(n["id"], []), key=lambda e: -e["sum_kzt"]); outg = sorted(self.outg.get(n["id"], []), key=lambda e: -e["sum_kzt"])
        gaps = []
        if n["seed"]: gaps.append("входящие переводы seed извне выборки не выгружены")
        if n["truncated"]: gaps.append("край выборки (4-е колено): исходящие не выгружены")
        if n["out_deg"] == 0 and not n["truncated"]: gaps.append("исходящие от 5 000 ₸ внутри банка не наблюдаются: остатки, наличные и другие банки не видны")
        if n["out_kzt"] > n["in_kzt"] and not n["seed"]:
            gaps.append(f"отправил на {kzt(n['out_kzt'] - n['in_kzt'])} больше, чем получил в графе — поступления извне выборки или начальный остаток")
        tr = [e["target"] for e in outg if self.nodes[e["target"]]["truncated"]]
        if tr: gaps.append(f"{len(tr)} получателей стоят на краю выборки — их исходящие не выгружены")
        return {
            "gid": n["id"], "role": n["role"], "role_ru": ROLE_RU[n["role"]], "rule_id": n["rule_id"], "role_score": n["role_score"],
            "evidence": n["evidence"], "priority": n["priority"], "priority_terms": n.get("priority_terms", {}),
            "rank": self.rank(n["id"]), "cluster": n["cluster"], "depth": n["depth"], "seed": n["seed"], "truncated": n["truncated"],
            "in_deg": n["in_deg"], "out_deg": n["out_deg"], "in_kzt": n["in_kzt"], "out_kzt": n["out_kzt"],
            "seed_money_kzt": round(n["seed_money"]), "seed_money_share": n.get("seed_money_share", 0), "seed_reach": n["seed_reach"],
            "seed_reach_direction": "seed → … → выбранный клиент",
            "seed_reach_definition": "Число других seed с направленным путём к этому клиенту; без ограничения длины пути. Обратное направление не измеряется.",
            "flags": [FLAG_RU.get(f, f) for f in n.get("flags", [])], "flags_evidence": n.get("flags_evidence", ""),
            "levels": n.get("levels", []), "plan_step": n.get("plan_step"),
            "top_in": [{"from": e["source"], "role": self.nodes[e["source"]]["role"], "sum_kzt": e["sum_kzt"], "n_tx": e["n_tx"]} for e in inc[:5]],
            "top_out": [{"to": e["target"], "role": self.nodes[e["target"]]["role"], "sum_kzt": e["sum_kzt"], "n_tx": e["n_tx"]} for e in outg[:5]],
            "path_from_seed": self.path_from_seed(n["id"]), "data_gaps": gaps,
        }

    def rank(self, gid):
        order = sorted(self.nodes.values(), key=lambda x: -x["priority"])
        return next(i + 1 for i, x in enumerate(order) if x["id"] == gid)

    def top(self, k=30):
        return sorted(self.nodes.values(), key=lambda x: -x["priority"])[:k]

    def path_from_seed(self, gid):
        """Кратчайший путь от какого-либо seed до узла (BFS по входящим рёбрам)."""
        if self.nodes[gid]["seed"]: return [{"gid": gid, "role": "seed"}]
        prev, frontier, seen = {}, [gid], {gid}
        while frontier:
            nxt = []
            for v in frontier:
                for e in self.inc.get(v, []):
                    u = e["source"]
                    if u in seen: continue
                    seen.add(u); prev[u] = (v, e["sum_kzt"])
                    if self.nodes[u]["seed"]:
                        path, cur = [], u
                        while cur != gid:
                            v2, s = prev[cur]; path.append({"gid": cur, "role": self.nodes[cur]["role"], "sum_kzt": s}); cur = v2
                        path.append({"gid": gid, "role": self.nodes[gid]["role"]}); return path
                    nxt.append(u)
            frontier = nxt
        return []

    # ---- сцена 3: пересчёт следа денег и приоритета при изменении условий (та же формула, что в pipeline.py)
    def whatif(self, exclude=None, add_seed=None, k=10):
        ids = list(self.nodes); idx = {g: i for i, g in enumerate(ids)}; N = len(ids)
        seeds = [self.nodes[g]["seed"] for g in ids]
        if add_seed: seeds[idx[str(add_seed)]] = True
        blocked = {str(exclude)} if exclude else set()
        in_k = [0.0] * N; out_k = [0.0] * N
        for e in self.edges: in_k[idx[e["target"]]] += e["sum_kzt"]; out_k[idx[e["source"]]] += e["sum_kzt"]
        base = [max(a, b) for a, b in zip(in_k, out_k)]
        frac = [1.0 if s else 0.0 for s in seeds]; money = [0.0] * N
        for _ in range(100):
            money = [0.0] * N
            for e in self.edges: money[idx[e["target"]]] += e["sum_kzt"] * frac[idx[e["source"]]]
            new = [0.0 if ids[i] in blocked else 1.0 if seeds[i] else (min(1.0, money[i] / base[i]) if base[i] > 0 else 0.0) for i in range(N)]
            delta = max(abs(new[i] - frac[i]) * base[i] for i in range(N)); frac = new
            if delta < 1.0: break
        mx = max(money) or 1.0
        raw = {}
        for i, g in enumerate(ids):
            t = self.nodes[g].get("priority_terms", {})
            p_money = WEIGHTS["money"] * (money[i] / mx) ** 0.5
            mult = t.get("multiplier", 1.0) * (0.85 if (add_seed and g == str(add_seed)) else 1.0)
            raw[g] = (p_money + t.get("turnover", 0) + t.get("betweenness", 0) + t.get("role", 0) + t.get("reach", 0)) * mult
        lo, hi = min(raw.values()), max(raw.values())
        new_p = {g: (v - lo) / (hi - lo) for g, v in raw.items()}
        before = [n["id"] for n in self.top(k)]
        after = [g for g in sorted(ids, key=lambda g: -new_p[g]) if g not in blocked][:k]
        moved = [{"gid": g, "was": (before.index(g) + 1 if g in before else None), "now": after.index(g) + 1,
                  "seed_money_kzt_before": round(self.nodes[g]["seed_money"]), "seed_money_kzt_after": round(money[idx[g]])}
                 for g in after if g not in before or before.index(g) != after.index(g)]
        return {"change": {"exclude": exclude, "add_seed": add_seed}, "converged": delta < 1.0,
                "top_before": before, "top_after": after, "entered": [g for g in after if g not in before], "left": [g for g in before if g not in after],
                "moved": moved, "note": "пересчитаны след денег курьеров и приоритет; роли и кластеры не пересчитываются"}

    def draft_request(self, gid):
        f = self.facts(gid)
        lines = [f"Запрос сведений по клиенту {f['gid']}", "",
                 f"Основание: {f['role_ru']} по правилу {f['rule_id']} — {f['evidence']}.",
                 f"Приоритет проверки: {f['rank']} из {len(self.nodes)}; след денег известных курьеров: {kzt(f['seed_money_kzt'])} ({f['seed_money_share']:.1%}).",
                 "Метки: " + ("; ".join(f["flags"]) if f["flags"] else "нет") + ".",
                 "Чего не хватает: " + ("; ".join(f["data_gaps"]) if f["data_gaps"] else "существенных пробелов не выявлено") + ".",
                 "Запрашиваются: выписки по всем счетам клиента за июль 2026, входящие переводы из других банков, операции с наличными, сведения о владельце.",
                 "", "Формулировки — гипотезы для проверки; выводы о виновности не делаются."]
        return "\n".join(lines)


# ------------------------------------------------------------------ модель
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "description": "2–3 предложения простым языком: что делает узел по фактам"},
        "hypotheses": {"type": "array", "items": {"type": "string"}, "description": "осторожные гипотезы с опорой на цифры"},
        "alternative_explanations": {"type": "array", "items": {"type": "string"}},
        "missing_data": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": "string", "enum": ["check_neighbors", "request_data", "mark_legit", "escalate", "none"]},
        "next_step_reason": {"type": "string"},
    },
    "required": ["summary", "hypotheses", "alternative_explanations", "missing_data", "next_step", "next_step_reason"],
}
DECIDE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"choice": {"type": "string"}, "reasons": {"type": "array", "items": {"type": "string"}},
                   "what_would_change_it": {"type": "string"}, "second": {"type": "string"}},
    "required": ["choice", "reasons", "what_would_change_it", "second"],
}


class ModelError(RuntimeError):
    """Живая модель не ответила; нельзя выдавать шаблон за результат вызова."""


class Model:
    def __init__(self, replay=False, record=False):
        self.record = record; self.mode = "replay"; self.client = None; self.model = None
        if not replay and os.environ.get("OPENAI_API_KEY"):
            self.mode, self.model = "live", os.environ.get("OPENAI_MODEL", "gpt-6-luna")
        elif not replay and os.environ.get("NVIDIA_API_KEY"):
            self.mode, self.model = "nvidia", os.environ.get("NVIDIA_MODEL", "google/gemma-4-31b-it")
        self.fixtures = json.load(open(FIX, encoding="utf-8")) if FIX.exists() else {}

    def _client(self):
        if self.client is None:
            from openai import OpenAI
            self.client = OpenAI(timeout=45, max_retries=0) if self.mode == "live" else OpenAI(api_key=os.environ["NVIDIA_API_KEY"], base_url="https://integrate.api.nvidia.com/v1", timeout=45, max_retries=0)
        return self.client

    def ask(self, key, task, payload, schema):
        """key — ключ фикстуры; task — инструкция; payload — факты (dict); schema — JSON-схема ответа."""
        if self.mode == "replay":
            if key in self.fixtures: return self.fixtures[key] | {"_source": "replay"}
            return template_answer(task, payload, schema) | {"_source": "template"}
        msg = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task + "\n\nФакты (JSON):\n" + json.dumps(payload, ensure_ascii=False)}]
        try:
            c = self._client()
            if self.mode == "live":
                r = c.responses.create(model=self.model, input=msg, reasoning={"effort": "none"}, store=False,
                                       text={"format": {"type": "json_schema", "name": "answer", "schema": schema, "strict": True}})
                ans = json.loads(r.output_text)
            else:
                r = c.chat.completions.create(model=self.model, messages=msg + [{"role": "user", "content": "Ответь только JSON по схеме: " + json.dumps(schema, ensure_ascii=False)}],
                                              response_format={"type": "json_object"}, temperature=0.2)
                ans = json.loads(r.choices[0].message.content)
            ans["_source"] = f"{self.mode}:{self.model}"
            if self.record:
                self.fixtures[key] = {k: v for k, v in ans.items() if not k.startswith("_")}
                FIX.parent.mkdir(exist_ok=True); json.dump(self.fixtures, open(FIX, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return ans
        except Exception as ex:
            raise ModelError(f"Ошибка live-модели ({type(ex).__name__}). Ответ replay не подставлен.") from ex


def template_answer(task, payload, schema):
    """Шаблонное объяснение из фактов — без модели. Используется только в replay без фикстуры."""
    if set(schema["properties"]) == {"summary"}:   # сцена 3: текст собирается в scene_whatif
        return {"summary": ""}
    if "choice" in schema["properties"]:
        cands = payload["candidates"]; best = cands[0]
        return {"choice": best["gid"], "second": cands[1]["gid"] if len(cands) > 1 else "",
                "reasons": [f"{best['role_ru']} ({best['rule_id']}): {best['evidence']}",
                            f"след денег курьеров {kzt(best['seed_money_kzt'])} — больше, чем у остальных кандидатов" if best["seed_money_kzt"] >= max(c["seed_money_kzt"] for c in cands) else f"приоритет {best['priority']:.2f} — максимальный среди кандидатов"],
                "what_would_change_it": "если аналитик подтвердит легальный бизнес у этого узла — исключить его и пересчитать (сцена 3)"}
    f = payload
    hyp = [f"{f['role_ru']} по правилу {f['rule_id']}: {f['evidence']}"]
    if f.get("seed_money_kzt"): hyp.append(f"сюда дошло {kzt(f['seed_money_kzt'])} денег известных курьеров ({f['seed_money_share']:.1%} от всех) — путь есть у {f['seed_reach']} seed")
    if f.get("flags_evidence"): hyp.append(f["flags_evidence"])
    alt = {"consolidator": ["сбор оплат за товар или услугу", "касса точки продаж"], "coordinator": ["расчётный счёт бизнеса с большим числом контрагентов"],
           "distributor": ["выплаты зарплаты или вознаграждений"], "transit": ["перевод между своими счетами", "оплата поставщику"],
           "terminal": ["накопление, крупная покупка"], "peripheral": ["разовые бытовые переводы"]}[f["role"]]
    step = "request_data" if f["data_gaps"] else ("check_neighbors" if f["out_deg"] + f["in_deg"] > 0 else "none")
    return {"summary": f"Узел {f['gid']} — {f['role_ru']}, место {f['rank']} по приоритету. Получил {kzt(f['in_kzt'])} от {f['in_deg']} плательщиков, отправил {kzt(f['out_kzt'])} {f['out_deg']} получателям.",
            "hypotheses": hyp, "alternative_explanations": alt, "missing_data": f["data_gaps"],
            "next_step": step, "next_step_reason": "сначала закрыть пробелы в данных" if f["data_gaps"] else "проверить крупнейших контрагентов из списка"}


# ------------------------------------------------------------------ журнал
class DatabaseError(RuntimeError):
    pass


class ApprovalConflict(ValueError):
    pass


def _db():
    url = os.environ.get("DATABASE_URL")
    if not url: return None
    try:
        import psycopg2; return psycopg2.connect(url, connect_timeout=3)
    except Exception as ex:
        raise DatabaseError("PostgreSQL недоступна; операция не подтверждена") from ex


def current_run_id(out=OUT):
    path = out / "nodes_roles.csv"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else None


def _insert_run(cur, kind, payload, answer, out=OUT):
    src = str(answer.get("_source", "replay"))
    mode = "replay" if src.startswith(("replay", "template")) else "live"
    cur.execute("insert into agent_runs (run_id, kind, mode, model, facts, output) values (%s,%s,%s,%s,%s,%s) returning id",
                (current_run_id(out), kind, mode, src.split(":", 1)[1] if mode == "live" and ":" in src else None,
                 json.dumps(payload, ensure_ascii=False), json.dumps(answer, ensure_ascii=False)))
    return cur.fetchone()[0]


def log_run(kind, payload, answer, out=OUT):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "input": payload, "answer": answer}
    con = _db()
    log_id = None
    if con:
        try:
            with con, con.cursor() as cur:
                log_id = _insert_run(cur, kind, payload, answer, out)
        except Exception as ex:
            raise DatabaseError("Не удалось записать прогон в PostgreSQL") from ex
        finally:
            con.close()
    # В Compose PostgreSQL — источник журнала; локальный JSONL нужен автономному CLI.
    if not con:
        out.mkdir(exist_ok=True)
        with open(out / "agent_runs.jsonl", "a", encoding="utf-8") as fh: fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return log_id


# ------------------------------------------------------------------ сцены
def scene_explain(G, M, gid):
    f = G.facts(gid); ans = M.ask(f"explain:{gid}", "Объясни аналитику этот узел и предложи следующий шаг.", f, SCHEMA)
    log_id = log_run("explain", f, ans, G.out); return {"facts": f, "answer": ans, "agent_run_id": log_id}


def scene_decide(G, M, k=3):
    cands = [G.facts(n["id"]) for n in G.top(k)]
    ans = M.ask(f"decide:{k}", f"Из {k} узлов с наибольшим приоритетом выбери, кого проверять первым, и объясни. Учитывай след денег, роль, метки и пробелы в данных.",
                {"candidates": cands}, DECIDE_SCHEMA)
    log_run("decide", {"candidates": cands}, ans, G.out); return {"candidates": [{"gid": c["gid"], "role": c["role"], "priority": c["priority"], "seed_money_kzt": c["seed_money_kzt"]} for c in cands], "answer": ans}


def scene_whatif(G, M, exclude=None, add_seed=None):
    res = G.whatif(exclude=exclude, add_seed=add_seed)
    ans = M.ask(f"whatif:{exclude or ''}:{add_seed or ''}", "Аналитик изменил условия. Объясни в 2–3 предложениях, что изменилось в очереди проверки и почему.",
                {"change": res["change"], "entered": res["entered"], "left": res["left"], "moved": res["moved"][:5]},
                {"type": "object", "additionalProperties": False, "properties": {"summary": {"type": "string"}}, "required": ["summary"]})
    if ans.get("_source", "").startswith("template"):
        ans["summary"] = (f"После изменения ({'исключён ' + str(exclude) if exclude else 'добавлен seed ' + str(add_seed)}) в топ-10 вошли {len(res['entered'])} узлов и вышли {len(res['left'])}; "
                          f"след денег пересчитан с сохранением массы, роли не менялись.")
    log_run("whatif", res["change"], ans, G.out); return res | {"answer": ans}


_approval_lock = Lock()


def scene_request(G, M, gid, confirm=False, decision=None, action_id=None, reviewer="analyst"):
    draft = G.draft_request(gid)
    rid = current_run_id(G.out)
    aid = hashlib.sha256(f"{rid}:{gid}:{draft}".encode()).hexdigest()
    if action_id is not None and action_id != aid:
        raise ApprovalConflict("Черновик изменился: запросите его заново")
    if decision is None and not confirm:
        return {"status": "awaiting_human_approval", "action_id": aid, "draft": draft,
                "note": "Запрос сохраняется локально только после подтверждения человеком; внешней отправки нет."}
    approved = bool(confirm) if decision is None else decision
    if not isinstance(approved, bool): raise ValueError("approved должен быть boolean")
    path = G.out / "requests" / f"request_{gid}.md"
    result = {"status": "saved" if approved else "rejected", "action_id": aid, "draft": draft, "approved": approved}
    if approved: result["path"] = str(path)
    with _approval_lock:
        con = _db()
        try:
            if con:
                with con, con.cursor() as cur:
                    # Одна транзакция и блокировка на действие защищают также параллельные запросы.
                    cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (aid,))
                    cur.execute("select id, approved from approvals where run_id=%s and action=%s order by id limit 1", (rid, aid))
                    existing = cur.fetchone()
                    if existing:
                        if existing[1] != approved: raise ApprovalConflict("Решение по этому действию уже принято")
                        return result | {"approval_id": existing[0], "duplicate": True}
                    cur.execute("insert into approvals (run_id,gid,action,approved,reviewer) values (%s,%s,%s,%s,%s) returning id",
                                (rid, gid, aid, approved, reviewer))
                    approval_id = cur.fetchone()[0]
                    log_id = _insert_run(cur, "request_approved" if approved else "request_rejected",
                                         {"gid": gid, "action_id": aid, "approved": approved, "draft": draft}, result, G.out)
                    if approved:
                        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(draft, encoding="utf-8")
                    result.update(approval_id=approval_id, agent_run_id=log_id, duplicate=False)
            else:
                receipt = G.out / "approvals" / f"{aid}.json"
                if receipt.exists():
                    old = json.loads(receipt.read_text(encoding="utf-8"))
                    if old["approved"] != approved: raise ApprovalConflict("Решение по этому действию уже принято")
                    return old | {"duplicate": True}
                if approved:
                    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(draft, encoding="utf-8")
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except (ApprovalConflict, OSError):
            raise
        except Exception as ex:
            raise DatabaseError("Не удалось сохранить решение в PostgreSQL") from ex
        finally:
            if con: con.close()
    return result


def pretty(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


# ------------------------------------------------------------------ HTTP для карточки узла
def serve(G, M, port):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/explain": body = scene_explain(G, M, q["gid"])
                elif u.path == "/api/decide": body = scene_decide(G, M, int(q.get("k", 3)))
                elif u.path == "/api/whatif": body = scene_whatif(G, M, exclude=q.get("exclude"), add_seed=q.get("add_seed"))
                elif u.path == "/api/request": body = scene_request(G, M, q["gid"], confirm=q.get("confirm") == "1")
                elif u.path == "/api/health": body = {"ok": True, "mode": M.mode, "model": M.model, "nodes": len(G.nodes)}
                else: self.send_response(404); self.end_headers(); return
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(data)
            except Exception as ex:
                self.send_response(400); self.send_header("Content-Type", "application/json"); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
                self.wfile.write(json.dumps({"error": str(ex)}, ensure_ascii=False).encode())
        def log_message(self, *a): pass
    print(f"ассистент: http://127.0.0.1:{port}/api/health — режим {M.mode}"); HTTPServer(("127.0.0.1", port), H).serve_forever()


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("cmd", choices=["explain", "decide", "whatif", "request", "demo", "serve"]); a.add_argument("gid", nargs="?")
    a.add_argument("--k", type=int, default=3); a.add_argument("--exclude"); a.add_argument("--add-seed"); a.add_argument("--confirm", action="store_true")
    a.add_argument("--replay", action="store_true"); a.add_argument("--record", action="store_true"); a.add_argument("--port", type=int, default=8765)
    n = a.parse_args(); G = Graph(); M = Model(replay=n.replay, record=n.record)
    print(f"[режим модели: {M.mode}{' ' + M.model if M.model else ''}]", file=sys.stderr)
    if n.cmd == "explain": pretty(scene_explain(G, M, n.gid))
    elif n.cmd == "decide": pretty(scene_decide(G, M, n.k))
    elif n.cmd == "whatif": pretty(scene_whatif(G, M, exclude=n.exclude, add_seed=n.add_seed))
    elif n.cmd == "request": pretty(scene_request(G, M, n.gid, confirm=n.confirm))
    elif n.cmd == "serve": serve(G, M, n.port)
    else:
        top = G.top(3); g1 = top[0]["id"]
        print("\n=== СЦЕНА 1. Факты и объяснение: узел №1 по приоритету ==="); pretty(scene_explain(G, M, g1))
        print("\n=== СЦЕНА 2. Решение: кого из топ-3 проверять первым ==="); pretty(scene_decide(G, M, 3))
        print(f"\n=== СЦЕНА 3. Аналитик исключил {g1} как легальный бизнес → пересчёт ==="); pretty(scene_whatif(G, M, exclude=g1))
        print(f"\n=== СЦЕНА 4. Guardrail: запрос по {top[1]['id']} без подтверждения ==="); pretty(scene_request(G, M, top[1]["id"], confirm=False))


if __name__ == "__main__":
    main()
