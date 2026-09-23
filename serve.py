#!/usr/bin/env python3
"""Один процесс для демо: считает результат, кладёт его в базу (если есть) и отдаёт карту с API ассистента.

    python serve.py                 # http://localhost:8000 — карта, /api/* — ассистент, /api/db/health — база
Шаги: pipeline.py → addons_v3.py → check.py (пропускаются, если есть out/graph.json и задан SKIP_PIPELINE=1)
→ db_load.py (только при DATABASE_URL) → HTTP-сервер: статика из web/ и /api/* из assistant.py.
Переменные: PORT (8000), OUT (out), DATA (data), DATABASE_URL, OPENAI_API_KEY, OPENAI_MODEL — см. .env.example.
"""
import email, email.policy, json, os, re, shutil, subprocess, sys, time, uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
OUT = Path(os.environ.get("OUT", "out")); DATA = os.environ.get("DATA", "data"); PORT = int(os.environ.get("PORT", "8000"))
PROJECTS = Path(os.environ.get("PROJECTS", "projects")); MAIN_NAME = os.environ.get("PROJECT_NAME", "Freedom, июль 2026")
MAX_UPLOAD = 200 * 1024 * 1024
os.environ.setdefault("GRAPH_OUT", str(OUT))


# ------------------------------------------------------------------ проекты: своя выгрузка → расчёт → graph.json
def project_meta(pdir):
    try:
        return json.load(open(pdir / "meta.json", encoding="utf-8"))
    except Exception:
        return None


