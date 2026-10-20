"""把 CSV / JSON 数据导入 SQLite。

用法：
    python -m stzb_app.ingest            # 增量导入（幂等）
    python -m stzb_app.ingest --rebuild  # 删库重建

导入顺序有依赖：skill → hero（需要按名称解析“基础战法/可拆战法”的外键）→ combo → 证据 → 战报。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from . import db as dbmod
from .core import chance_range, clean, is_missing, num, target_count_of

ROOT = Path(__file__).resolve().parent.parent
HERO_CSV = ROOT / "stzb_hero_data" / "率土之滨_武将属性表.csv"
SKILL_CSV = ROOT / "stzb_skill_data" / "率土之滨_战法表.csv"
WAR_JSON = ROOT / "stzb_team_eval" / "战报采集" / "war_reports.json"
BATTLE_JSON = ROOT / "stzb_team_eval" / "战报采集" / "battles.json"

MISSING_TOKENS = {"", "未获取", "--", "-", "无", "nan", "None"}
TRUE_TOKENS = {"1", "true", "yes", "y", "是"}


def _as_bool(v: Any) -> bool:
    """健壮布尔解析：`bool("0")`/`bool("false")` 在 Python 里是 True，直接用会把 PvP 战报算成 PvE。

    规则：bool 直取；数字非 0 为真；字符串去空白转小写后只认 TRUE_TOKENS，其余一律假。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return clean(v).lower() in TRUE_TOKENS



# ------------------------------------------------------------------ 通用读取


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = [h.strip() for h in (reader.fieldnames or [])]
        rows = []
        for raw in reader:
            rows.append({(k or "").strip(): (v if v is not None else "") for k, v in raw.items()})
    return header, rows


def unwrap_records(obj: Any) -> list[dict]:
    """从 JSON 顶层（list / dict 各种包装）里找出记录数组。"""
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        for key in ("data", "records", "items", "list", "reports", "result", "warReports", "battles"):
            v = obj.get(key)
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return [r for r in v if isinstance(r, dict)]
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):
                inner = unwrap_records(v)
                if inner:
                    return inner
    return []


def pick(rec: dict, *names: str, default=None):
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return default


# ------------------------------------------------------------------ 战法 / 武将


def split_named_value(text: str) -> tuple[str, str]:
    """'斩铁：对敌军单体…' -> ('斩铁', '对敌军单体…')；没有冒号时返回 (text, '')。"""
    t = clean(text)
    for sep in ("：", ":"):
        if sep in t:
            name, _, desc = t.partition(sep)
            return clean(name), clean(desc)
    return t, ""


class SkillNameResolver:
    """按战法名 → 战法ID 解析。键名兼容数据库行（skill_id/name）与 CSV 原始行（战法ID/战法名称）。"""

    def __init__(self, skills: list[dict]) -> None:
        self.by_name: dict[str, str] = {}
        for s in skills:
            sid = clean(s.get("skill_id") or s.get("战法ID") or "")
            nm = clean(s.get("name") or s.get("战法名称") or "")
            if sid and nm:
                self.by_name.setdefault(nm, sid)
        # 长名优先，避免「攻击」抢先匹配「攻击强化」
        self.names_by_len = sorted(self.by_name, key=len, reverse=True)

    def resolve(self, text: str) -> tuple[str | None, str]:
        t = clean(text)
        if not t or t in MISSING_TOKENS:
            return None, t
        name, _ = split_named_value(t)
        for cand in (name, t):
            if cand in self.by_name:
                return self.by_name[cand], cand
            stripped = re.sub(r"[（(].*?[)）]", "", cand).strip()
            if stripped in self.by_name:
                return self.by_name[stripped], stripped
        return None, name or t


