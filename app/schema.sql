-- 全部 DDL 的单一事实来源。修改后删除 data/app.db 重新初始化即可。
-- 时间一律存 UTC ISO8601 文本（'YYYY-MM-DDTHH:MM:SSZ'），字典序即时间序。

-- 逻辑赛事（"蓝桥杯"本身，不分年份）
CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY,
  slug          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  category      TEXT NOT NULL,
  organizer     TEXT,
  official_url  TEXT,
  description   TEXT,
  aliases       TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，供 B 类源标题匹配
  -- 1 = 系列赛事（蓝桥杯、CCPC 这类一年一届，时间线上一届一条泳道）
  -- 0 = 平台聚合（天池、讯飞这类一年上百场独立比赛，时间线上按年聚合成一条）
  series        INTEGER NOT NULL DEFAULT 1,
  active        INTEGER NOT NULL DEFAULT 1
);

-- 数据源注册表 + 健康状态
CREATE TABLE IF NOT EXISTS sources (
  id                   INTEGER PRIMARY KEY,
  key                  TEXT NOT NULL UNIQUE,
  name                 TEXT NOT NULL,
  kind                 TEXT NOT NULL CHECK (kind IN ('structured', 'news')),
  base_url             TEXT,
  enabled              INTEGER NOT NULL DEFAULT 1,
  note                 TEXT,                  -- 给用户看的口径说明，如"数据不完整"
  last_run_at          TEXT,
  last_success_at      TEXT,
  last_error           TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  health               TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (health IN ('ok', 'degraded', 'failing', 'needs_reauth', 'unknown'))
);

-- 一次爬取批次
CREATE TABLE IF NOT EXISTS crawl_runs (
  id            INTEGER PRIMARY KEY,
  trigger       TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual')),
  mode          TEXT NOT NULL DEFAULT 'incremental'
                CHECK (mode IN ('incremental', 'backfill')),
  status        TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  total_new     INTEGER NOT NULL DEFAULT 0,
  total_updated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON crawl_runs(started_at DESC);

-- 单源执行结果（前端进度轮询查这张表）
CREATE TABLE IF NOT EXISTS crawl_run_items (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
  source_key    TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('pending', 'running', 'success', 'failed', 'skipped')),
  started_at    TEXT,
  finished_at   TEXT,
  duration_ms   INTEGER,
  items_found   INTEGER NOT NULL DEFAULT 0,
  items_new     INTEGER NOT NULL DEFAULT 0,
  items_updated INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  UNIQUE (run_id, source_key)
);
CREATE INDEX IF NOT EXISTS idx_run_items_run ON crawl_run_items(run_id);

-- 原始抓取记录。raw_json 永久保留：站点改版后可回放重解析，不必重抓。
CREATE TABLE IF NOT EXISTS source_items (
  id            INTEGER PRIMARY KEY,
  source_key    TEXT NOT NULL,
  external_id   TEXT NOT NULL,
  title         TEXT,
  url           TEXT,
  published_at  TEXT,
  content_hash  TEXT NOT NULL,
  raw_json      TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  UNIQUE (source_key, external_id)
);
CREATE INDEX IF NOT EXISTS idx_source_items_seen ON source_items(source_key, last_seen_at DESC);

-- 核心：赛事届次（赛事 × 年份 × 子赛道）
CREATE TABLE IF NOT EXISTS event_editions (
  id                 INTEGER PRIMARY KEY,
  event_id           INTEGER NOT NULL REFERENCES events(id),
  year               INTEGER NOT NULL,
  -- 系列赛事：赛站/赛道（'哈尔滨站'、'网络赛'）；平台聚合：该场比赛的名称。
  -- 平台类源必须用比赛名做区分，否则同一年的上百场独立比赛会被唯一键折叠成一行
  -- （实测天池 576 场会被压成 17 行，丢 96% 数据）。
  sub_event          TEXT NOT NULL DEFAULT '',
  title              TEXT,                       -- 该条记录的完整标题
  origin             TEXT NOT NULL CHECK (origin IN ('crawl', 'manual')),
  source_key         TEXT NOT NULL DEFAULT '',
  source_item_id     INTEGER REFERENCES source_items(id),
  source_url         TEXT,
  source_note        TEXT,                 -- 人工条目的整理依据

  registration_start TEXT,
  registration_end   TEXT,
  contest_start      TEXT,
  contest_end        TEXT,
  registration_raw   TEXT,                 -- 原始文本，前端悬停显示，便于人工核对
  contest_raw        TEXT,

  time_confidence    TEXT NOT NULL DEFAULT 'low'
                     CHECK (time_confidence IN ('high', 'medium', 'low')),
  time_precision     TEXT DEFAULT 'date' CHECK (time_precision IN ('date', 'second')),
  -- 地域。语义：guangdong=广东线下赛站；other=外省线下赛站；online=无线下举办地
  -- （各竞赛平台）或本就是线上赛。开启"只看广东"时保留 guangdong 与 online，
  -- 只过滤 other —— 否则会把没有举办地的线上赛事一并误删。
  region             TEXT NOT NULL DEFAULT 'online'
                     CHECK (region IN ('guangdong', 'other', 'online')),
  status             TEXT,
  reward             TEXT,
  team_info          TEXT,
  extra              TEXT NOT NULL DEFAULT '{}',
  first_seen_at      TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (event_id, year, sub_event, source_key, origin)
);
CREATE INDEX IF NOT EXISTS idx_ed_year    ON event_editions(year);
CREATE INDEX IF NOT EXISTS idx_ed_reg_end ON event_editions(registration_end);
CREATE INDEX IF NOT EXISTS idx_ed_contest ON event_editions(contest_start);
CREATE INDEX IF NOT EXISTS idx_ed_event   ON event_editions(event_id, year DESC);

-- 人工种子与爬取数据共存，读取时按优先级仲裁：
-- manual 优先于 crawl；同 origin 下 time_confidence 高者优先。人工修正永不被下次爬取冲掉。
CREATE VIEW IF NOT EXISTS v_edition_best AS
SELECT * FROM (
  SELECT e.*,
    ROW_NUMBER() OVER (
      PARTITION BY event_id, year, sub_event
      ORDER BY CASE origin WHEN 'manual' THEN 100 ELSE 50 END DESC,
               CASE time_confidence WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC,
               updated_at DESC
    ) AS rn
  FROM event_editions e
) WHERE rn = 1;
