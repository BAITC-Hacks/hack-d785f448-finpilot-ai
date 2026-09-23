# Запуск на локальной машине: Docker Compose + PostgreSQL

Весь проект поднимается одной командой на любом компьютере с Docker. Облачные сервисы не используются.
Раздел закрывает требование организаторов: приложение и база в Docker, инструкция запуска, карта переменных без значений.

## Что нужно на компьютере

- Docker Desktop (Windows/macOS) или Docker Engine + Compose v2 (Linux): https://docs.docker.com/get-docker/
- около 2 ГБ места под образы и 1 минута на первую сборку.

## Запуск

```bash
git clone <ссылка на репозиторий> && cd <папка>
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
docker compose up --build
```

Через 1–2 минуты в логе появится `serve: http://localhost:8000`. Открыть http://localhost:8000.

Что происходит внутри при старте:

1. Контейнер `db` (PostgreSQL 16) создаёт базу и применяет схему `db/init/001_schema.sql` — один раз, при пустом томе.
2. Контейнер `app` ждёт готовности базы, запускает `pipeline.py → addons_v3.py → check.py` (около 10 секунд, 28 проверок), кладёт результат в базу (`db_load.py`) и поднимает HTTP-сервер (`serve.py`): карта из `web/` и API ассистента `/api/*`.

## Проверка, что всё работает

```bash
curl http://localhost:8000/api/health
# {"ok": true, "mode": "replay", "nodes": 2248, "db": {"ok": true, "counts": {"runs": 1, "nodes_roles": 2248, ...}}}

docker compose exec db psql -U graph -d graph -c "select role, count(*) from nodes_roles group by role order by 2 desc"
docker compose exec db psql -U graph -d graph -c "select step, gid, role, cumulative from block_plan order by step limit 5"
```

В интерфейсе: очередь топ-30 → клик по узлу → карточка → «Объяснить» (запись в `agent_runs`) → «Сформировать запрос» → «Подтвердить» (запись в `approvals`).

Своя выгрузка: «Новый проект» → название и три файла `nodes.parquet`, `edges.parquet`, `transactions.parquet` (схема как у данных кейса) → «Рассчитать». Сервер кладёт файлы в `projects/<id>/data/`, прогоняет пайплайн и проверку (около 10 секунд), результат появляется в списке проектов слева и, при заданном `DATABASE_URL`, в базе как отдельный `run_id`. Каталог `projects/` смонтирован на хост и не попадает в Git.

Остановить: `docker compose down`. Сбросить базу и применить схему заново: `docker compose down -v`.
Результаты прогона лежат на хосте в `out/` (том смонтирован).

## Архитектура

```
браузер ──HTTP 8000──▶ app (python: serve.py)
                         ├─ web/            статика: карта, очередь, карточка узла (читает graph.json)
                         ├─ /api/*          ассистент: explain / decide / whatif / request (assistant.py); /api/projects — список и загрузка выгрузок
                         ├─ pipeline.py, addons_v3.py, check.py   расчёт из data/*.parquet → out/
                         └─ db_load.py ──SQL──▶ db (PostgreSQL 16, только внутри compose-сети, порт наружу не открыт)
```

Источник данных для фронта — `web/graph.json`, а не база: интерфейс и пайплайн работают и без базы (`DATABASE_URL` не задан → `db_load.py` и журнал пропускаются). База — хранилище результатов прогонов и журнала ассистента для проверки и аудита.

## База данных

Таблицы (`db/init/001_schema.sql`):

| Таблица | Что хранит | Кто пишет | Кто читает |
|---|---|---|---|
| `runs` | прогон: run_id = sha256(nodes_roles.csv), число узлов и рёбер, сходимость следа денег, охват плана | `db_load.py` | `/api/health`, psql |
| `nodes_roles` | роль, правило, скор, кластер, приоритет, evidence и метрики каждого из 2 248 узлов | `db_load.py` | psql, отчёты |
| `node_flags` | шесть меток и их обоснование по узлу | `db_load.py` | psql |
| `block_plan` | 30 шагов плана охвата | `db_load.py` | psql |
| `agent_runs` | журнал ассистента: вид запроса, режим (live/replay), модель, переданные факты, ответ | `assistant.py` | psql, аудит |
| `approvals` | подтверждения человеком действий с последствиями (сцена «сформировать запрос») | `assistant.py` | psql, аудит |

Потоки данных: приложение → база: результаты прогона (один раз на уникальный результат — повторная загрузка того же файла не дублирует), записи журнала и подтверждений. База → приложение: только сводка в `/api/health`. Браузер к базе не обращается никогда.

Права: единственный пользователь базы — `POSTGRES_USER` из `.env`, доступен только из compose-сети. RPC, функций, auth и storage нет. Миграции — файлы `db/init/*.sql`, применяются при первом старте; новая миграция = новый файл с большим номером и `docker compose down -v` для чистого применения.

## Переменные окружения — карта без значений

Реальные значения живут только в `.env` (в `.gitignore`). В Git — `.env.example` с пустыми или заведомо локальными значениями.

| Переменная | Назначение | Кто использует | Где хранится |
|---|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | учётные данные локальной базы | контейнер `db`; из них собирается `DATABASE_URL` для `app` | `.env` |
| `DATABASE_URL` | строка подключения приложения к базе | `serve.py`, `db_load.py`, `assistant.py`; пусто → без базы | собирается в `compose.yaml`; вручную — только для запуска без Docker |
| `APP_PORT` | порт карты на хосте (по умолчанию 8000) | compose | `.env` |
| `OPENAI_API_KEY` | ключ живой модели ассистента; пусто → режим replay без сети | `assistant.py` внутри `app` | `.env`; никогда в Git, README и логах |
| `OPENAI_MODEL` | имя модели (по умолчанию `gpt-6-luna`) | `assistant.py` | `.env` |

Правило: если переменная позволяет что-то изменить или стоит денег — её значение в Git не попадает. Перед коммитом: `git status`, `git diff --check`, `git diff --cached | grep -i "key\|password\|token"` должен быть пуст.

## Запуск без Docker (проверка воспроизводимости из README)

```bash
pip install -r requirements.txt
python run.py                 # pipeline → addons → check, результат в out/
python serve.py               # карта и ассистент на http://localhost:8000, без базы
```

С локальным PostgreSQL: применить `db/init/001_schema.sql`, задать `DATABASE_URL` и запустить `python serve.py` — загрузка и журнал включатся автоматически.