def ingest_skills(conn, path: Path) -> dict:
    header, rows = read_csv_rows(path)
    stats = {"rows": len(rows), "inserted": 0, "dup": 0, "missing_range": 0, "missing_proc": 0, "issue": 0}
    seen: set[str] = set()
    for r in rows:
        sid = clean(r.get("战法ID"))
        name = clean(r.get("战法名称"))
        if not sid or not name:
            stats["issue"] += 1
            continue
        if sid in seen:
            stats["dup"] += 1
            continue
        seen.add(sid)
        rng_text = clean(r.get("有效距离"))
        rng = None if rng_text in MISSING_TOKENS else int(num(rng_text) or 0)
        if rng is None:
            stats["missing_range"] += 1
        ch = chance_range(r.get("发动几率"))
        if ch is None:
            stats["missing_proc"] += 1
        conn.execute(
            "INSERT OR REPLACE INTO skill(skill_id,name,quality,skill_type,troop_limit,valid_range,"
            "target_group,target_count,proc_text,proc_min,proc_max,desc_text,research_cond1,research_cond2,"
            "note_text,duration_turns,effect_json,conflict_group_id,source_url,data_status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
            (sid, name, clean(r.get("战法品质")) or None, clean(r.get("战法类型")) or None,
             clean(r.get("兵种类型")) or None, rng, clean(r.get("目标群体")) or None,
             target_count_of(r.get("目标群体")), clean(r.get("发动几率")) or None,
             ch["lo"] if ch else None, ch["hi"] if ch else None,
             clean(r.get("战法描述")) or None, clean(r.get("研究条件1")) or None,
             clean(r.get("研究条件2")) or None, clean(r.get("注意")) or None,
             clean(r.get("详情页URL")) or None,
             "ok" if rng is not None or ch is not None else "missing_source"),
        )
        stats["inserted"] += 1
    dbmod_record_source(conn, path, len(rows), len(header))
    return stats


def ingest_heroes(conn, path: Path, resolver: SkillNameResolver) -> dict:
    header, rows = read_csv_rows(path)
    stats = {"rows": len(rows), "inserted": 0, "unmatched": [], "missing_d2": 0, "missing_combo": 0,
             "dup_name": 0}
    seen_names: set[str] = set()
    for r in rows:
        hid = clean(r.get("武将ID"))
        name = clean(r.get("武将名称"))
        if not hid or not name:
            continue
        if name in seen_names:
            stats["dup_name"] += 1
        seen_names.add(name)
        innate_name_text = clean(r.get("基础战法"))
        d1_text = clean(r.get("可拆战法"))
        d2_text = clean(r.get("可拆战法2"))
        innate_id, innate_name = resolver.resolve(innate_name_text)
        d1_id, d1_name = resolver.resolve(d1_text)
        d2_id, d2_name = resolver.resolve(d2_text)
        for label, raw, resolved in (("基础战法", innate_name_text, innate_id),
                                     ("可拆战法", d1_text, d1_id),
                                     ("可拆战法2", d2_text, d2_id)):
            if raw and raw not in MISSING_TOKENS and not resolved:
                stats["unmatched"].append((name, label, raw))
        if not d2_id:
            stats["missing_d2"] += 1
        combo_name = clean(r.get("武将组合"))
        if not combo_name or combo_name in MISSING_TOKENS:
            stats["missing_combo"] += 1
            combo_name = None
        conn.execute(
            "INSERT OR REPLACE INTO hero(hero_id,name,star,camp,troop,cost,atk_range,"
            "innate_skill_name,innate_skill_id,dismantle_skill1_name,dismantle_skill1_id,"
            "dismantle_skill2_name,dismantle_skill2_id,combo_name,combo_members,combo_bonus,"
            "combo_cond,source_url,data_status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hid, name, int(num(r.get("武将星级")) or 0) or None, clean(r.get("阵营")) or None,
             clean(r.get("兵种")) or None, num(r.get("cost")), int(num(r.get("攻击距离")) or 0) or None,
             innate_name or None, innate_id, d1_name or None, d1_id, d2_name or None, d2_id,
             combo_name, clean(r.get("成员武将")) or None, clean(r.get("加成属性")) or None,
             clean(r.get("激活条件")) or None, clean(r.get("详情页URL")) or None,
             "ok" if innate_id else "unconfirmed"),
        )
        for code, col in (("guile", "初始谋略"), ("atk", "初始攻击"), ("siege", "初始攻城"),
                          ("def", "初始防御"), ("spd", "初始速度")):
            conn.execute(
                "INSERT OR REPLACE INTO hero_attr(hero_id,attr_code,init_value,growth_value,data_status)"
                " VALUES(?,?,?,NULL,?)",
                (hid, code, num(r.get(col)),
                 "ok" if num(r.get(col)) is not None else "missing_source"),
            )
        stats["inserted"] += 1
    dbmod_record_source(conn, path, len(rows), len(header))
    return stats


