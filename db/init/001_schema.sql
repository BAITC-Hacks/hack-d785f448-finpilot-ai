-- Схема базы для «Граф денег». Выполняется автоматически при первом старте контейнера db
-- (docker-entrypoint-initdb.d). Повторная инициализация: docker compose down -v.
-- Приложение пишет сюда результаты прогона пайплайна и журнал ассистента; фронт читает graph.json.

create table if not exists runs (
  run_id       text primary key,                 -- sha256 от nodes_roles.csv: одинаковый результат = один run_id
  created_at   timestamptz not null default now(),
  nodes        integer not null,
  edges        integer not null,
  converged    boolean not null,                  -- след денег сошёлся (run_meta.json)
  iterations   integer not null,
  capture_30   numeric(8,5)                       -- модельный охват плана из 30 счетов (plan_meta.json)
);

create table if not exists nodes_roles (
  run_id         text not null references runs(run_id) on delete cascade,
  gid            text not null,                   -- 18 цифр — только текстом
  role           text not null check (role in ('coordinator','consolidator','distributor','transit','terminal','peripheral')),
  rule_id        text not null,
  role_score     numeric(5,3) not null,
  cluster_id     integer not null,
  priority_score numeric(6,4) not null,
  evidence       text not null,
  depth          integer not null,
  is_seed        boolean not null,
  truncated      boolean not null,
  in_deg         integer not null,
  out_deg        integer not null,
  in_kzt         numeric(16,2) not null,
  out_kzt        numeric(16,2) not null,
  seed_money     numeric(16,2) not null,
  primary key (run_id, gid)
);
create index if not exists nodes_roles_priority on nodes_roles (run_id, priority_score desc);

create table if not exists node_flags (
  run_id            text not null references runs(run_id) on delete cascade,
  gid               text not null,
  payer_of_couriers boolean not null,
  courier_candidate boolean not null,
  beneficiary       boolean not null,
  in_cycle          boolean not null,
  layering          boolean not null,
  split_in          boolean not null,
  flags_evidence    text,
  primary key (run_id, gid)
);

create table if not exists block_plan (
  run_id            text not null references runs(run_id) on delete cascade,
  step              integer not null,
  gid               text not null,
  role              text not null,
  gain              numeric(8,5) not null,
  cumulative        numeric(8,5) not null,
  money_stopped_kzt numeric(16,2) not null,
  primary key (run_id, step)
);

-- журнал ассистента: что спросили, какие факты дали модели, что она ответила
create table if not exists agent_runs (
  id         bigserial primary key,
  run_id     text references runs(run_id) on delete set null,
  kind       text not null,                       -- explain / decide / whatif / request_approved
  mode       text not null check (mode in ('live','replay')),
  model      text,
  facts      jsonb not null,
  output     jsonb not null,
  created_at timestamptz not null default now()
);

-- подтверждения человеком действий с последствиями (сцена 4)
create table if not exists approvals (
  id         bigserial primary key,
  run_id     text references runs(run_id) on delete set null,
  gid        text not null,
  action     text not null,
  approved   boolean not null,
  reviewer   text not null,
  created_at timestamptz not null default now()
);
