create table demo_cases (
  id text primary key,
  title text not null,
  inputs jsonb not null
);

create table agent_runs (
  id uuid primary key default gen_random_uuid(),
  case_id text references demo_cases(id),
  mode text not null check (mode in ('live', 'replay')),
  model text,
  facts jsonb not null,
  output jsonb not null,
  created_at timestamptz not null default now()
);

create table approvals (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references agent_runs(id),
  action text not null,
  approved boolean not null,
  reviewer text not null,
  created_at timestamptz not null default now()
);

alter table demo_cases enable row level security;
alter table agent_runs enable row level security;
alter table approvals  enable row level security;

grant select on demo_cases, agent_runs, approvals to anon;
grant insert on agent_runs, approvals to anon;

create policy "read demo cases"  on demo_cases for select to anon using (true);
create policy "read runs"        on agent_runs for select to anon using (true);
create policy "insert runs"      on agent_runs for insert to anon with check (true);
create policy "read approvals"   on approvals  for select to anon using (true);
create policy "insert approvals" on approvals  for insert to anon with check (true);
-- UPDATE и DELETE: ни grant, ни политики → запрещены.

