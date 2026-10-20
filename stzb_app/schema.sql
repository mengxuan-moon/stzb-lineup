-- 率土之滨 配将推荐程序 —— SQLite 表结构
-- 约定：data_status ∈ {ok, missing_source, unconfirmed, not_applicable}
-- 注意：合法性校验一律在应用层做，数据库不设 cost / 兵种 / 冲突组 的 CHECK，
--       否则非法队伍无法入库，用户就看不到"错在哪"。

PRAGMA foreign_keys = ON;

-- ============================================================ A. 基础数据层

CREATE TABLE IF NOT EXISTS hero (
    hero_id            TEXT PRIMARY KEY,
    name               TEXT NOT NULL,        -- 注意：同名多形态武将存在（孙坚×4 等），不能 UNIQUE
    star               INTEGER,
    camp               TEXT,
    troop              TEXT,
    cost               REAL,
    atk_range          INTEGER,
    innate_skill_name  TEXT,
    innate_skill_id    TEXT,
    dismantle_skill1_name TEXT,
    dismantle_skill1_id TEXT,
    dismantle_skill2_name TEXT,
    dismantle_skill2_id TEXT,
    combo_name         TEXT,
    combo_members      TEXT,
    combo_bonus        TEXT,
    combo_cond         TEXT,
    source_url         TEXT,
    data_status        TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS hero_attr (
    hero_id       TEXT NOT NULL REFERENCES hero(hero_id) ON DELETE CASCADE,
    attr_code     TEXT NOT NULL,             -- atk / guile / siege / def / spd
    init_value    REAL,
    growth_value  REAL,                      -- 数据未提供成长值 → NULL + missing_source
    data_status   TEXT NOT NULL DEFAULT 'ok',
    PRIMARY KEY (hero_id, attr_code)
);

CREATE TABLE IF NOT EXISTS skill (
    skill_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    quality         TEXT,                    -- S/A/B/C/D
    skill_type      TEXT,                    -- 主动/指挥/被动/追击
    troop_limit     TEXT,                    -- 例：弓步骑 / 骑
    valid_range     INTEGER,                 -- 有效距离；追击战法为 NULL
    target_group    TEXT,
    target_count    INTEGER,                 -- 解析出的目标个数：1/2/3；全体=3
    proc_text       TEXT,                    -- 发动几率原文
    proc_min        REAL,
    proc_max        REAL,
    desc_text       TEXT,
    research_cond1  TEXT,
    research_cond2  TEXT,
    note_text       TEXT,
    duration_turns  INTEGER,                 -- 未标注 → NULL/unconfirmed
    effect_json     TEXT,                    -- 结构化效果，未标注 → NULL/unconfirmed
    conflict_group_id TEXT,                  -- 未标注 → NULL/unconfirmed
    source_url      TEXT,
    data_status     TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS skill_effect_tag (
    tag_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id    TEXT NOT NULL REFERENCES skill(skill_id) ON DELETE CASCADE,
    tag_code    TEXT NOT NULL,               -- damage.single / damage.aoe / control.chaos / heal / buff.atk ...
    value_text  TEXT,
    source      TEXT NOT NULL DEFAULT 'rule_extract',  -- rule_extract | manual
    confidence  REAL,
    data_status TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS combo (
    combo_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    hero_ids_json TEXT,
    bonus_text    TEXT,
    activate_cond TEXT,
    data_status   TEXT NOT NULL DEFAULT 'ok'
);

-- ============================================================ B. 规则配置层

CREATE TABLE IF NOT EXISTS conflict_group (
    group_id   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'control',   -- conflict | control
    name       TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'soft',      -- hard | soft
    mutually_exclusive INTEGER NOT NULL DEFAULT 0,
    rule_desc  TEXT,
    data_status TEXT NOT NULL DEFAULT 'unconfirmed'
);

CREATE TABLE IF NOT EXISTS skill_conflict (
    skill_id   TEXT NOT NULL REFERENCES skill(skill_id) ON DELETE CASCADE,
    group_id   TEXT NOT NULL REFERENCES conflict_group(group_id) ON DELETE CASCADE,
    priority   INTEGER NOT NULL DEFAULT 1,
    data_status TEXT NOT NULL DEFAULT 'unconfirmed',
    PRIMARY KEY (skill_id, group_id)
);

CREATE TABLE IF NOT EXISTS rule_param (
    param_key   TEXT PRIMARY KEY,
    scope       TEXT NOT NULL DEFAULT 'mechanic', -- mechanic | search | scoring
    value_json  TEXT,
    data_status TEXT NOT NULL DEFAULT 'unconfirmed',
    source      TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS scoring_profile (
    profile_id        TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    version           TEXT NOT NULL DEFAULT '1',
    merge_system      REAL NOT NULL DEFAULT 0.6,
    merge_applicability REAL NOT NULL DEFAULT 0.4,
    is_active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS score_weight (
    profile_id  TEXT NOT NULL REFERENCES scoring_profile(profile_id) ON DELETE CASCADE,
    scope       TEXT NOT NULL,               -- system | target
    target_mode TEXT NOT NULL DEFAULT '',    -- scope=target 时：打地/野战/攻城/守城；scope=system 时为 ''
    dim_code    TEXT NOT NULL,
    weight      REAL NOT NULL,
    rule_json   TEXT,
    PRIMARY KEY (profile_id, scope, target_mode, dim_code)
);

CREATE TABLE IF NOT EXISTS season_pool (
    season    TEXT NOT NULL,
    item_type TEXT NOT NULL,                 -- hero | skill
    item_id   TEXT NOT NULL,
    allowed   INTEGER NOT NULL DEFAULT 1,
    note      TEXT,
    PRIMARY KEY (season, item_type, item_id)
);

-- ============================================================ C. 用户输入层

CREATE TABLE IF NOT EXISTS user_hero (
    user_key      TEXT NOT NULL DEFAULT 'local',
    hero_id       TEXT NOT NULL REFERENCES hero(hero_id) ON DELETE CASCADE,
    owned         INTEGER NOT NULL DEFAULT 1,
    copies        INTEGER,
    advance_level INTEGER,
    awaken        INTEGER,
    note          TEXT,
    PRIMARY KEY (user_key, hero_id)
);

CREATE TABLE IF NOT EXISTS user_skill (
    user_key TEXT NOT NULL DEFAULT 'local',
    skill_id TEXT NOT NULL REFERENCES skill(skill_id) ON DELETE CASCADE,
    owned    INTEGER NOT NULL DEFAULT 1,
    copies   INTEGER,
    acquired_by_dismantle INTEGER,
    unlocked INTEGER,
    note     TEXT,
    PRIMARY KEY (user_key, skill_id)
);

CREATE TABLE IF NOT EXISTS lineup (
    lineup_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key        TEXT NOT NULL DEFAULT 'local',
    name            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'manual',   -- user | manual | variant | generated
    parent_lineup_id INTEGER REFERENCES lineup(lineup_id) ON DELETE SET NULL,
    swap_note       TEXT,
    target_mode     TEXT,
    fingerprint     TEXT,                              -- 武将+战法指纹，用于跨 run 复用同一队伍
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    note            TEXT
);

CREATE TABLE IF NOT EXISTS lineup_member (
    lineup_id INTEGER NOT NULL REFERENCES lineup(lineup_id) ON DELETE CASCADE,
    position  TEXT NOT NULL CHECK (position IN ('大营','中军','前锋')),
    hero_id   TEXT NOT NULL REFERENCES hero(hero_id),
    skill1_id TEXT REFERENCES skill(skill_id),
    skill2_id TEXT REFERENCES skill(skill_id),
    locked    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lineup_id, position),
    UNIQUE (lineup_id, hero_id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_key       TEXT PRIMARY KEY,
    cost_limit     REAL NOT NULL DEFAULT 10.0,
    allow_dismantle INTEGER NOT NULL DEFAULT 0,
    season         TEXT NOT NULL DEFAULT 'S1',
    target_mode    TEXT NOT NULL DEFAULT '打地',
    top_n          INTEGER NOT NULL DEFAULT 10,
    budget_profile TEXT NOT NULL DEFAULT 'fast'
);

-- ============================================================ D. 结果层

CREATE TABLE IF NOT EXISTS eval_run (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key       TEXT NOT NULL DEFAULT 'local',
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    profile_id     TEXT NOT NULL DEFAULT 'default',
    target_mode    TEXT NOT NULL,
    cost_limit     REAL NOT NULL,
    budget_profile TEXT NOT NULL,
    params_snapshot_json TEXT,
    engine_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_score (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES eval_run(run_id) ON DELETE CASCADE,
    lineup_id      INTEGER REFERENCES lineup(lineup_id) ON DELETE SET NULL,
    rank           INTEGER,
    total_score    REAL,
    system_score   REAL,
    applicability_score REAL,
    legality       TEXT,                     -- pass | warn | fail
    dim_scores_json TEXT,
    verdict        TEXT,
    fingerprint    TEXT,
    UNIQUE (run_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS finding (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES eval_run(run_id) ON DELETE CASCADE,
    lineup_id  INTEGER,
    rule_id    TEXT,
    level      TEXT NOT NULL,                -- error | warn | info
    message    TEXT NOT NULL,
    field_path TEXT
);

CREATE TABLE IF NOT EXISTS reason_item (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES eval_run(run_id) ON DELETE CASCADE,
    lineup_id  INTEGER,
    kind       TEXT NOT NULL,                -- rule | battle | community
    text       TEXT NOT NULL,
    basis_json TEXT,
    evidence_message_id TEXT
);

CREATE TABLE IF NOT EXISTS substitute (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES eval_run(run_id) ON DELETE CASCADE,
    lineup_id   INTEGER,
    kind        TEXT NOT NULL,               -- swap_hero | swap_skill | downgrade
    from_id     TEXT,
    to_id       TEXT,
    delta_score REAL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS counter_relation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES eval_run(run_id) ON DELETE CASCADE,
    lineup_id    INTEGER,
    counter_type TEXT NOT NULL,              -- troop | control | output
    target_desc  TEXT,
    direction    TEXT NOT NULL,              -- we_counter | they_counter | mutual
    confidence   REAL,
    basis_json   TEXT
);

-- ============================================================ E. 证据层

CREATE TABLE IF NOT EXISTS evidence_doc (
    message_id    TEXT PRIMARY KEY,
    kind          TEXT,                      -- lineup | analysis
    url           TEXT,
    author        TEXT,
    ts            TEXT,
    title         TEXT,
    body          TEXT,
    likes         INTEGER,
    hero_ids_json TEXT,
    skill_ids_json TEXT
);

CREATE TABLE IF NOT EXISTS evidence_hit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES eval_run(run_id) ON DELETE CASCADE,
    lineup_id  INTEGER,
    message_id TEXT REFERENCES evidence_doc(message_id) ON DELETE SET NULL,
    matched_terms TEXT,
    score      REAL,
    excerpt    TEXT
);

CREATE TABLE IF NOT EXISTS battle_stat (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trio_key     TEXT NOT NULL UNIQUE,       -- 三个 hero_id 排序后 join
    hero_ids_json TEXT NOT NULL,
    games        INTEGER NOT NULL DEFAULT 0,
    wins         INTEGER NOT NULL DEFAULT 0,
    losses       INTEGER NOT NULL DEFAULT 0,
    pve_games    INTEGER NOT NULL DEFAULT 0,
    sample_flag  TEXT NOT NULL DEFAULT 'ok', -- ok | low_sample
    win_rate     REAL
);

-- ============================================================ F. 溯源与预留

CREATE TABLE IF NOT EXISTS data_source (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL,
    sha256      TEXT,
    rows        INTEGER,
    cols        INTEGER,
    imported_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS data_issue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT NOT NULL,
    row_key     TEXT,
    field       TEXT,
    data_status TEXT NOT NULL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS hero_skill_binding (
    hero_id TEXT NOT NULL REFERENCES hero(hero_id) ON DELETE CASCADE,
    skill_id TEXT NOT NULL REFERENCES skill(skill_id) ON DELETE CASCADE,
    source  TEXT,
    PRIMARY KEY (hero_id, skill_id)
);

-- 武将立绘（联网抓取的本地缓存；v1 无图源，抓不到时程序用占位卡面）
-- file_name 相对 components/lineup_board/img/，前端用 onerror 回落到文字占位。
CREATE TABLE IF NOT EXISTS hero_portrait (
    hero_id    TEXT PRIMARY KEY REFERENCES hero(hero_id) ON DELETE CASCADE,
    file_name  TEXT NOT NULL,                -- 例：1001.jpg
    source_url TEXT NOT NULL,
    bytes      INTEGER,
    width      INTEGER,
    height     INTEGER,
    fetched_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'ok',   -- ok / missing / failed
    note       TEXT
);

-- ============================================================ 索引

CREATE INDEX IF NOT EXISTS idx_hero_name ON hero(name);
CREATE INDEX IF NOT EXISTS idx_hero_camp ON hero(camp);
CREATE INDEX IF NOT EXISTS idx_skill_name ON skill(name);
CREATE INDEX IF NOT EXISTS idx_skill_type ON skill(skill_type);
CREATE INDEX IF NOT EXISTS idx_member_hero ON lineup_member(hero_id);
CREATE INDEX IF NOT EXISTS idx_score_run ON team_score(run_id, rank);
CREATE INDEX IF NOT EXISTS idx_reason_run ON reason_item(run_id, lineup_id);
CREATE INDEX IF NOT EXISTS idx_sub_run ON substitute(run_id, lineup_id);
CREATE INDEX IF NOT EXISTS idx_counter_run ON counter_relation(run_id, lineup_id);
CREATE INDEX IF NOT EXISTS idx_hit_run ON evidence_hit(run_id, lineup_id);
