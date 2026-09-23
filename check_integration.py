"""Проверка реального replay API и PostgreSQL: docker compose exec app python check_integration.py.

Создаёт объяснения и два тестовых решения; повторный запуск не дублирует решения.
Запускать только в локальном демонстрационном окружении без OpenAI-ключа.
"""
import concurrent.futures
import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace

import psycopg2
import assistant

BASE = os.environ.get("TEST_BASE_URL", "http://localhost:8000")


def api(path, body=None, expected=200):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as ex:
        response = ex
    with response:
        result = json.load(response)
        assert response.status == expected, (path, response.status, result)
        return result


def main():
    health = api("/api/health")
    assert health["ok"] and health["db"]["ok"] and health["mode"] == "replay"
    assert health["nodes"] == 2248
    # Отдельный тест обработки отказа SDK; не является живым вызовом модели.
    def fail_provider(**kwargs):
        assert kwargs["store"] is False
        raise TimeoutError("simulated provider outage")
    model = assistant.Model(replay=True)
    model.mode = "live"; model.model = "test-only"
    model.client = SimpleNamespace(responses=SimpleNamespace(create=fail_provider))
    try:
        model.ask("test", "test", {}, assistant.SCHEMA)
    except assistant.ModelError:
        pass
    else:
        raise AssertionError("Provider failure was hidden by replay")
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        for gid, reach in [("100000002398779100", 8), ("100000003684369100", 9)]:
            body = api("/api/agent", {"gid": gid})
            assert body["mode"] == "replay" and body["facts"]["seed_reach"] == reach
            assert body["facts"]["seed_reach_direction"] == "seed → … → выбранный клиент"
            with con.cursor() as cur:
                cur.execute("select run_id, mode, facts, output from agent_runs where id=%s", (body["agent_run_id"],))
                rid, mode, facts, output = cur.fetchone()
                assert rid == assistant.current_run_id() and mode == "replay"
                assert facts == body["facts"] and output == body["answer"]
        for gid, decision in [("100000002398779100", True), ("100000003684369100", False)]:
            draft = api("/api/agent", {"gid": gid, "action": "request"})
            payload = {"gid": gid, "action_id": draft["action_id"], "approved": decision}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: api("/api/approve", payload), range(4)))
            assert len({r["approval_id"] for r in results}) == 1
            assert sum(not r["duplicate"] for r in results) <= 1
            assert all(r["status"] == ("saved" if decision else "rejected") for r in results)
            api("/api/approve", payload | {"approved": not decision}, 409)
            api("/api/approve", payload | {"action_id": "stale"}, 409)
            with con.cursor() as cur:
                cur.execute("select count(*), bool_and(approved=%s) from approvals where action=%s", (decision, draft["action_id"]))
                assert cur.fetchone() == (1, True)
                cur.execute("select count(*) from agent_runs where facts->>'action_id'=%s", (draft["action_id"],))
                assert cur.fetchone()[0] == 1
        legacy = api("/api/request?gid=100000002398779100&confirm=1")
        assert legacy["duplicate"] is True
        api("/api/agent", {"gid": 123}, 400)
        api("/api/agent", {"gid": "missing"}, 404)
        api("/api/missing", expected=404)
        # Загруженные проекты: факты и журнал относятся к выбранному результату,
        # даже когда последний прогон в БД отличается от основного.
        from pathlib import Path
        checked_projects = 0
        for project in api('/api/projects')['projects']:
            if project['id'] == 'main' or project['status'] != 'ready': continue
            pid = project['id']
            graph = api('/' + project['graph_url'])
            out = Path('projects') / pid / 'out'
            expected_run = assistant.current_run_id(out)
            for index, approved in [(0, True), (1, False)]:
                node = graph['nodes'][index]; gid = node['id']
                explained = api('/api/agent', {'project': pid, 'gid': gid})
                assert explained['facts']['gid'] == gid
                assert explained['facts']['in_kzt'] == node['in_kzt']
                with con.cursor() as cur:
                    cur.execute('select run_id from agent_runs where id=%s', (explained['agent_run_id'],))
                    assert cur.fetchone()[0] == expected_run
                draft = api('/api/agent', {'project': pid, 'gid': gid, 'action':'request'})
                result = api('/api/approve', {'project':pid,'gid':gid,'action_id':draft['action_id'],'approved':approved})
                with con.cursor() as cur:
                    cur.execute('select run_id,approved from approvals where id=%s',(result['approval_id'],))
                    assert cur.fetchone() == (expected_run,approved)
            checked_projects += 1
        print('Uploaded project isolation checks:', checked_projects)
    finally:
        con.close()
    print("OK: health, both direction facts, replay journals, approve/reject, 4 concurrent retries, conflicts, legacy API, invalid input")
    print(json.dumps(api("/api/health"), ensure_ascii=False))


if __name__ == "__main__":
    main()
