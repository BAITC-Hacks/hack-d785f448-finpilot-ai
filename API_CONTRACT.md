# API для фронта и ассистента

Базовый адрес: http://localhost:8000. Все вызовы браузера — на тот же origin. GID передаётся строкой. Сервер использует существующий assistant.py и результаты опубликованного пайплайна.

## Объяснение и черновик

POST `/api/agent`, Content-Type: application/json:

```json
{"project":"main","gid":"100000002398779100","action":"explain"}
```

HTTP 200: `{facts, answer, agent_run_id, mode, model}`. `facts` содержит роль, правило, метрики, вклады в приоритет, пробелы, соседей и путь. `answer`: `summary`, `hypotheses`, `alternative_explanations`, `missing_data`, `next_step`, `next_step_reason`, `_source`. `agent_run_id` — фактическая запись PostgreSQL. По умолчанию action=explain.

Для черновика тот же путь:

```json
{"project":"main","gid":"100000002398779100","action":"request"}
```

HTTP 200: `{status:"awaiting_human_approval", action_id:"<sha256>", draft:"Текст", note:"Требуется подтверждение", mode:"replay", model:null}`.

## Решение человека

POST `/api/approve`:

```json
{"project":"main","gid":"100000002398779100","action_id":"<из черновика>","approved":true}
```

HTTP 200: `{status:"saved", action_id, approved:true, approval_id, agent_run_id, duplicate:false, path, draft, mode:"replay", model:null}`. Файл запроса сохраняется локально; внешней отправки нет. Решение и журнал сохраняются одной транзакцией PostgreSQL. При `approved:false`: `status:"rejected"`, файл не создаётся, отказ записывается в БД. Операция детерминирована даже при live-объяснениях.

Повтор того же решения возвращает прежний approval_id и duplicate=true; действие не выполняется вновь. Повторный ответ может не содержать agent_run_id. Противоположное решение или устаревший action_id — HTTP 409. action_id стабилен для одного результата расчёта, GID и текста черновика. Авторизации в локальном демо нет: reviewer=analyst не удостоверяет личность пользователя.

## Совместимость с текущим web/

| Метод и путь | Назначение |
|---|---|
| GET /api/health | Готовность приложения и БД; mode, model, nodes, db и счётчики шести таблиц |
| GET /api/db/health | Отдельное состояние базы |
| GET /api/explain?gid=… | То же объяснение, что POST /api/agent |
| GET /api/decide?k=3 | candidates, answer, mode, model |
| GET /api/whatif?exclude=… | Сценарий прекращения передачи модельного потока через узел; роли и кластеры не пересчитываются |
| GET /api/request?gid=… | Черновик с action_id |
| GET /api/request?gid=…&confirm=1 | Старое подтверждение, теперь защищено от дубликатов |

Кнопки «Подтвердить» и «Отклонить» отправляют POST /api/approve с approved=true/false. Передаются project, gid и action_id показанного черновика. При смене проекта или узла черновик сбрасывается; запоздавший ответ не заменяет текущую карточку. Старые GET сохранены для совместимости.

## Загруженные проекты

GET `/api/projects` возвращает `{projects:[{id,name,status,graph_url,nodes,edges,...}]}`. Основной проект имеет id=main. POST `/api/projects` принимает multipart/form-data: name, nodes (nodes.parquet), edges (edges.parquet), transactions (transactions.parquet), до 200 МБ суммарно. Сервер запускает опубликованный пайплайн, проверки и загрузку результата в PostgreSQL. HTTP 200 возвращает метаданные status=ready, check_ok=true и graph_url. Ошибка расчёта/проверок: HTTP 422 и status=error; ошибка записи БД: HTTP 500, проект не становится ready. Поле log содержит протокол расчёта.

Граф читается по graph_url из ответа. Для GET-сцен передавайте `?project=<id>&gid=…`, для POST — поле project в JSON. Если project отсутствует, используется main. Факты, журнал, решения и локальный файл подтверждённого запроса относятся к результату выбранного проекта. Одинаковые результаты делят один run_id; повторная загрузка не дублирует строки результата в БД, но создаёт отдельную запись проекта на диске.

## Направление и режим

seed_reach — число других seed с направленным путём **seed → … → выбранный клиент**, без ограничения длины пути. Сам выбранный seed исключён. seed_reach_direction и seed_reach_definition явно передаются модели. top_in[].from — плательщик, top_out[].to — получатель. Достижимость не доказывает происхождение конкретных денег; seed_money — модель смешивания, роли — гипотезы.

Без ключа: mode=replay, model=null. answer._source=template — шаблон из фактов, replay — сохранённый ответ. Успешный живой ответ: mode=live и _source=live:<model>. При сбое провайдера возвращается HTTP 502 с явной ошибкой; шаблон replay не подставляется. Таймаут SDK 45 секунд, автоматических повторов нет. Health показывает настроенный режим; для проверки live проверять именно объяснение. Replay не является проверкой live-модели.

## Ошибки

JSON: {"error":"сообщение"}. HTTP 400 — неверный JSON/параметр; 403 — JSON POST с чужим Origin; 404 — GID, проект или путь отсутствует; 409 — конфликт решения/устаревший черновик; 422 — загруженные данные не прошли расчёт/проверки; 502 — ошибка live-модели; 503 — база недоступна или запись журнала не выполнена; 500 — внутренняя ошибка. При настроенной базе ошибка записи не считается успешным прогоном. JSON POST ограничен 16 КБ; multipart-загрузка — 200 МБ.

Проверка реального API и журнала: `docker compose exec app python check_integration.py`. Запускать в replay; создаёт два тестовых решения, повтор не дублирует их.