def list_projects():
    """Основной проект — результат из out/ (данные из data/); остальные — загруженные через интерфейс."""
    items = []
    g = OUT / "graph.json"
    if g.exists():
        meta = json.load(open(OUT / "run_meta.json", encoding="utf-8")) if (OUT / "run_meta.json").exists() else {}
        items.append({"id": "main", "name": MAIN_NAME, "status": "ready", "nodes": meta.get("nodes"), "edges": meta.get("edges"),
                      "graph_url": "graph.json", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(g.stat().st_mtime))})
    if PROJECTS.exists():
        for pdir in sorted(PROJECTS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            m = project_meta(pdir)
            if m: items.append(m)
    return items


def parse_multipart(handler):
    """Разбор multipart/form-data стандартной библиотекой: поля → str, файлы → (имя, bytes)."""
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0 or length > MAX_UPLOAD:
        raise ValueError(f"размер запроса {length} байт вне допустимого (до {MAX_UPLOAD // 1024 // 1024} МБ)")
    body = handler.rfile.read(length)
    msg = email.message_from_bytes(b"Content-Type: " + handler.headers.get("Content-Type", "").encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
                                   policy=email.policy.HTTP)
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name: continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename: files[name] = (filename, payload)
        else: fields[name] = payload.decode("utf-8", "replace").strip()
    return fields, files


def create_project(fields, files):
    name = (fields.get("name") or "").strip()[:60] or f"Проект {time.strftime('%d.%m %H:%M')}"
    missing = [k for k in ("nodes", "edges", "transactions") if k not in files or not files[k][1]]
    if missing:
        raise ValueError("не хватает файлов: " + ", ".join(f"{k}.parquet" for k in missing))
    pid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    pdir = PROJECTS / pid; (pdir / "data").mkdir(parents=True); (pdir / "out").mkdir()
    for k in ("nodes", "edges", "transactions"):
        (pdir / "data" / f"{k}.parquet").write_bytes(files[k][1])
    meta = {"id": pid, "name": name, "status": "running", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "graph_url": f"projects/{pid}/graph.json"}
    json.dump(meta, open(pdir / "meta.json", "w", encoding="utf-8"), ensure_ascii=False)
    log = []
    for script in ("pipeline.py", "addons_v3.py", "check.py"):
        r = subprocess.run([sys.executable, str(ROOT / script), "--data", str(pdir / "data"), "--out", str(pdir / "out")], capture_output=True, text=True)
        log.append(f"$ {script}\n{r.stdout[-3000:]}{r.stderr[-3000:]}")
        if r.returncode:
            meta.update(status="error", error=f"{script}: код {r.returncode}", log="\n".join(log)[-6000:])
            json.dump(meta, open(pdir / "meta.json", "w", encoding="utf-8"), ensure_ascii=False); return meta
        if script == "check.py": meta["check_ok"] = r.returncode == 0
    shutil.copy(pdir / "out" / "graph.json", pdir / "graph.json")
    rm = json.load(open(pdir / "out" / "run_meta.json", encoding="utf-8"))
    if os.environ.get("DATABASE_URL"):
        loaded = subprocess.run([sys.executable, str(ROOT / "db_load.py"), "--out", str(pdir / "out")], capture_output=True)
        if loaded.returncode:
            meta.update(status="error", error="Не удалось сохранить результат в PostgreSQL")
            json.dump(meta, open(pdir / "meta.json", "w", encoding="utf-8"), ensure_ascii=False)
            raise RuntimeError(meta["error"])
    meta.update(status="ready", nodes=rm.get("nodes"), edges=rm.get("edges"), log="\n".join(log)[-6000:])
    json.dump(meta, open(pdir / "meta.json", "w", encoding="utf-8"), ensure_ascii=False)
    return meta


def run_pipeline():
    if os.environ.get("SKIP_PIPELINE") == "1" and (OUT / "graph.json").exists():
        print("serve: SKIP_PIPELINE=1 — беру готовый out/graph.json"); return
    for script in ("pipeline.py", "addons_v3.py", "check.py"):
        code = subprocess.run([sys.executable, str(ROOT / script), "--data", DATA, "--out", str(OUT)]).returncode
        if code:
            print(f"serve: {script} завершился с кодом {code} — сервер не запускаю"); sys.exit(code)


def db_health():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return {"ok": False, "configured": False, "note": "DATABASE_URL не задан — работаем без базы"}
    try:
        import psycopg2
        con = psycopg2.connect(url, connect_timeout=3); cur = con.cursor()
        cur.execute("select run_id, created_at, nodes, capture_30 from runs order by created_at desc limit 1"); last = cur.fetchone()
        counts = {}
        for t in ("runs", "nodes_roles", "node_flags", "block_plan", "agent_runs", "approvals"):
            cur.execute(f"select count(*) from {t}"); counts[t] = cur.fetchone()[0]
        con.close()
        return {"ok": True, "configured": True, "counts": counts,
                "last_run": None if not last else {"run_id": last[0], "created_at": last[1].isoformat(), "nodes": last[2], "capture_30": float(last[3]) if last[3] is not None else None}}
    except Exception as ex:
        return {"ok": False, "configured": True, "error": f"{type(ex).__name__}: {ex}"}


def main():
    run_pipeline()
    if os.environ.get("DATABASE_URL"):
        subprocess.run([sys.executable, str(ROOT / "db_load.py"), "--out", str(OUT)], check=True)
    web = ROOT / "web"
    if not (web / "graph.json").exists() or (OUT / "graph.json").stat().st_mtime > (web / "graph.json").stat().st_mtime:
        (web / "graph.json").write_bytes((OUT / "graph.json").read_bytes()); print("serve: web/graph.json обновлён из out/")
    import assistant
    G = assistant.Graph(OUT); M = assistant.Model(); graphs = {"main": G}

    def graph_for(q):
        pid = q.get("project", "main")
        if not isinstance(pid, str) or not re.fullmatch(r"[\w-]+", pid): raise ValueError("Неверный project")
        if pid not in graphs:
            pdir = PROJECTS / pid
            if (project_meta(pdir) or {}).get("status") != "ready" or not (pdir / "out" / "graph.json").exists(): raise KeyError(f"проект {pid} не готов или не найден")
            graphs[pid] = assistant.Graph(pdir / "out")
        return graphs[pid]
    print(f"serve: ассистент в режиме {M.mode}{' ' + M.model if M.model else ''}")

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(web), **k)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path.startswith("/projects/"):
                m = re.fullmatch(r"/projects/([\w-]+)/graph\.json", u.path); f = PROJECTS / m.group(1) / "graph.json" if m else None
                if not f or not f.exists(): self.send_error(404); return
                data = f.read_bytes(); self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if not u.path.startswith("/api/"):
                return super().do_GET()
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/projects": body = {"projects": list_projects()}
                elif u.path == "/api/health":
                    database = db_health()
                    ready = database["ok"] if database.get("configured") else True
                    self._json(200 if ready else 503, {"ok": ready, "mode": M.mode, "model": M.model, "nodes": len(G.nodes), "db": database})
                    return
                elif u.path == "/api/db/health":
                    body = db_health(); self._json(200 if body["ok"] else 503, body); return
                elif u.path == "/api/explain": body = assistant.scene_explain(graph_for(q), M, q["gid"])
                elif u.path == "/api/decide": body = assistant.scene_decide(graph_for(q), M, int(q.get("k", 3)))
                elif u.path == "/api/whatif": body = assistant.scene_whatif(graph_for(q), M, exclude=q.get("exclude"), add_seed=q.get("add_seed"))
                elif u.path == "/api/request": body = assistant.scene_request(graph_for(q), M, q["gid"], confirm=q.get("confirm") == "1")
                else: self._json(404, {"error": "Маршрут не найден"}); return
                self._result(body)
            except Exception as ex:
                self._error(ex)

        def do_POST(self):
            if urlparse(self.path).path == "/api/projects":   # загрузка выгрузки: multipart, расчёт на сервере
                try:
                    fields, files = parse_multipart(self)
                    meta = create_project(fields, files)
                    self._json(200 if meta["status"] == "ready" else 422, meta)
                except Exception as ex:
                    self._error(ex)
                return
            # Браузер работает на том же origin. Не принимаем подтверждения с чужих страниц.
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self._json(403, {"error": "Чужой Origin"}); return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384: raise ValueError("Неверный размер JSON")
                q = json.loads(self.rfile.read(size))
                if not isinstance(q, dict): raise ValueError("Ожидается JSON object")
                gid = q.get("gid")
                if not isinstance(gid, str): raise ValueError("gid передаётся строкой")
                selected_graph = graph_for(q)
                selected_graph.node(gid)
                path = urlparse(self.path).path
                if path == "/api/agent":
                    action = q.get("action", "explain")
                    if action == "explain": body = assistant.scene_explain(selected_graph, M, gid)
                    elif action == "request": body = assistant.scene_request(selected_graph, M, gid)
                    else: raise ValueError("action: explain или request")
                elif path == "/api/approve":
                    if not isinstance(q.get("approved"), bool): raise ValueError("approved должен быть boolean")
                    if not isinstance(q.get("action_id"), str): raise ValueError("Нужен action_id черновика")
                    body = assistant.scene_request(selected_graph, M, gid, decision=q["approved"], action_id=q["action_id"])
                else: self._json(404, {"error": "Маршрут не найден"}); return
                self._result(body)
            except Exception as ex:
                self._error(ex)

        def _result(self, body):
            src = str(body.get("answer", {}).get("_source", "replay"))
            mode = "live" if src.startswith(("live:", "nvidia:")) else "replay"
            self._json(200, body | {"mode": mode, "model": M.model if mode == "live" else None})

        def _error(self, ex):
            if isinstance(ex, assistant.DatabaseError): code = 503
            elif isinstance(ex, assistant.ModelError): code = 502
            elif isinstance(ex, assistant.ApprovalConflict): code = 409
            elif isinstance(ex, KeyError): code = 404
            elif isinstance(ex, (ValueError, TypeError)): code = 400
            else: code = 500
            self._json(code, {"error": str(ex) if code != 500 else "Внутренняя ошибка сервера"})

        def _json(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def log_message(self, fmt, *args):
            if "/api/" in (args[0] if args else ""): sys.stderr.write("%s\n" % (fmt % args))

    PROJECTS.mkdir(exist_ok=True)
    print(f"serve: http://localhost:{PORT}  (карта)   http://localhost:{PORT}/api/health  (ассистент и база)   проектов: {len(list_projects())}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
