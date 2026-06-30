-- ══════════════════════════════════════════════════════
-- NorthstarAI — Trade Bot Tables
-- Kör detta i Supabase SQL Editor (supabase.com → SQL Editor)
-- ══════════════════════════════════════════════════════

-- 1. Daglig pre-market analys
create table if not exists bot_analyses (
  id          bigserial primary key,
  created_at  timestamptz default now(),
  symbol      text not null,
  date        date not null,
  close       numeric,
  ema20       numeric,
  ema50       numeric,
  ema200      numeric,
  rsi         numeric,
  macd_hist   numeric,
  atr         numeric,
  bb_upper    numeric,
  bb_lower    numeric,
  support     numeric,
  resistance  numeric,
  bull_score  integer,
  bias        text,        -- 'LONG' | 'SHORT' | 'FLAT'
  notes       text[]
);

-- 2. Trade-signaler (varje gång boten överväger entry)
create table if not exists bot_signals (
  id          bigserial primary key,
  created_at  timestamptz default now(),
  symbol      text not null,
  signal      text not null,   -- 'LONG' | 'SHORT' | 'FLAT' | 'NO_BREAKOUT'
  orb_high    numeric,
  orb_low     numeric,
  price       numeric,
  reason      text
);

-- 3. Trades (faktiska ordrar)
create table if not exists bot_trades (
  id           bigserial primary key,
  created_at   timestamptz default now(),
  symbol       text not null,
  side         text not null,   -- 'buy' | 'sell'
  qty          integer,
  entry_price  numeric,
  stop_loss    numeric,
  take_profit  numeric,
  risk_usd     numeric,
  status       text default 'open',   -- 'open' | 'closed' | 'cancelled'
  exit_price   numeric,
  pnl          numeric,
  closed_at    timestamptz
);

-- Index för snabba queries från NorthstarAI
create index if not exists idx_bot_analyses_date   on bot_analyses(date desc);
create index if not exists idx_bot_trades_status   on bot_trades(status);
create index if not exists idx_bot_signals_created on bot_signals(created_at desc);
