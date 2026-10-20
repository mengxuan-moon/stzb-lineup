"""校验与评分核心（旧版 stzb_team_eval/src/core.js 的 Python 移植 + Q4-4 距离几何）。

对外只暴露少量函数：
    build_index(heroes, skills) -> Index
    validate(members, index, cost_limit) -> Validation
    evaluate(members, index, target, cost_limit) -> Evaluation

"队伍" 统一表示为 3 个 member 的列表：
    {"position": "大营"|"中军"|"前锋", "hero": hero_dict, "skills": [skill_dict, skill_dict]}
允许 member 为 None（缺人）以便校验给出可读错误。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

QUALITY = {"S": 100, "A": 85, "B": 70, "C": 55, "D": 40}
TROOPS = ["步", "骑", "弓"]
POSITIONS = ["大营", "中军", "前锋"]
TARGET_MODES = ["打地", "野战", "攻城", "守城"]

KW_CONTROL = ["混乱", "暴走", "犹豫", "怯战", "挑衅", "围困", "妖术", "动摇", "恐慌", "燃烧", "缴械", "禁疗", "封印"]
KW_DAMAGE = ["伤害率", "造成攻击伤害", "造成策略攻击", "发动一次攻击", "发动一次猛攻", "策略攻击", "攻击伤害", "物理攻击"]
KW_HEAL = ["恢复一定兵力", "恢复率", "休整", "恢复兵力", "兵力恢复", "治疗"]
KW_BUFF = ["属性提高", "伤害提高", "受到伤害", "伤害降低", "免疫", "洞察"]

# 战法冲突（R-CONFLICT-01）把 KW_BUFF 再细分：增益 = 属性提高/伤害提高（自身强化），
# 减益 = 伤害降低/受到伤害/免疫（压制对方）。「洞察」（免控）任务清单未明确归类，暂并入增益
# ——(待补充：洞察归类依据)。
BUFF_UP = ["属性提高", "伤害提高", "洞察"]
BUFF_DOWN = ["伤害降低", "受到伤害", "免疫"]

# ---------------------------------------------------------------- 基础工具


def clean(s: Any) -> str:
    return " ".join(str(s if s is not None else "").split()).strip()


def is_missing(s: Any) -> bool:
    t = clean(s)
    return (not t) or t == "未获取" or t == "--"


def num(v: Any) -> float | None:
    if v is None:
        return None
    s = clean(v).replace("%", "")
    if not s or s == "未获取" or s == "--":
        return None
    out, buf = [], ""
    for ch in s:
        if ch.isdigit() or ch == "." or (ch == "-" and not buf):
            buf += ch
        elif buf:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    try:
        return float(out[0]) if out else None
    except ValueError:
        return None


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def pct(v: float) -> float:
    return round(clamp(v, 0.0, 100.0) * 10) / 10


def has_any(text: str, kws: Iterable[str]) -> bool:
    return any(k in text for k in kws)


def chance_range(s: Any) -> dict | None:
    """'35%' -> {lo:35,hi:35}; '25%-35%' -> {lo:25,hi:35}; '--' -> None"""
    t = clean(s)
    if not t or t in ("--", "未获取"):
        return None
    nums = []
    buf = ""
    for ch in t:
        if ch.isdigit() or ch == ".":
            buf += ch
        elif buf:
            nums.append(float(buf))
            buf = ""
    if buf:
        nums.append(float(buf))
    if not nums:
        return None
    return {"lo": min(nums), "hi": max(nums), "raw": t}


def target_kind(target: Any) -> str:
    s = clean(target)
    if not s:
        return "未知"
    if "攻击目标" in s:
        return "攻击目标"
    mine = ("我军" in s) or ("友军" in s)
    enemy = "敌军" in s
    if mine and enemy:
        return "敌我混合"
    if enemy:
        return "敌方"
    if mine:
        return "我方"
    if "自己" in s:
        return "自身"
    return "未知"


def target_count_of(target: Any) -> int:
    """从目标群体文本推断目标数：全体=3，群体=2，单体=1。"""
    s = clean(target)
    if "全体" in s:
        return 3
    if "群体" in s:
        return 2
    if "单体" in s:
        return 1
    return 1


def distance_need(target: Any) -> float | None:
    """旧版 R-DIST-01 的启发式"需要覆盖距离"，仅在我方/自身目标时作为兜底。"""
    s = clean(target)
    k = target_kind(s)
    if k in ("攻击目标", "自身"):
        return None
    group, grp, single = ("全体" in s), ("群体" in s), ("单体" in s)
    if k == "敌方":
        if group:
            return 5.0
        if grp:
            return 3.0
        if single:
            return 2.0
        return 3.0
    if k in ("我方", "敌我混合"):
        return 3.0
    return None


# ---------------------------------------------------------------- 距离几何（Q4-4）
# 我方：大营 0 → 中军 1 → 前锋 2 ；对方：前锋 3 → 中军 4 → 大营 5
OWN_SLOT = {"大营": 0, "中军": 1, "前锋": 2}
ENEMY_SLOT = {"前锋": 3, "中军": 4, "大营": 5}


def slot_distance(own_position: str, enemy_position: str) -> int:
    return abs(OWN_SLOT[own_position] - ENEMY_SLOT[enemy_position])


def reachable_enemy_positions(own_position: str, valid_range: float | None) -> list[str]:
    """该战法从该站位出发，能覆盖到对方哪些位置（对方三人齐全时）。"""
    if valid_range is None:
        return []
    return [ep for ep in POSITIONS if slot_distance(own_position, ep) <= valid_range]


# ---------------------------------------------------------------- 索引


@dataclass
class Index:
    heroes: list[dict]
    skills: list[dict]
    hero_by_id: dict[str, dict]
    skill_by_id: dict[str, dict]
    combos: list[dict]
    ranges: dict[str, dict] = field(default_factory=dict)


def build_index(heroes: list[dict], skills: list[dict], combos: list[dict]) -> Index:
    hero_by_id = {h["hero_id"]: h for h in heroes}
    skill_by_id: dict[str, dict] = {}
    for s in skills:
        skill_by_id.setdefault(s["skill_id"], s)

    nums = {"atk": [], "guile": [], "def": [], "spd": [], "siege": []}
    for h in heroes:
        for code, key in (("atk", "atk"), ("guile", "guile"), ("def", "def"), ("spd", "spd"), ("siege", "siege")):
            v = num(h.get(code))
            if v is not None:
                nums[key].append(v)
    ranges = {k: {"min": min(v), "max": max(v)} for k, v in nums.items() if v}

    return Index(heroes=heroes, skills=skills, hero_by_id=hero_by_id,
                 skill_by_id=skill_by_id, combos=combos, ranges=ranges)


def norm(v: float | None, r: dict | None) -> float:
    if v is None or not r or r["max"] == r["min"]:
        return 0.5
    return clamp((v - r["min"]) / (r["max"] - r["min"]), 0.0, 1.0)


# ---------------------------------------------------------------- 校验


@dataclass
class Validation:
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    infos: list[dict] = field(default_factory=list)
    total_cost: float = 0.0
    limit: float = 10.0

    @property
    def blocking(self) -> bool:
        return bool(self.errors)

    @property
    def legality(self) -> str:
        if self.errors:
            return "fail"
        return "warn" if self.warnings else "pass"


def _skill_slots(members: list) -> list[dict]:
    out = []
    for i, m in enumerate(members):
        if not m:
            continue
        for k in range(2):
            sk = (m["skills"] or [None, None])[k] if k < len(m.get("skills") or []) else None
            out.append({"member": m, "hero": m["hero"], "hero_idx": i, "skill": sk,
                        "label": f"第 {i + 1} 位（{m['position']}）战法 {k + 1}"})
    return out


def _skill_effect_categories(skill: dict) -> list[str]:
    """返回战法命中的效果类别（R-CONFLICT-01 判定用）。

    控制=KW_CONTROL；恢复=KW_HEAL；增益=BUFF_UP；减益=BUFF_DOWN；伤害=KW_DAMAGE。
    同一战法可能命中多个类别，全部返回，供「同类型且类别相交」判定。
    """
    desc = clean(skill.get("desc_text"))
    cats: list[str] = []
    if has_any(desc, KW_CONTROL):
        cats.append("控制")
    if has_any(desc, KW_HEAL):
        cats.append("恢复")
    if has_any(desc, BUFF_DOWN):
        cats.append("减益")
    if has_any(desc, BUFF_UP):
        cats.append("增益")
    if has_any(desc, KW_DAMAGE):
        cats.append("伤害")
    return cats


_CONFLICT_TEXT = {
    "控制": "同类型不可叠加，后释放失效",
    "增益": "数值替换，效果更高者保留",
    "减益": "数值替换，效果更高者保留",
    "恢复": "同类型不可叠加（恢复效果）",  # (待补充：恢复是否也后释放失效)
    "伤害": "同类型并存（伤害非叠加类判定）",  # (待补充：伤害战法是否冲突)
}


def _skill_conflicts(members: list) -> list[dict]:
    """队内 6 条战法两两比较，找出「skill_type 相同 + 效果类别相交」的冲突（R-CONFLICT-01）。

    只返回真正冲突的对；每对记录 type、重叠类别与文案。
    """
    skills: list[dict] = []
    for m in members:
        if not m:
            continue
        for s in (m.get("skills") or []):
            if s:
                skills.append(s)
    out: list[dict] = []
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            a, b = skills[i], skills[j]
            ta, tb = clean(a.get("skill_type")), clean(b.get("skill_type"))
            if not ta or not tb or ta != tb:
                continue
            ca, cb = _skill_effect_categories(a), _skill_effect_categories(b)
            shared = [c for c in ca if c in cb]
            if not shared:
                continue
            texts = [_CONFLICT_TEXT.get(c, f"同类{c}效果冲突") for c in shared]
            out.append({
                "skill_a": a.get("name"), "skill_b": b.get("name"),
                "type": ta, "categories": shared,
                "text": f"「{a.get('name')}」与「{b.get('name')}」同属{ta}且同含{'/'.join(shared)}效果：{'；'.join(texts)}",
            })
    return out


def validate(members: list, index: Index, cost_limit: float = 10.0) -> Validation:
    v = Validation(limit=cost_limit)

    present = [m for m in members if m and m.get("hero")]
    if len(present) != 3:
        v.errors.append({"rule": "R-INPUT-01", "text": f"需要 3 名武将，当前 {len(present)} 名"})
    positions = [m["position"] for m in present]
    if len(set(positions)) != len(positions):
        v.errors.append({"rule": "R-INPUT-01", "text": f"站位重复：{positions}"})

    seen: dict[str, int] = {}
    for i, m in enumerate(members):
        if not m or not m.get("hero"):
            continue
        h = m["hero"]
        hid = h["hero_id"]
        if hid not in index.hero_by_id:
            v.errors.append({"rule": "R-INPUT-01", "text": f"第 {i + 1} 位武将「{h.get('name', hid)}」不存在于武将数据中（ID {hid}）"})
        if hid in seen:
            v.errors.append({"rule": "R-INPUT-02", "text": f"武将重复：第 {seen[hid] + 1} 位与第 {i + 1} 位都是「{h.get('name')}」"})
        else:
            seen[hid] = i
        v.total_cost += num(h.get("cost")) or 0.0

    if v.total_cost > cost_limit + 1e-9:
        v.errors.append({"rule": "R-INPUT-03",
                         "text": f"cost 总和 {v.total_cost:.1f} 超过上限 {cost_limit:.1f}（超出 {v.total_cost - cost_limit:.1f}）"})

    skill_seen: dict[str, str] = {}
    for slot in _skill_slots(members):
        h, s, label = slot["hero"], slot["skill"], slot["label"]
        if not s:
            v.errors.append({"rule": "R-INPUT-01", "text": f"{label}未选择"})
            continue
        sid = s.get("skill_id")
        if sid not in index.skill_by_id:
            v.errors.append({"rule": "R-INPUT-01", "text": f"{label}「{s.get('name', sid)}」不存在于战法数据中（ID {sid}）"})
            continue
        if sid in skill_seen:
            v.errors.append({"rule": "R-INPUT-02", "text": f"战法重复：「{s.get('name')}」同时出现在{skill_seen[sid]}与{label}"})
        else:
            skill_seen[sid] = label

        troop_limit = clean(s.get("troop_limit"))
        hero_troop = clean(h.get("troop"))
        if troop_limit and troop_limit != "未获取" and hero_troop not in troop_limit:
            v.warnings.append({"rule": "R-INPUT-04",
                               "text": f"{label}「{s.get('name')}」标注兵种「{troop_limit}」，武将「{h.get('name')}」兵种「{hero_troop}」，不在其中（非硬限制，按警告）"})
        if is_missing(s.get("valid_range")) or is_missing(s.get("proc_text")):
            v.infos.append({"rule": "R-INPUT-05",
                            "text": f"{label}「{s.get('name')}」有效距离/发动几率为「{s.get('valid_range')}」「{s.get('proc_text')}」，已跳过该值"})

    # R-CONFLICT-01：同类战法的同种增益/减益/控制效果不可叠加（见 _skill_conflicts）
    for cf in _skill_conflicts(members):
        v.warnings.append({"rule": "R-CONFLICT-01", "text": cf["text"]})

    for m in present:
        h = m["hero"]
        # R-DIST-03：普攻只能打攻击距离内的目标；最低要求 大营 3 / 中军 2 / 前锋 1（到敌方前锋）。
        # atk_range 缺失（None）不报——数据没给攻击距离时无从判断，不做误报。
        atk_range = num(h.get("atk_range"))
        if atk_range is not None:
            need = slot_distance(m["position"], "前锋")
            if atk_range < need:
                v.warnings.append({"rule": "R-DIST-03",
                                   "text": f"「{h.get('name')}」攻击距离 {atk_range:g} < 从{m['position']}到敌方前锋的 {need} 格，普攻够不到"})
        if h.get("dismantle_skill2_name") in (None, "未获取"):
            v.infos.append({"rule": "R-INPUT-05", "text": f"武将「{h.get('name')}」的可拆战法2 为「未获取」，未参与任何判断"})
        if h.get("combo_name") in (None, "未获取"):
            v.infos.append({"rule": "R-INPUT-05", "text": f"武将「{h.get('name')}」的组合字段为「未获取」，组合加分按 0 计"})
    return v


# ---------------------------------------------------------------- 各维度评分


def compliance_score(v: Validation) -> float:
    return 0.0 if v.errors else pct(100 - 15 * len(v.warnings))


def cost_score(v: Validation) -> float:
    if any(e["rule"] == "R-INPUT-03" for e in v.errors):
        return 0.0
    if v.limit <= 0:
        return 0.0
    usage = v.total_cost / v.limit
    if usage > 1:
        return 0.0
    return 100.0 if usage >= 0.85 else pct(40 + 60 * (usage / 0.85))


def _top_count(values: list[str]) -> tuple[str | None, int]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None, 0
    best_v, best_n = "", 0
    for k, n in counts.items():
        if n > best_n:
            best_v, best_n = k, n
    return best_v, best_n


def find_active_combos(members: list, index: Index) -> list[dict]:
    present = [m for m in members if m and m.get("hero")]
    names = [clean(m["hero"].get("name")) for m in present]
    if len(names) < 3 or len(set(names)) != 3:
        return []
    out = []
    for c in index.combos:
        members_txt = clean(c.get("combo_members"))
        if not members_txt:
            continue
        hit = all(n in members_txt for n in names)
        if hit:
            out.append(c)
    return out


DEFAULT_SYNERGY_POINTS = {"camp": {3: 55, 2: 30, 1: 10}, "troop": {3: 25, 2: 12, 1: 0}, "combo": 20}


def _int_keyed(mapping: dict) -> dict:
    """把 rule_param 里 JSON 的字符串键（"3"/"2"/"1"）归一到 int 键，兼容直接传 int 键的默认值。"""
    out: dict = {}
    for k, v in (mapping or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            out[k] = v
    return out


def synergy_score(members: list, index: Index, points: dict | None = None) -> dict:
    present = [m for m in members if m and m.get("hero")]
    camp_v, camp_n = _top_count([clean(m["hero"].get("camp")) for m in present])
    troop_v, troop_n = _top_count([clean(m["hero"].get("troop")) for m in present])
    pts = points or DEFAULT_SYNERGY_POINTS
    camp_table = _int_keyed(pts.get("camp", {}))
    troop_table = _int_keyed(pts.get("troop", {}))
    camp_pts = camp_table.get(camp_n, 0)
    troop_pts = troop_table.get(troop_n, 0)
    combos = find_active_combos(members, index)
    combo_pts = int(pts.get("combo", 20)) if combos else 0
    return {"score": pct(camp_pts + troop_pts + combo_pts), "camp": camp_v, "camp_n": camp_n,
            "troop": troop_v, "troop_n": troop_n, "combos": combos,
            "camp_pts": camp_pts, "troop_pts": troop_pts, "combo_pts": combo_pts}


def skill_fit_score(members: list, index: Index) -> dict:
    matched = total = 0
    qs: list[float] = []
    low_quality: list[dict] = []
    for m in members:
        if not m or not m.get("hero"):
            continue
        hero_troop = clean(m["hero"].get("troop"))
        for s in (m.get("skills") or []):
            if not s:
                continue
            total += 1
            tt = clean(s.get("troop_limit"))
            if not tt or tt == "未获取" or hero_troop in tt:
                matched += 1
            q = QUALITY.get(clean(s.get("quality")), 0)
            qs.append(q)
            if clean(s.get("quality")) in ("C", "D"):
                low_quality.append(s)
    rate = matched / total if total else 0.0
    q_avg = statistics.fmean(qs) if qs else 0.0
    return {"score": pct(55 * rate + 45 * (q_avg / 100)), "matched": matched, "total": total,
            "rate": rate, "q_avg": q_avg, "low_quality": low_quality}


def coverage_of(member: dict, skill: dict) -> tuple[float | None, list[str], str | None]:
    """返回 (覆盖率 0~1, 可达敌方位置, 说明)。覆盖率 None 表示该战法不参与距离评分。"""
    pos = member["position"]
    rng = num(skill.get("valid_range"))
    if rng is None:
        return None, [], None
    kind = target_kind(skill.get("target_group"))
    if kind == "敌方":
        need = target_count_of(skill.get("target_group"))
        reach = reachable_enemy_positions(pos, rng)
        cover = min(1.0, len(reach) / need) if need else 1.0
        note = None
        if cover < 1:
            note = (f"「{skill.get('name')}」目标「{skill.get('target_group')}」，从{pos}出发有效距离 {rng:g} "
                    f"只能覆盖 {len(reach)}/{need} 个目标位置（可达：{'、'.join(reach) or '无'}）")
        return cover, reach, note
    need = distance_need(skill.get("target_group"))
    if need is None:
        return None, [], None
    return clamp(rng / need, 0.0, 1.0), [], None


DEFAULT_DISTANCE_SPLIT = {"skill_cover": 0.6, "position_fit": 0.4}


def position_fit_score(members: list, index: Index) -> dict:
    """站位适性（0~100）：大营=主力输出 → max(atk,guile)；中军=控制/辅助 → 0.6*控制覆盖+0.4*速度；
    前锋=肉盾 → def。属性侧用已有 norm() 标准化（不做新阈值），战法侧按 KW_* 计数/2 clamp。

    中军 0.6/0.4 与「/2 clamp」均为待确认口径（无官方权重）——(待补充：中军权重与覆盖分母依据)。
    """
    r = index.ranges
    fits: list[float] = []
    notes: list[dict] = []
    for m in members:
        if not m or not m.get("hero"):
            continue
        h = m["hero"]
        pos = m["position"]
        skills = [s for s in (m.get("skills") or []) if s]
        control_n = sum(1 for s in skills if has_any(clean(s.get("desc_text")), KW_CONTROL))
        if pos == "大营":
            out_v = max(num(h.get("atk")) or 0.0, num(h.get("guile")) or 0.0)
            fit = norm(out_v, r.get("atk"))
        elif pos == "中军":
            control_cover = clamp(control_n / 2, 0.0, 1.0)
            spd = norm(num(h.get("spd")), r.get("spd"))
            fit = 0.6 * control_cover + 0.4 * spd
        else:  # 前锋
            fit = norm(num(h.get("def")), r.get("def"))
        fits.append(fit)
        notes.append({"position": pos, "hero": h.get("name"), "fit": pct(fit * 100)})
    return {"score": pct(statistics.fmean(fits) * 100) if fits else 50.0,
            "per_member": notes}


def distance_score(members: list, index: Index | None = None, split: dict | None = None) -> dict:
    parts: list[float] = []
    notes: list[str] = []
    covers: list[dict] = []
    for m in members:
        if not m or not m.get("hero"):
            continue
        for s in (m.get("skills") or []):
            if not s:
                continue
            cover, reach, note = coverage_of(m, s)
            if cover is None:
                continue
            parts.append(cover)
            covers.append({"position": m["position"], "skill": s.get("name"), "cover": cover, "reach": reach})
            if note:
                notes.append(note)
            if num(s.get("valid_range")) == 1 and (num(m["hero"].get("atk_range")) or 0) >= 3:
                notes.append(f"「{s.get('name')}」有效距离 1（近身），而「{m['hero'].get('name')}」攻击距离 {m['hero'].get('atk_range')}，可能覆盖不到常规站位（R-DIST-02）")
    cover_score = pct(statistics.fmean(parts) * 100) if parts else 50.0
    sp = split or DEFAULT_DISTANCE_SPLIT
    pos_fit = position_fit_score(members, index) if index is not None else {"score": 50.0, "per_member": []}
    score = pct(sp.get("skill_cover", 0.6) * cover_score + sp.get("position_fit", 0.4) * pos_fit["score"])
    return {"score": score, "covered": len(parts), "notes": notes, "covers": covers,
            "skill_cover_score": cover_score, "position_fit": pos_fit}


DEFAULT_CONFLICT_PENALTY = {"per": 8, "cap": 16}


def structure_score(members: list, conflict_penalty: dict | None = None) -> dict:
    skills: list[dict] = []
    for m in members:
        for s in (m.get("skills") or []):
            if s:
                skills.append(s)
    damage = [s for s in skills if has_any(clean(s.get("desc_text")), KW_DAMAGE)]
    control = [s for s in skills if has_any(clean(s.get("desc_text")), KW_CONTROL)]
    heal = [s for s in skills if has_any(clean(s.get("desc_text")), KW_HEAL)]
    active = [s for s in skills if clean(s.get("skill_type")) == "主动"]
    chances = [c["hi"] for c in (chance_range(s.get("proc_text")) for s in active) if c]
    chance_avg = statistics.fmean(chances) if chances else None
    if chance_avg is None:
        chance_pts = 50.0
    elif chance_avg >= 40:
        chance_pts = 100.0
    elif chance_avg >= 30:
        chance_pts = 80.0
    elif chance_avg >= 25:
        chance_pts = 60.0
    else:
        chance_pts = 40.0
    cover = (40 if len(damage) >= 2 else 20 if len(damage) == 1 else 0) \
        + (30 if control else 0) + (30 if heal else 0)
    # R-CONFLICT-01 扣分：每条冲突扣 per 分，上限 cap 分（口径待确认）。
    cp = conflict_penalty or DEFAULT_CONFLICT_PENALTY
    conflicts = _skill_conflicts(members)
    penalty = min(len(conflicts) * int(cp.get("per", 8)), int(cp.get("cap", 16)))
    return {"score": pct(0.6 * cover + 0.4 * chance_pts - penalty), "damage": len(damage),
            "control": len(control), "heal": len(heal), "active": len(active),
            "chance_avg": chance_avg, "chance_pts": chance_pts, "cover": cover,
            "conflicts": conflicts, "conflict_penalty": penalty}


DEFAULT_SIEGE_SKILL_KEYWORDS = ["攻城", "云梯", "冲车", "毁墙", "器械"]


def combat_scores(members: list, index: Index, mode: str, weights: dict[str, float],
                  siege_keywords: list[str] | None = None) -> dict:
    present = [m for m in members if m and m.get("hero")]
    if not present:
        return {"total": 0.0, "parts": {}, "mode": mode, "weights": weights}
    atk_intel = [max(num(m["hero"].get("atk")) or 0.0, num(m["hero"].get("guile")) or 0.0) for m in present]
    defs = [num(m["hero"].get("def")) or 0.0 for m in present]
    speeds = [num(m["hero"].get("spd")) or 0.0 for m in present]
    sieges = [num(m["hero"].get("siege")) or 0.0 for m in present]
    skills = [s for m in present for s in (m.get("skills") or []) if s]
    damage_n = sum(1 for s in skills if has_any(clean(s.get("desc_text")), KW_DAMAGE))
    control_n = sum(1 for s in skills if has_any(clean(s.get("desc_text")), KW_CONTROL))
    heal_n = sum(1 for s in skills if has_any(clean(s.get("desc_text")), KW_HEAL))

    r = index.ranges
    output = pct(0.75 * statistics.fmean(norm(v, r.get("atk")) for v in atk_intel) * 100
                 + 0.25 * clamp(damage_n / 3, 0, 1) * 100)
    survive = pct(0.70 * statistics.fmean(norm(v, r.get("def")) for v in defs) * 100
                  + 0.30 * clamp(heal_n / 2, 0, 1) * 100)
    control = pct(clamp(control_n / 2, 0, 1) * 100)
    speed = pct(statistics.fmean(norm(v, r.get("spd")) for v in speeds) * 100)
    # (8) 攻城分量：0.7*攻城属性norm + 0.3*min(1, 攻城战法数/2)。仅在 mode="攻城" 时权重高，
    # 其它场景权重本就是 0/0.05，公式保持一致即可（不另起分支）。
    skw = siege_keywords or DEFAULT_SIEGE_SKILL_KEYWORDS
    siege_attr = statistics.fmean(norm(v, r.get("siege")) for v in sieges)
    siege_skill_n = sum(1 for s in skills if has_any(clean(s.get("desc_text")), skw))
    siege = pct(0.7 * siege_attr * 100 + 0.3 * min(1.0, siege_skill_n / 2) * 100)
    parts = {"output": output, "survive": survive, "control": control, "speed": speed, "siege": siege}
    total = pct(sum(weights.get(k, 0.0) * v for k, v in parts.items()))
    return {"total": total, "parts": parts, "mode": mode, "weights": weights,
            "damage_n": damage_n, "control_n": control_n, "heal_n": heal_n, "siege_skill_n": siege_skill_n}


@dataclass
class Evaluation:
    validation: Validation
    dims: dict
    system_score: float
    combat: dict
    total: float
    verdict: str
    verdict_reason: str

    @property
    def legality(self) -> str:
        return self.validation.legality


def evaluate(members: list, index: Index, target: str, cost_limit: float,
             system_weights: dict[str, float], combat_weights: dict[str, float],
             merge: tuple[float, float] = (0.6, 0.4), thresholds: dict | None = None,
             params: dict | None = None) -> Evaluation:
    """params 是可选的 rule_param 覆盖（engine.load_rule_params 传入）。

    为 None 时各维度全部走内置默认值 == 旧版数值，所以既有调用方
    （cli / apptest_smoke）不改也不会变分（兼容性契约）。
    """
    p = params or {}
    v = validate(members, index, cost_limit)
    dims = {
        "compliance": {"score": compliance_score(v), "errors": len(v.errors), "warnings": len(v.warnings)},
        "cost": {"score": cost_score(v), "total": v.total_cost, "limit": v.limit,
                 "usage": (v.total_cost / v.limit) if v.limit else 0.0},
        "synergy": synergy_score(members, index, p.get("synergy_points")),
        "skillFit": skill_fit_score(members, index),
        "distance": distance_score(members, index, p.get("distance_split")),
        "structure": structure_score(members, p.get("conflict_penalty")),
    }
    system_score = pct(sum(system_weights.get(k, 0.0) * dims[k]["score"] for k in system_weights))
    combat = combat_scores(members, index, target, combat_weights, p.get("siege_skill_keywords"))
    total = pct(merge[0] * system_score + merge[1] * combat["total"])

    thr = thresholds or {"ok": 75, "tune": 55}
    if v.errors:
        verdict, reason = "invalid", f"存在 {len(v.errors)} 项硬性错误，需先修正"
    elif total >= thr.get("ok", 75):
        verdict, reason = "ok", f"综合分 {total:.1f} ≥ {thr.get('ok', 75)}"
    elif total >= thr.get("tune", 55):
        verdict, reason = "tune", f"综合分 {total:.1f} 落在需调整区间"
    else:
        verdict, reason = "no", f"综合分 {total:.1f} < {thr.get('tune', 55)}"
    return Evaluation(validation=v, dims=dims, system_score=system_score, combat=combat,
                      total=total, verdict=verdict, verdict_reason=reason)


def members_from_row(rows: list[dict], index: Index) -> list:
    """把 lineup_member 行（position/hero_id/skill1_id/skill2_id）还原成 members。"""
    by_pos = {}
    for r in rows:
        m = {"position": r["position"], "hero": index.hero_by_id.get(r["hero_id"]),
             "skills": [index.skill_by_id.get(r.get("skill1_id")), index.skill_by_id.get(r.get("skill2_id"))]}
        by_pos[r["position"]] = m
    return [by_pos.get(p) for p in POSITIONS]


def fingerprint(members: list) -> str:
    """按「位置:武将:该将战法（排序后逗号连接）」逐段拼接，段间用 # 分隔。

    旧实现把 hero_ids 与 skill_ids 各自排序后拼接，丢掉了「谁带哪个战法」的配对关系：
    两支武将相同、战法集合相同但配对互换的队伍会撞出同一个指纹，于是
    `team_score` 的唯一键 (run_id, fingerprint) 让后一条覆盖前一条、`lineup` 复用
    也会把两支不同的队伍当成同一支。这里按位置拼，配对关系进指纹。
    位置升序遍历（大营 → 中军 → 前锋），签名保持 `fingerprint(members) -> str`。
    """
    by_pos = {m["position"]: m for m in members if m and m.get("hero")}
    segs: list[str] = []
    for pos in POSITIONS:
        m = by_pos.get(pos)
        if not m:
            continue
        skill_ids = sorted(s["skill_id"] for s in (m.get("skills") or []) if s)
        segs.append(f"{pos}:{m['hero']['hero_id']}:{','.join(skill_ids)}")
    return "#".join(segs)