def ingest_combos(conn) -> dict:
    """从 hero 表反推 combo 表（成员武将文本里有名字，无需再解析 CSV）。"""
    rows = conn.execute(
        "SELECT combo_name, combo_members, combo_bonus, combo_cond FROM hero "
        "WHERE combo_name IS NOT NULL AND combo_name != '未获取' GROUP BY combo_name"
    ).fetchall()
    conn.execute("DELETE FROM combo")
    names = {r["name"]: r["hero_id"] for r in conn.execute("SELECT name, hero_id FROM hero")}
    inserted = 0
    for i, r in enumerate(rows, 1):
        cid = f"C{i:03d}"
        members_txt = clean(r["combo_members"])
        ids = [names[n] for n in names if n and n in members_txt]
        conn.execute(
            "INSERT OR REPLACE INTO combo(combo_id,name,hero_ids_json,bonus_text,activate_cond,data_status)"
            " VALUES(?,?,?,?,?,?)",
            (cid, clean(r["combo_name"]), json.dumps(ids, ensure_ascii=False),
             clean(r["combo_bonus"]) or None, clean(r["combo_cond"]) or None, "ok"),
        )
        inserted += 1
    return {"combos": inserted}


# ------------------------------------------------------------------ 证据 / 战报


def build_name_index(conn) -> tuple[dict[str, str], dict[str, str]]:
    heroes = {r["name"]: r["hero_id"] for r in conn.execute("SELECT name, hero_id FROM hero")}
    skills = {r["name"]: r["skill_id"] for r in conn.execute("SELECT name, skill_id FROM skill")}
    return heroes, skills


def match_names(text: str, table: dict[str, str]) -> list[str]:
    hits = [vid for name, vid in table.items() if name and name in text]
    return sorted(set(hits))


def extract_team_ids(teams: Any, hero_name_to_id: dict[str, str], skill_name_to_id: dict[str, str]) -> tuple[list[str], list[str]]:
    """从结构化 teams 里取武将/战法 ID（结构不固定，宽容处理）。"""
    if not teams:
        return [], []
    items = teams if isinstance(teams, list) else [teams]
    hero_ids: list[str] = []
    skill_ids: list[str] = []
    for t in items:
        if not isinstance(t, dict):
            continue
        heroes = t.get("heroes") or t.get("heroList") or t.get("members") or t.get("heros") or []
        if not isinstance(heroes, list):
            continue
        for h in heroes:
            if not isinstance(h, dict):
                if isinstance(h, str):
                    hero_ids.append(hero_name_to_id.get(clean(h), clean(h)))
                continue
            hid = str(pick(h, "heroId", "id", "hero_id", "wid", default="") or "")
            if hid:
                hero_ids.append(hero_name_to_id.get(clean(hid), hid))
            sks = h.get("skills") or h.get("skillList") or h.get("skillIds") or h.get("skill_ids") or []
            if not isinstance(sks, list):
                sks = [sks]
            for s in sks:
                if isinstance(s, dict):
                    sid = str(pick(s, "skillId", "id", "skill_id", default="") or "")
                else:
                    sid = clean(s)
                if sid:
                    skill_ids.append(skill_name_to_id.get(clean(sid), sid))
    return sorted(set(hero_ids)), sorted(set(skill_ids))


def ingest_evidence(conn, path: Path) -> dict:
    if not path.exists():
        return {"skipped": str(path)}
    data = json.loads(path.read_text(encoding="utf-8"))
    recs = unwrap_records(data)
    heroes, skills = build_name_index(conn)
    conn.execute("DELETE FROM evidence_doc")
    n = 0
    for rec in recs:
        mid = str(pick(rec, "messageId", "message_id", "msgId", "id", "tid") or "")
        if not mid:
            continue
        title = clean(pick(rec, "title", "subject", "name", default=""))
        content = clean(pick(rec, "content", "text", "body", "textBody", default=""))
        text_body = clean(pick(rec, "textBody", default=""))
        body = "\n".join(x for x in (content, text_body) if x)
        if not body.strip():
            body = title
        rec_type = clean(pick(rec, "type", default="")).lower()
        kind = "analysis" if ("解析" in title or "攻略" in title or "评价" in title or rec_type == "analysis") else "lineup"
        likes = int(num(pick(rec, "likes", "like", "likeCount", "up", default=0)) or 0)
        team_hero_ids, team_skill_ids = extract_team_ids(rec.get("teams"), heroes, skills)
        if not team_hero_ids:
            team_hero_ids = match_names(body + title, heroes)
        if not team_skill_ids:
            team_skill_ids = match_names(body + title, skills)
        conn.execute(
            "INSERT OR REPLACE INTO evidence_doc(message_id,kind,url,author,ts,title,body,likes,"
            "hero_ids_json,skill_ids_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, kind, clean(pick(rec, "url", "link", default="")) or None,
             clean(pick(rec, "author", "userName", "nick", default="")) or None,
             str(pick(rec, "ts", "time", "createdAt", "publishTime", default="")) or None,
             title or None, body[:6000], likes,
             json.dumps(team_hero_ids, ensure_ascii=False),
             json.dumps(team_skill_ids, ensure_ascii=False)),
        )
        n += 1
    dbmod_record_source(conn, path, len(recs), 0)
    return {"docs": n, "records": len(recs)}


