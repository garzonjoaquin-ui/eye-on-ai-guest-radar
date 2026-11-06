-- Guest Radar schema. Safe to run more than once.

create table if not exists public.runs (
  id            bigserial primary key,
  week_of       date not null unique,
  title         text not null,
  summary       text,
  briefing_script text,
  audio_url     text,
  audio_seconds integer,
  model         text,
  published     boolean not null default true,
  created_at    timestamptz not null default now()
);

create table if not exists public.guests (
  id            bigserial primary key,
  run_id        bigint not null references public.runs(id) on delete cascade,
  position      integer not null default 0,
  name          text not null,
  role          text,
  org           text,
  category      text not null,
  why_now       text not null,
  angle         text not null,
  previous_note text,
  video_url     text,
  video_title   text,
  sources       jsonb not null default '[]'::jsonb,
  created_at    timestamptz not null default now()
);
create index if not exists guests_run_idx on public.guests(run_id);

create table if not exists public.feedback (
  id         bigserial primary key,
  guest_id   bigint not null references public.guests(id) on delete cascade,
  verdict    text not null check (verdict in ('interested', 'no', 'known')),
  note       text check (note is null or char_length(note) <= 2000),
  created_at timestamptz not null default now()
);
create index if not exists feedback_guest_idx on public.feedback(guest_id);

-- People already interviewed on Eye on AI (the agent never re-suggests them unless flagged as a return guest).
create table if not exists public.past_guests (
  name       text primary key,
  detail     text,
  source     text,
  created_at timestamptz not null default now()
);

alter table public.runs        enable row level security;
alter table public.guests      enable row level security;
alter table public.feedback    enable row level security;
alter table public.past_guests enable row level security;

-- The public hub can read published weeks and their guests.
drop policy if exists "read published runs" on public.runs;
create policy "read published runs" on public.runs for select to anon using (published);

drop policy if exists "read guests of published runs" on public.guests;
create policy "read guests of published runs" on public.guests for select to anon
  using (exists (select 1 from public.runs r where r.id = run_id and r.published));

-- The hub can record and show Craig's reactions. No update/delete from the browser.
drop policy if exists "add feedback" on public.feedback;
create policy "add feedback" on public.feedback for insert to anon with check (true);

drop policy if exists "read feedback" on public.feedback;
create policy "read feedback" on public.feedback for select to anon using (true);

grant usage on schema public to anon;
grant select on public.runs, public.guests, public.feedback to anon;
grant insert on public.feedback to anon;
grant usage, select on sequence public.feedback_id_seq to anon;
