-- AI Test Generator schema (mirrors backend/db.py).
-- Apply with the Supabase CLI (`supabase db push`) or paste into the SQL editor.

create table if not exists public.users (
  id            bigint primary key,              -- GitHub user id
  login         varchar(100) not null,
  name          varchar(255),
  avatar_url    text,
  created_at    timestamptz not null default now(),
  last_login_at timestamptz not null default now()
);

create table if not exists public.sessions (
  id_hash      varchar(64) primary key,          -- sha256 of the session cookie value
  user_id      bigint not null references public.users (id) on delete cascade,
  github_token text not null,                    -- Fernet-encrypted with SESSION_SECRET
  scopes       text,
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null
);
create index if not exists ix_sessions_user_id on public.sessions (user_id);

create table if not exists public.runs (
  id            varchar(36) primary key,
  user_id       bigint not null references public.users (id) on delete cascade,
  user_login    varchar(100) not null,
  kind          varchar(16) not null check (kind in ('code', 'repo', 'spec')),
  status        varchar(16) not null check (status in ('queued', 'running', 'completed', 'failed')),
  project       varchar(300) not null,
  owner         varchar(100),                    -- GitHub owner (user or organization) of analysed repos
  ref           varchar(255),
  commit_sha    varchar(64),
  languages     text,
  model         varchar(100),
  progress      integer not null default 0,
  progress_note text,
  summary       jsonb,
  report        jsonb,
  error         text,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz,
  duration_ms   integer
);
create index if not exists ix_runs_user_created  on public.runs (user_id, created_at);
create index if not exists ix_runs_owner_created on public.runs (owner, created_at);
create index if not exists ix_runs_project       on public.runs (project);

-- Row Level Security on, with no policies: the Supabase anon/authenticated roles
-- (PostgREST, supabase-js) cannot read or write these tables. Only the backend,
-- connecting with the database connection string, has access.
alter table public.users    enable row level security;
alter table public.sessions enable row level security;
alter table public.runs     enable row level security;
