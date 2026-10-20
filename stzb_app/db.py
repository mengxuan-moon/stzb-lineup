"""SQLite 连接、建表与种子数据。

设计要点：
- 建表用 schema.sql，幂等（IF NOT EXISTS）；
- 规则数值一律落在 rule_param / score_weight，不在代码里硬编码，
  这样玩家后续补充数值时不用改代码、不用迁移；
- 合法性校验不在数据库层做（见 schema.sql 顶部注释）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
# 允许用环境变量把库指到别处（相对路径按当前工作目录解析）。
# 冒烟测试就是这么把 AppTest 引到生产库的**副本**上的：否则界面测试一路
# 往真库里写队伍/评分历史，测试一跑用户的持仓和推荐记录就被改了。
DB_PATH = Path(os.environ.get("STZB_DB") or (BASE_DIR / "data" / "stzb.db"))

ENGINE_VERSION = "1.0.0"
PROFILE_ID = "default"

# 库结构版本：写进 PRAGMA user_version。schema/种子有实质变化时才 +1，
# 否则每次页面加载都会重跑建表脚本 —— 见 README「database is locked」那条。
SCHEMA_VERSION = 5
LOCK_RETRIES = 8
LOCK_BASE_SLEEP = 0.15

# 新用户无 user_settings 行时的默认设置（与 _seed_settings 的 'local' 行保持一致）。
DEFAULT_SETTINGS = {
    "cost_limit": 10.0,
    "allow_dismantle": 0,
    "season": "S1",
    "target_mode": "打地",
    "top_n": 10,
    "budget_profile": "fast",
}

# 系统六维（旧版 core.js WEIGHTS.system，原样复用）
SYSTEM_WEIGHTS = {
    "compliance": 0.20,
    "cost": 0.15,
    "synergy": 0.15,
    "skillFit": 0.20,
    "distance": 0.10,
    "structure": 0.20,
}

# 目标五维（旧版 core.js WEIGHTS.combat，攻城/守城已按场景差异修正：攻城值只作用建筑耐久，
# 打地/野战/守城拼的是输出与生存，故攻城值在这些场景降为 0/0.05。每行合计仍为 1.0）
COMBAT_WEIGHTS = {
    "打地": {"output": 0.32, "survive": 0.28, "control": 0.15, "speed": 0.20, "siege": 0.05},
    "野战": {"output": 0.32, "control": 0.28, "speed": 0.22, "survive": 0.18, "siege": 0.00},
    "攻城": {"siege": 0.45, "output": 0.25, "survive": 0.18, "speed": 0.07, "control": 0.05},
    "守城": {"survive": 0.34, "output": 0.26, "control": 0.24, "speed": 0.16, "siege": 0.00},
}

FINAL_WEIGHTS = {"system": 0.60, "combat": 0.40}

# 规则参数种子。data_status 语义见 README：
#   ok            —— 已确认，参与计算
#   unconfirmed   —— 机制存在但数值待确认，跳过并标注
#   missing_source —— 源数据本身缺失
RULE_PARAMS = [
    # Q4-2 已确认：骑克步、步克弓、弓克骑
    ("troop_counter_chain", "mechanic", json.dumps(["骑", "步", "弓", "骑"], ensure_ascii=False),
     "ok", "用户答复 Q4-2", "相克链：前克后。骑>步>弓>骑"),
    # Q4-4 已确认的距离几何
    ("position_geometry", "mechanic",
     json.dumps({"own": {"大营": 0, "中军": 1, "前锋": 2},
                 "enemy": {"前锋": 3, "中军": 4, "大营": 5},
                 "own_to_enemy_back": 5}, ensure_ascii=False),
     "ok", "用户答复 Q4-4", "每位置 1 格；我方大营→对方大营 = 5"),
    ("verdict_thresholds", "scoring", json.dumps({"ok": 75, "tune": 55}),
     "ok", "旧版 R-VERDICT-01", "≥75 可行；55–74 需调整；<55 不推荐"),
    ("search_budget_fast", "search", json.dumps({"per_lineup": 20, "global_cap": 300}),
     "ok", "用户答复 5-2(A)", "A 档：每队 20 变体，全局 300"),
    ("search_budget_deep", "search", json.dumps({"per_lineup": 200, "global_cap": 3000}),
     "ok", "用户答复 5-2(B)", "B 档：每队 200 变体，全局 3000"),
    ("evidence_excerpt_max_chars", "scoring", "120", "ok", "N2(b) 摘录规则", "社区摘录长度上限"),
    ("combo_bonus_coeff", "mechanic", None, "unconfirmed", "用户答复 Q4-6", "组合实际加成数值待补"),
    ("camp_bonus", "mechanic", None, "unconfirmed", "用户答复 Q4-6", "阵营加成数值待补"),
    ("troop_bonus", "mechanic", None, "unconfirmed", "用户答复 Q4-6", "兵种加成数值待补"),
    # (1) 相克是「减伤」而非「增伤」：骑克步 = 步兵对骑兵伤害 −30%，步克弓 = 弓对步 −25%，
    #     弓克骑 = 骑对弓 −20%。数值来自社区（9game/率土兵种介绍），非官方面板。
    ("troop_counter_damage", "mechanic",
     json.dumps({"骑": {"beats": "步", "reduce": 0.30},
                 "步": {"beats": "弓", "reduce": 0.25},
                 "弓": {"beats": "骑", "reduce": 0.20}}, ensure_ascii=False),
     "unconfirmed", "社区：9game 兵种相克效果 + 率土兵种介绍",
     "相克=被克方对我方伤害按 reduce 降低（减伤，非增伤）"),
    # (2) 阵营/兵种加成：3 同阵营（点将台 1 级）全属性 +5.5%；3 同兵种 +10% 攻/+10% 速；2 同兵种 +5%/+5%。
    ("camp_bonus_table", "mechanic", json.dumps({"3": 0.055, "2": None}, ensure_ascii=False),
     "unconfirmed", "率土点将台 1 级", "3 同阵营全属性 +5.5%；2 同阵营数值(待补充：…)"),
    ("troop_bonus_table", "mechanic",
     json.dumps({"3": {"atk": 0.10, "spd": 0.10}, "2": {"atk": 0.05, "spd": 0.05}}, ensure_ascii=False),
     "unconfirmed", "率土兵种加成", "3 同兵种 +10% 攻/+10% 速；2 同兵种 +5%/+5%"),
    # (3) synergy 硬编码分值改为可配置：默认即旧版 core.js 原值（分数不变）。
    ("synergy_points", "scoring",
     json.dumps({"camp": {"3": 55, "2": 30, "1": 10}, "troop": {"3": 25, "2": 12, "1": 0}, "combo": 20},
                ensure_ascii=False),
     "ok", "旧版 core.js 原值，暂定", "协同分三段取值；combo 恒 +20"),
    # (5) distance 维度内部合成比例：0.6 战法覆盖 + 0.4 站位适性（无官方权重）。
    ("distance_split", "scoring", json.dumps({"skill_cover": 0.6, "position_fit": 0.4}, ensure_ascii=False),
     "unconfirmed", "攻略一致认为大营输出/前锋扛伤是最强约束",
     "比例依据：攻略一致认为大营输出/前锋扛伤是最强约束，但无官方权重"),
    # (6) 战法冲突扣分：每条冲突 −8，上限 −16。
    ("conflict_penalty", "scoring", json.dumps({"per": 8, "cap": 16}, ensure_ascii=False),
     "unconfirmed", "战法冲突口径待确认", "structure_score 每条冲突扣 per 分，上限 cap 分"),
    # (8) 攻城战法识别：攻城队应带攻城类战法（云梯/冲车/毁墙等），堆攻城值。
    ("siege_skill_keywords", "mechanic",
     json.dumps(["攻城", "云梯", "冲车", "毁墙", "器械"], ensure_ascii=False),
     "ok", "社区攻城队攻略", "siege 分量 = 0.7*攻城属性norm + 0.3*min(1,攻城战法数/2)"),
]


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开数据库；默认落在 stzb_app/data/stzb.db。

    check_same_thread=False：Streamlit 每次 rerun 都可能换脚本线程
    （1.66 的 runner.fastReruns 默认 True），若沿用 Python 默认的线程亲和，
    第二次交互就会抛 ProgrammingError 并被误判成「数据库为空」。
    这里用 timeout + busy_timeout 兜住并发写，WAL 让读写互不阻塞。

    注意 WAL 只在**还不是 WAL** 时才切：`PRAGMA journal_mode=WAL` 需要一个
    短暂的独占锁，多个会话/进程同时 connect 时它会直接抛 database is locked，
    而且 busy_timeout 对这类库级切换不一定生效。
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def _retry_locked(fn):
    """database is locked / busy 时退避重试：并发写是单机多标签页的常态。"""
    last: Exception | None = None
    for i in range(LOCK_RETRIES):
        try:
            return fn()
        except sqlite3.OperationalError as exc:  # pragma: no cover - 并发路径
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = exc
            time.sleep(LOCK_BASE_SLEEP * (1.7 ** i))
    raise last  # type: ignore[misc]


def db_ready(conn: sqlite3.Connection) -> bool:
    """库是否已经是当前版本（只读检查，不写库、不加写锁）。"""
    try:
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            return False
        row = conn.execute(
            "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name IN ('hero','skill','user_settings')"
        ).fetchone()
        return int(row["c"]) == 3
    except sqlite3.DatabaseError:
        return False


def init_db(conn: sqlite3.Connection, force: bool = False) -> None:
    """建表 + 种子数据（幂等，且已经有版本标记时**完全不写库**）。

    force=True 用于 ingest --rebuild：重建后 schema 可能变了，必须重跑。
    """
    if not force and db_ready(conn):
        return
    _retry_locked(lambda: _init_once(conn))


def _init_once(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    _seed_profiles(conn)
    _seed_params(conn)
    _seed_settings(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：老库补列 + 目标权重旧值→新值 + 多用户 user_key（不丢数据）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lineup)")}
    if "fingerprint" not in cols:
        conn.execute("ALTER TABLE lineup ADD COLUMN fingerprint TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lineup_fingerprint ON lineup(fingerprint)")

    # v2 → v3：四目标权重按「场景差异」新口径重排（每行合计仍 1.0）。
    # 只改「仍等于旧默认值」的行（带旧值条件），用户改过的自定义权重原样保留。
    # 逐行 old→new 对照（只列发生变化的 dim，未变化的 dim 无需迁移）。
    _weight_migration: list[tuple[str, str, float, float]] = [
        ("打地", "output", 0.30, 0.32),
        ("打地", "survive", 0.25, 0.28),
        ("打地", "speed", 0.15, 0.20),
        ("打地", "siege", 0.15, 0.05),
        ("野战", "output", 0.30, 0.32),
        ("野战", "control", 0.25, 0.28),
        ("野战", "speed", 0.20, 0.22),
        ("野战", "survive", 0.15, 0.18),
        ("野战", "siege", 0.10, 0.00),
        ("攻城", "siege", 0.40, 0.45),
        ("攻城", "survive", 0.20, 0.18),
        ("攻城", "speed", 0.10, 0.07),
        ("守城", "survive", 0.30, 0.34),
        ("守城", "output", 0.25, 0.26),
        ("守城", "control", 0.20, 0.24),
        ("守城", "siege", 0.15, 0.00),
        ("守城", "speed", 0.10, 0.16),
    ]
    for mode, dim, old, new in _weight_migration:
        conn.execute(
            "UPDATE score_weight SET weight=? "
            "WHERE profile_id=? AND scope='target' AND target_mode=? AND dim_code=? AND weight=?",
            (new, PROFILE_ID, mode, dim, old),
        )

    # v4 → v5：五张用户表加 user_key（多用户隔离）。幂等：已有该列就整表跳过。
    _migrate_user_key(conn)


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    """表里是否已有某列（迁移幂等判断用）。"""
    return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate_user_key(conn: sqlite3.Connection) -> None:
    """v4→v5：user_hero/user_skill/user_settings 重建主键为含 user_key；
    lineup/eval_run 补 user_key 列 + 重建受影响索引。老数据一律 user_key='local'。

    SQLite 不能直接改主键，所以改 PK 的表走「建新表→copy→drop→rename」；
    lineup/eval_run 只是加列 + 改索引，ALTER TABLE ADD COLUMN 即可。每张表先
    查列是否已存在，避免重复执行；「database is locked」由 _retry_locked 兜底，
    而锁会阻止 DDL 执行，故不会留下半迁移状态。
    """
    # --- user_hero：PK (user_key, hero_id) ---
    if not _has_col(conn, "user_hero", "user_key"):
        conn.execute(
            "CREATE TABLE user_hero_new ("
            " user_key TEXT NOT NULL DEFAULT 'local',"
            " hero_id TEXT NOT NULL REFERENCES hero(hero_id) ON DELETE CASCADE,"
            " owned INTEGER NOT NULL DEFAULT 1,"
            " copies INTEGER,"
            " advance_level INTEGER,"
            " awaken INTEGER,"
            " note TEXT,"
            " PRIMARY KEY (user_key, hero_id))"
        )
        conn.execute(
            "INSERT INTO user_hero_new(user_key,hero_id,owned,copies,advance_level,awaken,note)"
            " SELECT 'local',hero_id,owned,copies,advance_level,awaken,note FROM user_hero"
        )
        conn.execute("DROP TABLE user_hero")
        conn.execute("ALTER TABLE user_hero_new RENAME TO user_hero")

    # --- user_skill：PK (user_key, skill_id) ---
    if not _has_col(conn, "user_skill", "user_key"):
        conn.execute(
            "CREATE TABLE user_skill_new ("
            " user_key TEXT NOT NULL DEFAULT 'local',"
            " skill_id TEXT NOT NULL REFERENCES skill(skill_id) ON DELETE CASCADE,"
            " owned INTEGER NOT NULL DEFAULT 1,"
            " copies INTEGER,"
            " acquired_by_dismantle INTEGER,"
            " unlocked INTEGER,"
            " note TEXT,"
            " PRIMARY KEY (user_key, skill_id))"
        )
        conn.execute(
            "INSERT INTO user_skill_new(user_key,skill_id,owned,copies,acquired_by_dismantle,unlocked,note)"
            " SELECT 'local',skill_id,owned,copies,acquired_by_dismantle,unlocked,note FROM user_skill"
        )
        conn.execute("DROP TABLE user_skill")
        conn.execute("ALTER TABLE user_skill_new RENAME TO user_skill")

    # --- user_settings：每用户一行，PK 用 user_key（原 settings_id 恒为 1，丢弃） ---
    if not _has_col(conn, "user_settings", "user_key"):
        conn.execute(
            "CREATE TABLE user_settings_new ("
            " user_key TEXT PRIMARY KEY,"
            " cost_limit REAL NOT NULL DEFAULT 10.0,"
            " allow_dismantle INTEGER NOT NULL DEFAULT 0,"
            " season TEXT NOT NULL DEFAULT 'S1',"
            " target_mode TEXT NOT NULL DEFAULT '打地',"
            " top_n INTEGER NOT NULL DEFAULT 10,"
            " budget_profile TEXT NOT NULL DEFAULT 'fast')"
        )
        conn.execute(
            "INSERT INTO user_settings_new(user_key,cost_limit,allow_dismantle,season,target_mode,top_n,budget_profile)"
            " SELECT 'local',cost_limit,allow_dismantle,season,target_mode,top_n,budget_profile"
            " FROM user_settings WHERE settings_id=1"
        )
        conn.execute("DROP TABLE user_settings")
        conn.execute("ALTER TABLE user_settings_new RENAME TO user_settings")

    # --- lineup：补 user_key 列（索引统一在下面建） ---
    if not _has_col(conn, "lineup", "user_key"):
        conn.execute("ALTER TABLE lineup ADD COLUMN user_key TEXT NOT NULL DEFAULT 'local'")

    # --- eval_run：补 user_key 列（索引统一在下面建） ---
    if not _has_col(conn, "eval_run", "user_key"):
        conn.execute("ALTER TABLE eval_run ADD COLUMN user_key TEXT NOT NULL DEFAULT 'local'")

    # --- user_key 相关索引：新库老库都确保存在 ---
    # 新库的 user_key 列来自 schema.sql 的 CREATE TABLE，老库由上面 ALTER 补上，
    # 到这里两边的列都已就位。idx_lineup_fingerprint 从单列改成 (user_key, fingerprint)，
    # 名称不变但定义变了，先 DROP 再 CREATE（IF NOT EXISTS 不会覆盖旧定义）。
    conn.execute("DROP INDEX IF EXISTS idx_lineup_fingerprint")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lineup_fingerprint ON lineup(user_key, fingerprint)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lineup_user_created ON lineup(user_key, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_run_user_created ON eval_run(user_key, created_at)")


def _seed_profiles(conn: sqlite3.Connection) -> None:
    """只在权重缺失时写入（不要每次 init 都 DELETE 再插，那是写风暴）。"""
    conn.execute(
        "INSERT OR IGNORE INTO scoring_profile(profile_id,name,version,merge_system,merge_applicability,is_active)"
        " VALUES(?,?,?,?,?,1)",
        (PROFILE_ID, "默认评分方案（复用旧版六维）", "1", FINAL_WEIGHTS["system"], FINAL_WEIGHTS["combat"]),
    )
    have = int(conn.execute(
        "SELECT COUNT(*) c FROM score_weight WHERE profile_id = ?", (PROFILE_ID,)
    ).fetchone()["c"])
    if have > 0:
        return
    for dim, w in SYSTEM_WEIGHTS.items():
        conn.execute(
            "INSERT INTO score_weight(profile_id,scope,target_mode,dim_code,weight) VALUES(?,?,?,?,?)",
            (PROFILE_ID, "system", "", dim, w),
        )
    for mode, dims in COMBAT_WEIGHTS.items():
        for dim, w in dims.items():
            conn.execute(
                "INSERT INTO score_weight(profile_id,scope,target_mode,dim_code,weight) VALUES(?,?,?,?,?)",
                (PROFILE_ID, "target", mode, dim, w),
            )


def _seed_params(conn: sqlite3.Connection) -> None:
    for key, scope, value, status, source, note in RULE_PARAMS:
        conn.execute(
            "INSERT OR IGNORE INTO rule_param(param_key,scope,value_json,data_status,source,note)"
            " VALUES(?,?,?,?,?,?)",
            (key, scope, value, status, source, note),
        )


def _seed_settings(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO user_settings(user_key,cost_limit,allow_dismantle,season,target_mode,top_n,budget_profile)"
        " VALUES('local',10.0,0,'S1','打地',10,'fast')"
    )


def get_param(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value_json FROM rule_param WHERE param_key=?", (key,)).fetchone()
    if not row:
        return default
    if row["value_json"] is None:
        return default
    try:
        return json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return row["value_json"]


def get_param_status(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT data_status FROM rule_param WHERE param_key=?", (key,)).fetchone()
    return row["data_status"] if row else "missing_source"


def get_settings(conn: sqlite3.Connection, user_key: str = "local") -> dict:
    row = conn.execute("SELECT * FROM user_settings WHERE user_key=?", (user_key,)).fetchone()
    if not row:
        return dict(DEFAULT_SETTINGS)
    return dict(row)


def set_settings(conn: sqlite3.Connection, user_key: str = "local", **kwargs) -> None:
    if not kwargs:
        return
    # 新用户可能还没有这一行，先 upsert 一行再更新，避免 UPDATE 落空。
    conn.execute("INSERT OR IGNORE INTO user_settings(user_key) VALUES(?)", (user_key,))
    cols = ", ".join(f"{k}=?" for k in kwargs)
    conn.execute(f"UPDATE user_settings SET {cols} WHERE user_key=?", (*kwargs.values(), user_key))
    conn.commit()


if __name__ == "__main__":
    connection = connect()
    init_db(connection)
    print(f"initialized: {DB_PATH}")