def ingest_battles(conn, path: Path) -> dict:
    if not path.exists():
        return {"skipped": str(path)}
    data = json.loads(path.read_text(encoding="utf-8"))
    recs = unwrap_records(data)
    heroes = {r["name"]: r["hero_id"] for r in conn.execute("SELECT name, hero_id FROM hero")}
    agg: dict[tuple[str, ...], dict] = {}
    # 丢弃/降级计数。旧版 `len(ids) != 3` 直接静默 return：源 3433 条、库里只有 3239 场，
    # 对不上账也没人知道；未知 result 码同样被静默当平局。两者都要露出来。
    dropped = {"not3": 0, "no_code": 0}
    unknown_codes: dict[str, int] = {}
    accepted = 0

    def add(ids: tuple[str, ...], result: str, pve: bool) -> None:
        nonlocal accepted
        if len(ids) != 3:
            dropped["not3"] += 1
            return
        accepted += 1
        key = tuple(sorted(ids))
        d = agg.setdefault(key, {"games": 0, "wins": 0, "losses": 0, "ties": 0, "pve": 0})
        d["games"] += 1
        if pve:
            d["pve"] += 1
        if result == "win":
            d["wins"] += 1
        elif result == "lose":
            d["losses"] += 1
        else:
            d["ties"] += 1

    for rec in recs:
        own = pick(rec, "ownHeroes", "heroes", "myHeroes", "team", default=[]) or []
        if not isinstance(own, list):
            continue
        ids: list[str] = []
        for h in own:
            if isinstance(h, dict):
                hid = str(pick(h, "id", "heroId", "hero_id", default=""))
                if not hid:
                    nm = clean(pick(h, "name", "heroName", default=""))
                    hid = heroes.get(nm, "")
            else:
                hid = heroes.get(clean(h), "")
            if hid:
                ids.append(hid)
        outcome = clean(pick(rec, "outcome", default="")).lower()
        if outcome in ("win", "lose", "tie"):
            result = outcome
        elif outcome in ("mutual", "draw"):
            result = "tie"
        else:
            # 回退：result 原始码 + 攻守视角（meta.outcomeRule），但优先用 outcome
            code = num(pick(rec, "result", default=None))
            is_attack = num(pick(rec, "isAttack", default=1))
            WIN = {1, 2, 3, 4, 5, 10, 11, 12, 15, 17, 18, 19, 20, 21, 22, 23, 25, 27, 29, 30, 31}
            LOSE = {0, 14, 16, 26, 28, 32}
            TIE = {6, 7, 8, 13, 24}
            if code is None or int(code) == 9:
                dropped["no_code"] += 1
                continue
            c = int(code)
            if c in WIN:
                result = "win"
            elif c in LOSE:
                result = "lose"
            elif c in TIE:
                result = "tie"
            else:
                # 未知码：不再静默当平局，按平局计入场次但登记一条 data_issue 待确认语义
                unknown_codes[str(c)] = unknown_codes.get(str(c), 0) + 1
                result = "tie"
            if is_attack is not None and int(is_attack) == 0:
                result = "lose" if result == "win" else "win" if result == "lose" else "tie"
        pve_flag = pick(rec, "pve", "isPve", default=None)
        pve = _as_bool(pve_flag) if pve_flag is not None else \
            clean(pick(rec, "type", "battleType", "mode", default="")).lower() == "pve"
        add(tuple(ids), result, pve)

    conn.execute("DELETE FROM battle_stat")
    conn.execute("DELETE FROM data_issue WHERE table_name='battle_stat'")
    for code, cnt in sorted(unknown_codes.items()):
        conn.execute(
            "INSERT INTO data_issue(table_name,row_key,field,data_status,reason) VALUES(?,?,?,?,?)",
            ("battle_stat", code, "result", "unconfirmed",
             f"未知 result 码 {code}：{cnt} 条战报暂按平局计入场次，语义待确认"),
        )
    n = 0
    for key, d in agg.items():
        games = d["games"]
        win_rate = (d["wins"] / games) if games else None
        conn.execute(
            "INSERT OR REPLACE INTO battle_stat(trio_key,hero_ids_json,games,wins,losses,pve_games,"
            "sample_flag,win_rate) VALUES(?,?,?,?,?,?,?,?)",
            ("|".join(key), json.dumps(list(key), ensure_ascii=False), games, d["wins"], d["losses"],
             d["pve"], "ok" if games >= 5 else "low_sample", win_rate),
        )
        n += 1
    dbmod_record_source(conn, path, len(recs), 0)
    return {"records": len(recs), "accepted": accepted, "dropped": dropped,
            "trios": n, "unknown_codes": unknown_codes}


