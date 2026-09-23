# Запуск на локальной машине: Docker Compose + PostgreSQL

Весь проект поднимается одной командой на любом компьютере с Docker. Облачные сервисы не используются.
Раздел закрывает требование организаторов: приложение и база в Docker, инструкция запуска, карта переменных без значений.

## Что нужно на компьютере

- Docker Desktop (Windows/macOS) или Docker Engine + Compose v2 (Linux): https://docs.docker.com/get-docker/
- работающий Docker Engine (`docker info` должен завершаться успешно), свободный порт 8000;
- не менее 5 ГБ свободного места для первой сборки и доступ к Docker Hub/PyPI. Время скачивания и сборки зависит от сети и компьютера.

## Запуск

```bash
git clone https://github.com/BAITC-Hacks/hack-d785f448-finpilot-ai.git
cd hack-d785f448-finpilot-ai
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
docker compose up --build
```

Дождаться сообщения `serve: http://localhost:8000` и готовности `/api/health`. Открыть http://localhost:8000. Порт приложения опубликован только на `127.0.0.1`, PostgreSQL наружу не публикуется.

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

Остановить с сохранением базы: `docker compose down`. Повторный `docker compose up --build` использует тот же постоянный том. Команда `docker compose down -v` удаляет базу; для обычной остановки и обновления её не используют.
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

Права: единственный пользователь базы — `POSTGRES_USER` из `.env`, доступен только из compose-сети. RPC, функций, auth и storage нет. `db/init/*.sql` применяются только при первом старте пустого тома. Обновления схемы существующей базы следует применять отдельно через `psql`, после резервного копирования; пересоздание тома для обновления не требуется.

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

## Если сборка упала

`Read-only file system` в Docker вместе с ошибкой записи `containerd ... meta.db` — сбой хранилища Docker, а не успешный запуск приложения. Проверьте свободное место на диске хоста, освободите место и перезапустите Docker Desktop. Не удаляйте постоянный том базы ради повторной сборки. Фактический статус проверки текущей версии — в [PROGRESS.md](PROGRESS.md).

## Интеграционная проверка

После готовности /api/health:

```sh
docker compose exec app python check.py --data data --out out --prev out_sample
docker compose exec app python check_integration.py
docker compose exec app python db_load.py --out out
```

check.py проверяет 28 условий, включая воспроизводимость. check_integration.py вызывает реальный HTTP API и читает PostgreSQL: оба случая направления, журнал с полными фактами и run_id, подтверждение и отказ, четыре одновременных повтора, конфликт решения и неверный ввод. Выполнять без OpenAI-ключа; тест создаёт два решения. Повтор не создаёт дубликатов решений, новые объяснения журналируются.

Для проверки сохранности сравните /api/health до и после `docker compose down` и `docker compose up --build`. Том db_data не удалять. В Compose журнал хранится в PostgreSQL; JSONL используется только при автономном запуске без DATABASE_URL. Ошибки подключения/записи журнала дают HTTP 503. Подтверждение и журнал пишутся одной транзакцией. Схема содержит шесть таблиц; approvals.action хранит стабильный идентификатор действия. Обе кнопки сайта сохраняют решение через POST /api/approve. Полный контракт — [API_CONTRACT.md](API_CONTRACT.md).

## Свои parquet и переключение проектов

Нажмите «Новый проект», задайте название и выберите nodes.parquet, edges.parquet, transactions.parquet с согласованными схемами. Сервер рассчитывает результат и проверяет его; после успеха проект появляется в боковой панели. Загруженные файлы, результаты и подтверждённые запросы остаются в папке projects/ на хосте; стандартный результат — в out/. Эти папки и постоянный том db_data сохраняются после down/up. Повторная загрузка одинакового результата создаёт отдельный проект на диске, но не дублирует расчётные строки PostgreSQL.

Граф, поиск, карточка, объяснение и решение используют выбранный проект. Неудачная проверка данных даёт status=error, а не готовый проект. Дополнительные проверки check_integration.py автоматически охватывают готовые загруженные проекты и проверяют привязку фактов и решений к их run_id.

## Replay и live

Для replay оставить OPENAI_API_KEY пустым; аккаунт OpenAI не нужен. Для live ключ получают в своём проекте OpenAI Platform, сохраняют только в .env; OPENAI_MODEL задаёт точный идентификатор доступной аккаунту модели. Пересоздать app: `docker compose up -d --force-recreate app`. Ключ и модель читаются при запуске процесса. Не отправлять ключ в браузер и не коммитить .env.

Два обязательных live-теста: объяснения GID 100000002398779100 (seed_reach=8) и 100000003684369100 (seed_reach=9) через /api/agent или /api/explain. Проверить _source=live:<model> и текст: направление от seed к выбранному узлу, а не наоборот. Обратная достижимость seed для этих узлов равна 0 и 1 соответственно. При ошибке модели API возвращает HTTP 502; replay не подставляется. Такой ответ live-тест не проходит.

В Responses API задан store=false для отключения хранения объекта ответа — [документация OpenAI](https://developers.openai.com/api/docs/guides/migrate-to-responses). Это не отменяет собственный журнал PostgreSQL.

Источники остальных настроек: POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB задаёт запускающий приложение (пример рассчитан на локальное демо); APP_PORT выбирается локально; DATABASE_URL собирает Compose. Для прямого Python-запуска доступны DATA (папка parquet, data), OUT (результаты, out), PORT (8000), SKIP_PIPELINE=1 (использовать существующий graph.json), GRAPH_OUT (папка ассистента; serve.py задаёт из OUT). Compose эти дополнительные параметры не передаёт: его стандартный запуск каждый раз пересчитывает данные.
