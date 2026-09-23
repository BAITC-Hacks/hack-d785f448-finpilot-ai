#!/usr/bin/env python3
"""Один процесс для демо: считает результат, кладёт его в базу (если есть) и отдаёт карту с API ассистента.

    python serve.py                 # http://localhost:8000 — карта, /api/* — ассистент, /api/db/health — база
Шаги: pipeline.py → addons_v3.py → check.py (пропускаются, если есть out/graph.json и задан SKIP_PIPELINE=1)
→ db_load.py (только при DATABASE_URL) → HTTP-сервер: статика из web/ и /api/* из assistant.py.
Переменные: PORT (8000), OUT (out), DATA (data), DATABASE_URL, OPENAI_API_KEY, OPENAI_MODEL — см. .env.example.
"""
import json, os, subprocess, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
OUT = Path(os.environ.get("OUT", "out")); DATA = os.environ.get("DATA", "data"); PORT = int(os.environ.get("PORT", "8000"))
os.environ.setdefault("GRAPH_OUT", str(OUT))


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
        subprocess.run([sys.executable, str(ROOT / "db_load.py"), "--out", str(OUT)])
    web = ROOT / "web"
    if not (web / "graph.json").exists() or (OUT / "graph.json").stat().st_mtime > (web / "graph.json").stat().st_mtime:
        (web / "graph.json").write_bytes((OUT / "graph.json").read_bytes()); print("serve: web/graph.json обновлён из out/")
    import assistant
    G = assistant.Graph(OUT); M = assistant.Model()
    print(f"serve: ассистент в режиме {M.mode}{' ' + M.model if M.model else ''}")

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(web), **k)

        def do_GET(self):
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                return super().do_GET()
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/health": body = {"ok": True, "mode": M.mode, "model": M.model, "nodes": len(G.nodes), "db": db_health()}
                elif u.path == "/api/db/health": body = db_health()
                elif u.path == "/api/explain": body = assistant.scene_explain(G, M, q["gid"])
                elif u.path == "/api/decide": body = assistant.scene_decide(G, M, int(q.get("k", 3)))
                elif u.path == "/api/whatif": body = assistant.scene_whatif(G, M, exclude=q.get("exclude"), add_seed=q.get("add_seed"))
                elif u.path == "/api/request": body = assistant.scene_request(G, M, q["gid"], confirm=q.get("confirm") == "1")
                else: self.send_error(404); return
                self._json(200, body)
            except Exception as ex:
                self._json(400, {"error": f"{type(ex).__name__}: {ex}"})

        def _json(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def log_message(self, fmt, *args):
            if "/api/" in (args[0] if args else ""): sys.stderr.write("%s\n" % (fmt % args))

    print(f"serve: http://localhost:{PORT}  (карта)   http://localhost:{PORT}/api/health  (ассистент и база)")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