# ------------------------------------------------------------------ 源数据登记


def dbmod_record_source(conn, path: Path, rows: int, cols: int) -> None:
    conn.execute("DELETE FROM data_source WHERE file_path=?", (str(path),))
    conn.execute(
        "INSERT INTO data_source(file_path,sha256,rows,cols) VALUES(?,?,?,?)",
        (str(path), sha256_of(path), rows, cols or None),
    )


def record_issues(conn, hero_stats: dict, skill_stats: dict) -> None:
    conn.execute("DELETE FROM data_issue")
    for name, label, raw in hero_stats.get("unmatched", [])[:200]:
        conn.execute(
            "INSERT INTO data_issue(table_name,row_key,field,data_status,reason) VALUES(?,?,?,?,?)",
            ("hero", name, label, "unconfirmed", f"战法名称无法匹配：{raw[:120]}"),
        )


# ------------------------------------------------------------------ 入口


def run(rebuild: bool = False) -> None:
    db_path = dbmod.DB_PATH
    if rebuild and db_path.exists():
        # WAL 模式下还有 -wal / -shm 边车文件，只删主库会留下陈旧 WAL
        for p in (db_path, db_path.with_name(db_path.name + "-wal"),
                  db_path.with_name(db_path.name + "-shm")):
            if p.exists():
                p.unlink()
    conn = dbmod.connect()
    # rebuild 时主库已删、user_version 归零，这里必须强制重跑建表 + 种子
    dbmod.init_db(conn, force=rebuild)

    if not SKILL_CSV.exists() or not HERO_CSV.exists():
        raise SystemExit(f"缺少数据文件：{SKILL_CSV} / {HERO_CSV}")

    print("[1/5] 导出战法 …")
    s1 = ingest_skills(conn, SKILL_CSV)
    print(f"      rows={s1['rows']} inserted={s1['inserted']} dup={s1['dup']} "
          f"无有效距离={s1['missing_range']} 无发动几率={s1['missing_proc']}")
    conn.commit()

    print("[2/5] 导入武将 …")
    resolver = SkillNameResolver([dict(r) for r in conn.execute("SELECT skill_id, name FROM skill")])
    s2 = ingest_heroes(conn, HERO_CSV, resolver)
    print(f"      rows={s2['rows']} inserted={s2['inserted']} 同名多形态={s2['dup_name']} "
          f"战法名未匹配={len(s2['unmatched'])} 可拆2缺失={s2['missing_d2']} 组合缺失={s2['missing_combo']}")
    for item in s2["unmatched"][:10]:
        print("      未匹配:", item)
    conn.commit()

    print("[3/5] 生成组合表 …")
    s3 = ingest_combos(conn)
    print(f"      combos={s3['combos']}")
    conn.commit()

    print("[4/5] 导入社区证据 …")
    s4 = ingest_evidence(conn, WAR_JSON)
    print(f"      {s4}")
    conn.commit()

    # record_issues 会先 DELETE FROM data_issue，所以战报的 data_issue 必须在它之后写，
    # 否则会被这里整表清空（顺序反了就丢问题记录）。
    record_issues(conn, s2, s1)
    print("[5/5] 导入战报统计 …")
    s5 = ingest_battles(conn, BATTLE_JSON)
    if "skipped" in s5:
        print(f"      {s5}")
    else:
        print(f"      records={s5['records']} 入库={s5['accepted']} "
              f"跳过非三人={s5['dropped']['not3']} 跳过无结果码={s5['dropped']['no_code']} "
              f"三人组={s5['trios']}")
        if s5["unknown_codes"]:
            print(f"      未知 result 码（按平局计入，已登记 data_issue）：{s5['unknown_codes']}")
    conn.commit()

    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("hero", "hero_attr", "skill", "combo", "evidence_doc", "battle_stat")}
    print("完成：", counts, f"→ {dbmod.DB_PATH}")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="删库重建")
    args = ap.parse_args()
    run(rebuild=args.rebuild)
