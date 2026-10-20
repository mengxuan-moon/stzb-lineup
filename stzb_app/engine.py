"""推荐引擎：变体生成 → 校验打分 → 排序 → 理由/克制/替补 → 落库。

流程（与伪代码一致）：
    种子队伍 → 邻域变体(换1将/换1战法) → 指纹去重 → 合法性+六维+适用性 → 排序取 TopN
    → 每条结果补 规则理由 / 战报数字 / 社区原文（带出处） / 克制关系 / 替补方案 → 写库
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from . import core
from . import db as dbmod

POSITIONS = core.POSITIONS


# ------------------------------------------------------------------ 数据装载


def load_index(conn: sqlite3.Connection) -> core.Index:
    heroes: list[dict] = []
    for r in conn.execute("SELECT * FROM hero"):
        h = dict(r)
        for code in ("atk", "guile", "def", "spd", "siege"):
            row = conn.execute(
                "SELECT init_value FROM hero_attr WHERE hero_id=? AND attr_code=?", (h["hero_id"], code)
            ).fetchone()
            h[code] = row["init_value"] if row else None
        heroes.append(h)
    skills = [dict(r) for r in conn.execute("SELECT * FROM skill")]
    combos = []
    for r in conn.execute("SELECT * FROM combo"):
        c = dict(r)
        ids = json.loads(c.get("hero_ids_json") or "[]")
        names = []
        for hid in ids:
            row = conn.execute("SELECT name FROM hero WHERE hero_id=?", (hid,)).fetchone()
            if row:
                names.append(row["name"])
        c["combo_members"] = " ".join(names)
        combos.append(c)
    return core.build_index(heroes, skills, combos)


def load_weights(conn: sqlite3.Connection, target: str) -> tuple[dict, dict, tuple[float, float], dict]:
    prof = conn.execute("SELECT * FROM scoring_profile WHERE is_active=1").fetchone()
    system: dict[str, float] = {}
    combat: dict[str, float] = {}
    if prof:
        for r in conn.execute(
            "SELECT scope,target_mode,dim_code,weight FROM score_weight WHERE profile_id=?", (prof["profile_id"],)
        ):
            if r["scope"] == "system":
                system[r["dim_code"]] = r["weight"]
            elif r["target_mode"] == target:
                combat[r["dim_code"]] = r["weight"]
        merge = (prof["merge_system"], prof["merge_applicability"])
    else:
        system, combat, merge = dict(dbmod.SYSTEM_WEIGHTS), dict(dbmod.COMBAT_WEIGHTS.get(target, {})), (0.6, 0.4)
    if not combat:
        combat = dict(dbmod.COMBAT_WEIGHTS.get(target, dbmod.COMBAT_WEIGHTS["打地"]))
    thresholds = dbmod.get_param(conn, "verdict_thresholds", {"ok": 75, "tune": 55}) or {"ok": 75, "tune": 55}
    return system, combat, merge, thresholds


def load_owned(conn: sqlite3.Connection, user_key: str = "local") -> tuple[list[str], list[str]]:
    hero_ids = [r["hero_id"] for r in conn.execute(
        "SELECT hero_id FROM user_hero WHERE user_key=? AND owned=1", (user_key,))]
    skill_ids = [r["skill_id"] for r in conn.execute(
        "SELECT skill_id FROM user_skill WHERE user_key=? AND owned=1", (user_key,))]
    return hero_ids, skill_ids


# ------------------------------------------------------------------ 队伍读写


def lineup_members(conn: sqlite3.Connection, lineup_id: int) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT position,hero_id,skill1_id,skill2_id,locked FROM lineup_member WHERE lineup_id=?", (lineup_id,)
    )]
    by_pos = {r["position"]: r for r in rows}
    return [by_pos.get(p) for p in POSITIONS]


def save_lineup(conn: sqlite3.Connection, name: str, members: list[dict],
                source: str = "user", parent_lineup_id: int | None = None, swap_note: str = "",
                target_mode: str = "", note: str = "", fingerprint: str | None = None,
                user_key: str = "local", commit: bool = True) -> int:
    """写一支队伍并返回 lineup_id。

    列必须与占位符一一对应：旧版 VALUES 只有 6 个 ?，`note` 被写进了 swap_note、
    note 恒空、target_mode 恒 NULL（错位不会报错，只会静默写歪）。
    target_mode 未传时取 user_settings 里的当前目标，target_mode 不再留 NULL。
    commit=False 供 engine._persist 用：一次推荐要写 lineup / team_score / reason_item
    等多张表，中途 commit 会把半截结果留在库里，所以整批写完由调用方提交一次。
    """
    tm = target_mode or (dbmod.get_settings(conn, user_key).get("target_mode") or "打地")
    cur = conn.execute(
        "INSERT INTO lineup(user_key,name,source,parent_lineup_id,swap_note,target_mode,fingerprint,note)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (user_key, name, source, parent_lineup_id, swap_note, tm, fingerprint, note),
    )
    lid = cur.lastrowid
    for m in members:
        if not m:
            continue
        conn.execute(
            "INSERT INTO lineup_member(lineup_id,position,hero_id,skill1_id,skill2_id,locked)"
            " VALUES(?,?,?,?,?,?)",
            (lid, m["position"], m["hero_id"], m.get("skill1_id"), m.get("skill2_id"), int(m.get("locked") or 0)),
        )
    # 必须提交：Streamlit 每次交互都是一次独立 rerun，连接在脚本结束时靠引用计数关掉；
    # 不 commit 的话这次 INSERT 会被回滚，用户看到「点了保存但已保存列表还是空」。
    if commit:
        conn.commit()
    return lid


def list_lineups(conn: sqlite3.Connection, user_key: str = "local") -> list[dict]:
    """该用户的「手动保存」队伍列表（不含 variant / generated），按保存顺序。
    每项附 members（位置→武将→战法），供界面直接展示与重建。
    """
    rows = conn.execute(
        "SELECT lineup_id,name,created_at FROM lineup WHERE user_key=? AND source='user' ORDER BY lineup_id",
        (user_key,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["members"] = lineup_members(conn, d["lineup_id"])
        out.append(d)
    return out


def _hero_of(member: dict | None) -> dict:
    """取 member 的 hero；查不到（旧库漂移 / 武将已删）时返回空 dict，不让调用方炸。

    spec_to_members 用 index.hero_by_id.get()，历史 lineup 里若留着已不存在的 hero_id，
    member["hero"] 就是 None；旧代码直接 m["hero"][...] → TypeError → ④ 页整页白屏。
    """
    return ((member or {}).get("hero") or {})


def _hero_ids(members: list) -> list[str]:
    """member 列表 → hero_id 列表，跳过空位与失效武将（失效的没有 hero_id 可用）。"""
    return [h["hero_id"] for h in (_hero_of(m) for m in (members or [])) if h and h.get("hero_id")]


def members_to_spec(members: list[dict | None]) -> list[dict]:
    """core members（含 hero/skill dict）→ 纯 ID 规格，便于存库与去重。

    失效武将（hero 为 None）直接跳过：它的 hero_id 已经不在 hero 表里，
    硬写进 lineup_member 会撞外键，整次推荐就白屏了。
    """
    out = []
    for m in members:
        if not m:
            continue
        hero = _hero_of(m)
        if not hero:
            continue
        sks = [s["skill_id"] for s in (m.get("skills") or []) if s]
        out.append({"position": m["position"], "hero_id": hero["hero_id"],
                    "skills": sks})
    return out


def spec_to_members(spec: list[dict], index: core.Index) -> list[dict | None]:
    by_pos = {s["position"]: s for s in spec}
    out = []
    for pos in POSITIONS:
        s = by_pos.get(pos)
        if not s:
            out.append(None)
            continue
        sks_ids = s.get("skills")
        if sks_ids is None:  # 兼容 lineups 表 / cli 种子里的 skill1_id + skill2_id 形式
            sks_ids = [x for x in (s.get("skill1_id"), s.get("skill2_id")) if x]
        sks = [index.skill_by_id.get(x) for x in sks_ids]
        while len(sks) < 2:
            sks.append(None)
        out.append({"position": pos, "hero": index.hero_by_id.get(s["hero_id"]), "skills": sks[:2]})
    return out


# ------------------------------------------------------------------ 变体生成（P2/P3 已确认：换将不限兵种；大营可改）


def expand_variants(seed: list[dict], index: core.Index, owned_hero_ids: list[str],
                    owned_skill_ids: list[str], limit: int, cost_limit: float) -> list[dict]:
    if not seed or any(m is None or not m.get("hero") for m in seed):
        # 有失效武将（hero=None）时无法生成变体：新队伍里会留下同样的空位，
        # 指纹/校验都失去意义，直接返回空表（推荐主流程仍会把原队伍展示出来）。
        return []
    base_fp = core.fingerprint(seed)
    used_heroes = {m["hero"]["hero_id"] for m in seed if m}
    used_skills = {s["skill_id"] for m in seed if m for s in (m.get("skills") or []) if s}
    out: list[dict] = []
    seen: set[str] = {base_fp}
    # 两段预算：换将最多吃一半名额，剩下的留给换战法。
    # 旧版共用 len(out) >= limit：武将池一大，「换将」就把 20 个名额全占了，
    # 「换战法」一个都生成不出来（fast 档 per_lineup=20 时尤其明显）。
    hero_cap = max(1, limit // 2)

    def add_variant(new_members: list[dict], note: str) -> bool:
        if len(out) >= limit:
            return False
        fp = core.fingerprint(new_members)
        if fp in seen:
            return False
        total_cost = sum(core.num(m["hero"].get("cost")) or 0.0 for m in new_members if m and m.get("hero"))
        if total_cost > cost_limit + 1e-9:
            return False
        seen.add(fp)
        out.append({"members": new_members, "swap_note": note, "fingerprint": fp})
        return True

    # ① 换将：每个位置换成池内另一名武将（不锁定则允许换），最多 hero_cap 个
    hero_added = 0
    for i, m in enumerate(seed):
        if hero_added >= hero_cap:
            break
        if not m or m.get("locked"):
            continue
        for hid in owned_hero_ids:
            if hero_added >= hero_cap:
                break
            if hid in used_heroes:
                continue
            hero = index.hero_by_id.get(hid)
            if not hero:
                continue
            new = [dict(x) if x else None for x in seed]
            new[i] = {"position": m["position"], "hero": hero, "skills": list(m.get("skills") or [])}
            if add_variant(new, f"{m['position']}：{m['hero']['name']} → {hero['name']}"):
                hero_added += 1

    # ② 换战法：同位置同格子换成池内另一战法（兵种类型必须兼容），用剩余预算
    for i, m in enumerate(seed):
        if len(out) >= limit:
            break
        if not m:
            continue
        hero_troop = core.clean(m["hero"].get("troop"))
        for k in range(2):
            if len(out) >= limit:
                break
            old = (m.get("skills") or [None, None])[k]
            for sid in owned_skill_ids:
                if len(out) >= limit:
                    break
                if sid in used_skills:
                    continue
                sk = index.skill_by_id.get(sid)
                if not sk:
                    continue
                tt = core.clean(sk.get("troop_limit"))
                if tt and tt not in ("未获取", "--") and hero_troop not in tt:
                    continue
                new = [dict(x) if x else None for x in seed]
                sks = list(new[i].get("skills") or [None, None])
                while len(sks) < 2:
                    sks.append(None)
                sks[k] = sk
                new[i] = {"position": m["position"], "hero": m["hero"], "skills": sks}
                old_name = old["name"] if old else "空"
                add_variant(new, f"{m['position']}战法{k + 1}：{old_name} → {sk['name']}")
    return out


# ------------------------------------------------------------------ 证据检索（N2(b)，LIKE 多关键词）


SENT_SPLIT = "。！？!?\n"


def split_sentences(text: str) -> list[str]:
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in SENT_SPLIT:
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def best_excerpt(body: str, terms: list[str], max_chars: int = 120) -> tuple[str, list[str]]:
    sents = split_sentences(body)
    best, best_hits = "", []
    for i in range(len(sents)):
        window = ""
        hits: set[str] = set()
        for j in range(i, len(sents)):
            if len(window) + len(sents[j]) > max_chars:
                break
            window += sents[j]
            hits |= {t for t in terms if t in sents[j]}
            if len(hits) > len(best_hits) or (len(hits) == len(best_hits) and len(window) < len(best or "x" * 999)):
                best, best_hits = window.strip(), sorted(hits)
    return best, best_hits


def search_evidence(conn: sqlite3.Connection, terms: list[str], limit: int = 3) -> list[dict]:
    terms = [t for t in dict.fromkeys(terms) if t]
    if not terms:
        return []
    where = " OR ".join(["body LIKE ?"] * len(terms))
    params = [f"%{t}%" for t in terms]
    rows = conn.execute(
        f"SELECT message_id,title,body,likes,url,author,ts FROM evidence_doc WHERE {where} LIMIT 200", params
    ).fetchall()
    scored = []
    for r in rows:
        hits = [t for t in terms if t in (r["body"] or "")]
        if not hits:
            continue
        score = len(hits) * 10 + min(r["likes"] or 0, 50) * 0.1
        scored.append({"message_id": r["message_id"], "title": r["title"], "body": r["body"],
                       "likes": r["likes"] or 0, "url": r["url"], "author": r["author"],
                       "ts": r["ts"], "hits": hits, "score": score})
    scored.sort(key=lambda x: (-x["score"], -x["likes"]))
    max_chars = int(dbmod.get_param(conn, "evidence_excerpt_max_chars", 120) or 120)
    out = []
    for s in scored[:limit]:
        excerpt, hits = best_excerpt(s["body"] or "", terms, max_chars)
        if not excerpt or not hits:
            continue
        s["excerpt"], s["hits"] = excerpt, hits
        out.append(s)
    return out


# ------------------------------------------------------------------ 理由 / 克制 / 替补


def rule_reasons(ev: core.Evaluation, members: list, index: core.Index,
                 conn: sqlite3.Connection | None = None) -> list[dict]:
    out: list[dict] = []
    d = ev.dims
    syn = d["synergy"]
    if syn["camp_n"] == 3:
        out.append({"text": f"三人同属{syn['camp']}阵营，阵营协同加满（+{syn['camp_pts']}）"})
    elif syn["camp_n"] == 2:
        out.append({"text": f"两人同属{syn['camp']}阵营，阵营协同 +{syn['camp_pts']}"})
    if syn["troop_n"] >= 2:
        out.append({"text": f"{syn['troop_n']} 人同为{syn['troop']}兵，兵种一致（+{syn['troop_pts']}）"})
    for c in syn["combos"]:
        out.append({"text": f"激活武将组合「{c.get('name')}」：{c.get('bonus_text') or '加成数值待补'}（+{syn['combo_pts']}）"})
    fit = d["skillFit"]
    out.append({"text": f"战法兵种适配 {fit['matched']}/{fit['total']} 条，品质均值 {fit['q_avg']:.0f}"})
    for s in fit["low_quality"][:2]:
        out.append({"text": f"「{s['name']}」品质仅 {s.get('quality')}，是明显短板"})
    st = d["structure"]
    out.append({"text": f"战法结构：伤害 {st['damage']} 条 / 控制 {st['control']} 条 / 回复 {st['heal']} 条"
                         + (f"，主动战法平均发动率 {st['chance_avg']:.0f}%" if st.get("chance_avg") else "")})
    cost = d["cost"]
    out.append({"text": f"统率占用 {cost['total']:.1f}/{cost['limit']:.1f}（利用率 {cost['usage'] * 100:.0f}%）"})
    for note in d["distance"]["notes"][:3]:
        out.append({"text": note})

    # R-CONFLICT-01：冲突不是「不能用」，而是同类效果被覆盖 —— 照常计分，只作提示。
    for cf in st["conflicts"][:2]:
        out.append({"text": cf["text"] + "（已按 R-CONFLICT-01 扣结构分，不禁止使用）"})
    # 站位适性（distance 维的 position_fit 分量）：只点出最短板，避免理由变噪声。
    fit_rows = sorted((d["distance"].get("position_fit") or {}).get("per_member") or [],
                      key=lambda x: x.get("fit", 100))[:1]
    for f in fit_rows:
        if f.get("fit", 100) < 55:
            out.append({"text": f"「{f['hero']}」站{f['position']}的适性只有 {f['fit']:.0f}"
                                 f"（按该位置主属性折算：大营看攻击/谋略、中军看控制+速度、前锋看防御），是站位短板"})
    if conn is not None:
        camp_n, troop_n = syn.get("camp_n") or 0, syn.get("troop_n") or 0
        # camp_bonus_table 是「人数→标量」；troop_bonus_table 是「人数→{atk,spd}」，
        # 两种形状都要兼容，否则 float(dict) 会把整条推荐链打断。
        camp_tab = dbmod.get_param(conn, "camp_bonus_table", {}) or {}
        troop_tab = dbmod.get_param(conn, "troop_bonus_table", {}) or {}

        def _num(v) -> float:
            if isinstance(v, dict):
                return float(v.get("atk") or v.get("all") or 0)
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0

        cb = _num(camp_tab.get(str(camp_n))) if camp_n else 0.0
        if cb:
            out.append({"text": f"阵营加成：{syn.get('camp')}×{camp_n} 人，按点将台 1 级全属性 +{cb * 100:.1f}%"
                                 f"（协同分按 +{syn.get('camp_pts')} 计）"})
        tb = _num(troop_tab.get(str(troop_n))) if troop_n else 0.0
        if tb:
            out.append({"text": f"兵种加成：{syn.get('troop')}×{troop_n} 人，攻击/速度各 +{tb * 100:.0f}%"
                                 f"（协同分按 +{syn.get('troop_pts')} 计）"})
    # 攻城战法数在适用性结果（ev.combat）里，不在 dims 六维中。
    if (ev.combat or {}).get("siege_skill_n"):
        out.append({"text": f"队内有 {ev.combat['siege_skill_n']} 条攻城向战法（命中「攻城/耐久/器械」类描述），"
                             f"在攻城目标下计入攻城分量"})
    return out


def battle_reason(conn: sqlite3.Connection, trio_ids: list[str]) -> dict | None:
    key = "|".join(sorted(trio_ids))
    row = conn.execute("SELECT * FROM battle_stat WHERE trio_key=?", (key,)).fetchone()
    if not row or not row["games"]:
        return None
    wr = (row["win_rate"] or 0) * 100
    tag = "样本偏少" if row["sample_flag"] == "low_sample" else "样本充足"
    # 说清分母：胜率 = 胜/全部场次，而「全部场次」里混了 PvE 和平局。
    # 不写出来玩家会以为是纯 PvP 胜率（PvE 与平局都计入场次，见 README 已知限制）。
    pve = int(row["pve_games"] or 0)
    pvp = max(0, int(row["games"]) - pve)
    ties = max(0, int(row["games"]) - int(row["wins"] or 0) - int(row["losses"] or 0))
    return {"text": f"战报库同组 {row['games']} 场（PvP {pvp} / PvE {pve}，平局 {ties}），胜率 {wr:.1f}%"
                    f"（胜 {row['wins']} / 负 {row['losses']}，{tag}）",
            "basis": {"games": row["games"], "wins": row["wins"], "losses": row["losses"],
                      "pve_games": pve, "ties": ties, "sample_flag": row["sample_flag"]}}


def troop_cycle(conn: sqlite3.Connection) -> list[str]:
    chain = dbmod.get_param(conn, "troop_counter_chain", ["骑", "步", "弓", "骑"]) or ["骑", "步", "弓", "骑"]
    # 种子是闭环写法 ["骑","步","弓","骑"]，先按出现顺序去重成 ["骑","步","弓"]；
    # 不去重的话 chain.index("骑") 恒为 0，下面 `idx-1` 越界 → 骑兵队永远没有「被克制」。
    return list(dict.fromkeys(chain))


def counter_analysis(conn: sqlite3.Connection, members: list) -> list[dict]:
    chain = troop_cycle(conn)
    # 只看真的解析出武将的 member：失效武将（hero=None）没有兵种/战法可分析
    present = [m for m in members if m and m.get("hero")]
    rows: list[dict] = []
    if chain and present:
        counts: dict[str, int] = {}
        for m in present:
            t = core.clean(m["hero"].get("troop"))
            counts[t] = counts.get(t, 0) + 1
        main_troop = max(counts, key=counts.get)
        if main_troop in chain:
            idx = chain.index(main_troop)
            n = len(chain)
            # 环形取模：最后一环能绕回第一环，第一环也能绕回最后一环（骑兵被弓克）。
            beats = chain[(idx + 1) % n] if n else None
            loses_to = chain[(idx - 1) % n] if n else None
            if n == 1:  # 链只有一种兵种时无克制可言，避免自克
                beats = loses_to = None
            if beats:
                rows.append({"counter_type": "troop", "target_desc": f"{beats}兵队伍",
                             "direction": "we_counter", "confidence": 0.8,
                             "basis": {"rule": "troop_counter_chain", "chain": chain, "main_troop": main_troop}})
            if loses_to:
                rows.append({"counter_type": "troop", "target_desc": f"{loses_to}兵队伍",
                             "direction": "they_counter", "confidence": 0.8,
                             "basis": {"rule": "troop_counter_chain", "chain": chain, "main_troop": main_troop}})
    ctrl: dict[str, int] = {}
    for m in present:
        for s in (m.get("skills") or []):
            if not s:
                continue
            for kw in core.KW_CONTROL:
                if kw in core.clean(s.get("desc_text")):
                    ctrl[kw] = ctrl.get(kw, 0) + 1
    for kw, n in sorted(ctrl.items(), key=lambda kv: -kv[1])[:3]:
        rows.append({"counter_type": "control", "target_desc": kw,
                     "direction": "we_counter", "confidence": 0.6,
                     "basis": {"rule": "control_keywords", "keyword": kw, "skills": n}})
    return rows


def build_substitutes(conn: sqlite3.Connection, base_members: list, base_total: float, index: core.Index,
                      owned_hero_ids: list[str], owned_skill_ids: list[str], limit: int = 3,
                      user_key: str = "local") -> list[dict]:
    out: list[dict] = []
    seen_fp = {core.fingerprint(base_members)}
    cost_limit = float(dbmod.get_settings(conn, user_key).get("cost_limit") or 10.0)
    ev_params = _eval_params(conn, user_key)
    # limit=60 交给 expand_variants 自己劈成「换将 ≤30 / 换战法用剩余」两段，
    # 所以替补里换将、换战法两类都会出现，不会全被换将占满。
    all_scored: list[dict] = []
    for v in expand_variants(base_members, index, owned_hero_ids, owned_skill_ids, limit=60, cost_limit=cost_limit):
        fp = v["fingerprint"]
        if fp in seen_fp:
            continue
        ev = core.evaluate(v["members"], index, ev_params["target"], cost_limit,
                           ev_params["system"], ev_params["combat"], ev_params["merge"],
                           ev_params["thresholds"], params=ev_params["rule"])
        delta = ev.total - base_total
        kind = "swap_hero" if "→" in v["swap_note"] and "战法" not in v["swap_note"] else "swap_skill"
        spec = members_to_spec(v["members"])
        changed = _changed(kind, base_members, v["members"])
        all_scored.append({"kind": kind, "from_id": changed[0], "to_id": changed[1], "delta_score": round(delta, 2),
                           "reason": f"{v['swap_note']}（综合 {base_total:.1f} → {ev.total:.1f}）",
                           "probe": spec})
    all_scored.sort(key=lambda x: -x["delta_score"])
    out = [x for x in all_scored if x["delta_score"] > 0]
    picked = out[:limit]
    # top-N 常被同一种 kind 占满（换战法的组合数远多于换将），替补区就只剩一类方案。
    # 规则：取前 N 后若全是同一 kind，用另一 kind 里 delta 最高的一条替换末位，
    # 并在理由末尾标注「替补补齐」。优先从「比原队更好」的候选里找；一个都没有时
    # 才回退到 all_scored 里该类最高者（delta 可能 ≤0，理由里会写明），
    # 保证替补区两类都有可看的方案。不动任何分值与排序口径。
    kinds = {x["kind"] for x in picked}
    if picked and len(kinds) == 1:
        have = next(iter(kinds))
        other_kind = "swap_skill" if have == "swap_hero" else "swap_hero"
        alt = next((x for x in out[limit:] if x["kind"] == other_kind), None)
        note_extra = "（替补补齐：本轮只有同一类方案，用另一类里 delta 最高者替换末位）"
        if alt is None:
            alt = next((x for x in all_scored if x["kind"] == other_kind), None)
            note_extra = "（替补补齐：本轮只有同一类方案且另一类没有更优解，用另一类里 delta 最高者替换末位）"
        if alt:
            alt = dict(alt)
            alt["reason"] = f"{alt['reason']}{note_extra}"
            picked = picked[:-1] + [alt]
    return picked


def _changed(kind: str, base: list, new: list) -> tuple[str | None, str | None]:
    if kind == "swap_hero":
        for a, b in zip(base, new):
            if a and b and a["hero"]["hero_id"] != b["hero"]["hero_id"]:
                return a["hero"]["hero_id"], b["hero"]["hero_id"]
    else:
        for a, b in zip(base, new):
            if not a or not b:
                continue
            for sa, sb in zip(a.get("skills") or [], b.get("skills") or []):
                if sa and sb and sa["skill_id"] != sb["skill_id"]:
                    return sa["skill_id"], sb["skill_id"]
    return None, None


# 规则参数的默认值必须与 core.py 里的 DEFAULT_* 常量一致：
# rule_param 表缺行时（老库/手删）不能让评分静默改用另一套数值。
RULE_PARAM_DEFAULTS = {
    "synergy_points": core.DEFAULT_SYNERGY_POINTS,
    "distance_split": core.DEFAULT_DISTANCE_SPLIT,
    "conflict_penalty": core.DEFAULT_CONFLICT_PENALTY,
    "siege_skill_keywords": core.DEFAULT_SIEGE_SKILL_KEYWORDS,
}


def load_rule_params(conn: sqlite3.Connection) -> dict:
    """把参与评分的 rule_param 读成 dict，交给 core.evaluate(params=...)。

    只有这几个键会进评分；相克减伤/阵营兵种加成是「文案用」参数，由
    rule_reasons / counter_analysis 各自读，不进分数（敌方兵种未知，无法算期望）。
    """
    return {k: dbmod.get_param(conn, k, default) for k, default in RULE_PARAM_DEFAULTS.items()}


def _eval_params(conn: sqlite3.Connection, user_key: str = "local") -> dict:
    settings = dbmod.get_settings(conn, user_key)
    target = settings.get("target_mode") or "打地"
    system, combat, merge, thresholds = load_weights(conn, target)
    return {"target": target, "system": system, "combat": combat, "merge": merge, "thresholds": thresholds,
            "cost_limit": float(settings.get("cost_limit") or 10.0), "rule": load_rule_params(conn)}


# ------------------------------------------------------------------ 主流程


def recommend(conn: sqlite3.Connection, seeds: list[dict], user_key: str = "local", persist: bool = True) -> dict:
    """seeds: [{"name": str, "members": [{position,hero_id,skill1_id,skill2_id}, ...]}]"""
    init = _eval_params(conn, user_key)
    settings = dbmod.get_settings(conn, user_key)
    target, cost_limit = init["target"], init["cost_limit"]
    top_n = int(settings.get("top_n") or 10)
    budget_key = "search_budget_deep" if settings.get("budget_profile") == "deep" else "search_budget_fast"
    budget = dbmod.get_param(conn, budget_key, {"per_lineup": 20, "global_cap": 300})

    index = load_index(conn)
    owned_hero_ids, owned_skill_ids = load_owned(conn, user_key)
    # 没勾持仓时旧版静默回落全库，用户会以为算的是自己的卡池。
    # 这里记录回落事实，由 app 显式提示（数字由 app 从索引里取，不在这里塞）。
    hero_fallback = not owned_hero_ids
    skill_fallback = not owned_skill_ids
    if hero_fallback:
        owned_hero_ids = [h["hero_id"] for h in index.heroes]
    if skill_fallback:
        owned_skill_ids = [s["skill_id"] for s in index.skill_by_id.values()]

    run = conn.execute(
        "INSERT INTO eval_run(user_key,profile_id,target_mode,cost_limit,budget_profile,params_snapshot_json,engine_version)"
        " VALUES(?,'default',?,?,?,?,?)",
        (user_key, target, cost_limit, settings.get("budget_profile") or "fast",
         json.dumps({"system": init["system"], "combat": init["combat"], "merge": init["merge"],
                     "thresholds": init["thresholds"], "budget": budget, "rule": init["rule"]},
                    ensure_ascii=False),
         dbmod.ENGINE_VERSION),
    )
    run_id = run.lastrowid

    candidates: list[dict] = []
    truncated = False
    for seed in seeds:
        members = spec_to_members(seed["members"], index)
        ev = core.evaluate(members, index, target, cost_limit, init["system"], init["combat"],
                           init["merge"], init["thresholds"], params=init["rule"])
        candidates.append({"members": members, "source": seed.get("source", "manual"),
                           "parent": seed.get("name", ""), "swap_note": "", "fingerprint": core.fingerprint(members),
                           "eval": ev})
        for v in expand_variants(members, index, owned_hero_ids, owned_skill_ids,
                                 int(budget.get("per_lineup", 20)), cost_limit):
            ev2 = core.evaluate(v["members"], index, target, cost_limit, init["system"], init["combat"],
                                init["merge"], init["thresholds"], params=init["rule"])
            candidates.append({"members": v["members"], "source": "variant", "parent": seed.get("name", ""),
                               "swap_note": v["swap_note"], "fingerprint": v["fingerprint"], "eval": ev2})
        # 全局预算按 global_cap **真正截断**。旧版截成 global_cap*2，fast 档实际允许
        # 600 个候选，和 rule_param 里「全局上限 300」的字面约定不符（deep 档 6000）。
        # 截断必须告诉调用方：UI 上少了候选却没有任何提示，用户会以为「这就是全部」。
        cap = int(budget.get("global_cap", 300))
        if len(candidates) > cap:
            candidates = candidates[:cap]
            truncated = True

    # 去重：同指纹保留分数最高者
    best: dict[str, dict] = {}
    for c in candidates:
        cur = best.get(c["fingerprint"])
        if cur is None or c["eval"].total > cur["eval"].total:
            best[c["fingerprint"]] = c

    def tiebreak(c: dict) -> tuple:
        trio = _hero_ids(c["members"])
        row = conn.execute("SELECT games,win_rate FROM battle_stat WHERE trio_key=?",
                           ("|".join(sorted(trio)),)).fetchone()
        games = row["games"] if row else 0
        return (-round(c["eval"].total, 1), -games)

    ranked = sorted(best.values(), key=tiebreak)[:top_n]

    results = []
    for rank, c in enumerate(ranked, 1):
        ev = c["eval"]
        members = c["members"]
        trio_ids = _hero_ids(members)
        reasons: list[dict] = []
        for r in rule_reasons(ev, members, index, conn):
            reasons.append({"kind": "rule", **r})
        br = battle_reason(conn, trio_ids)
        if br:
            reasons.append({"kind": "battle", **br})
        # 检索词取「有效武将名 + 战法名」：失效武将没有名字，跳过即可
        terms = [h["name"] for h in (_hero_of(m) for m in members) if h and h.get("name")] \
            + [s["name"] for m in members if m for s in (m.get("skills") or []) if s]
        ev_hits = search_evidence(conn, terms, limit=2)
        for h in ev_hits:
            reasons.append({"kind": "community",
                            "text": f"社区同类阵容（{h['author'] or '匿名'}，赞 {h['likes']}）：「{h['excerpt']}」",
                            # score 是检索得分（命中词数 × 10 + 点赞上限 50 × 0.1），
                            # 旧版落库写的是 len(hits)（命中数），把「命中几个词」当成打分了。
                            "basis": {"message_id": h["message_id"], "url": h["url"], "hits": h["hits"],
                                      "title": h["title"], "score": h["score"]},
                            "evidence_message_id": h["message_id"]})

        counters = counter_analysis(conn, members)
        subs = build_substitutes(conn, members, ev.total, index, owned_hero_ids, owned_skill_ids, user_key=user_key)
        trio_row = conn.execute("SELECT games,win_rate,sample_flag FROM battle_stat WHERE trio_key=?",
                                ("|".join(sorted(trio_ids)),)).fetchone()
        findings = ([{**e, "level": "error"} for e in ev.validation.errors]
                    + [{**w, "level": "warn"} for w in ev.validation.warnings]
                    + [{**i, "level": "info"} for i in ev.validation.infos])

        results.append({
            "rank": rank, "fingerprint": c["fingerprint"], "source": c["source"], "parent": c["parent"],
            "swap_note": c["swap_note"], "total": ev.total, "system": ev.system_score,
            "applicability": ev.combat["total"], "legality": ev.legality, "verdict": ev.verdict,
            "verdict_reason": ev.verdict_reason, "members": members, "spec": members_to_spec(members),
            "reasons": reasons, "counters": counters, "substitutes": subs,
            "findings": findings,
            "battle": dict(trio_row) if trio_row else None,
            "dim_scores": {k: v["score"] for k, v in ev.dims.items()},
        })

    if persist:
        _persist(conn, user_key, run_id, results, index)

    conn.commit()
    return {"run_id": run_id, "target": target, "results": results,
            "candidates": len(candidates), "budget": budget,
            "truncated": truncated,
            "pool_fallback": {"heroes": hero_fallback, "skills": skill_fallback}}


def _persist(conn: sqlite3.Connection, user_key: str, run_id: int, results: list[dict], index: core.Index) -> None:
    for r in results:
        name = f"#{r['rank']} {r['total']:.1f}分"
        members = [{"position": s["position"], "hero_id": s["hero_id"],
                    "skill1_id": s["skills"][0] if len(s["skills"]) > 0 else None,
                    "skill2_id": s["skills"][1] if len(s["skills"]) > 1 else None}
                   for s in r["spec"]]
        lid = None
        if r.get("fingerprint"):
            row = conn.execute("SELECT lineup_id FROM lineup WHERE user_key=? AND fingerprint=?",
                               (user_key, r["fingerprint"])).fetchone()
            if row:
                lid = row["lineup_id"]
        if lid is None:
            lid = save_lineup(conn, name, members, source=r["source"], swap_note=r["swap_note"],
                              fingerprint=r.get("fingerprint"), user_key=user_key, commit=False)
        conn.execute(
            "INSERT OR REPLACE INTO team_score(run_id,lineup_id,rank,total_score,system_score,"
            "applicability_score,legality,dim_scores_json,verdict,fingerprint)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, lid, r["rank"], r["total"], r["system"], r["applicability"], r["legality"],
             json.dumps(r["dim_scores"], ensure_ascii=False), r["verdict"], r["fingerprint"]),
        )
        for f in r["findings"]:
            conn.execute("INSERT INTO finding(run_id,lineup_id,rule_id,level,message) VALUES(?,?,?,?,?)",
                         (run_id, lid, f.get("rule"), f.get("level", "info"), f.get("text")))
        for reason in r["reasons"]:
            conn.execute("INSERT INTO reason_item(run_id,lineup_id,kind,text,basis_json,evidence_message_id)"
                         " VALUES(?,?,?,?,?,?)",
                         (run_id, lid, reason["kind"], reason["text"],
                          json.dumps(reason.get("basis"), ensure_ascii=False), reason.get("evidence_message_id")))
        for h in [x for x in r["reasons"] if x["kind"] == "community"]:
            conn.execute("INSERT INTO evidence_hit(run_id,lineup_id,message_id,matched_terms,score,excerpt)"
                         " VALUES(?,?,?,?,?,?)",
                         (run_id, lid, h["evidence_message_id"],
                          json.dumps(h["basis"].get("hits"), ensure_ascii=False),
                          float(h["basis"].get("score") or 0.0), h["text"]))
        for c in r["counters"]:
            conn.execute("INSERT INTO counter_relation(run_id,lineup_id,counter_type,target_desc,direction,"
                         "confidence,basis_json) VALUES(?,?,?,?,?,?,?)",
                         (run_id, lid, c["counter_type"], c["target_desc"], c["direction"], c.get("confidence"),
                          json.dumps(c.get("basis"), ensure_ascii=False)))
        for s in r["substitutes"]:
            conn.execute("INSERT INTO substitute(run_id,lineup_id,kind,from_id,to_id,delta_score,reason)"
                         " VALUES(?,?,?,?,?,?,?)",
                         (run_id, lid, s["kind"], s["from_id"], s["to_id"], s["delta_score"], s["reason"]))
    # 整批写完只提交一次：中途 commit 时若后面某条写失败，库里会留下
    # 「有 lineup 却没有 team_score/reason_item」的半截结果，界面看着像数据错乱。
    conn.commit()
