"""「配将界面」自定义组件（武将画像条 + 拖拽入位）。

为什么用组件而不是纯 Streamlit 控件：Streamlit 没有拖拽原语，用
``st.selectbox`` 只能做到「用下拉框选人」，复刻不出主城配将界面的
「底部立绘条 → 拖进大营/中军/前锋」的交互。组件用 ``declare_component``
挂 ``components/lineup_board/index.html``，通过 component 协议回传
``{"name": ..., "slots": {...}}``，Python 侧只负责准备数据与落库。

降级：``declare_component`` 失败（例如目录被删）时 ``available()`` 为 False，
``app.py`` 会回落到原来的下拉框录入界面，不会白屏。
"""
from __future__ import annotations

import os

import streamlit.components.v1 as components

from . import core

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "components", "lineup_board")
_POS = list(core.POSITIONS)

_COMPONENT = None
_ERROR: str | None = None
try:  # 组件目录缺失/环境不支持时不要拖垮整页
    _COMPONENT = components.declare_component("stzb_lineup_board", path=_DIR)
except Exception as exc:  # pragma: no cover - 取决于运行环境
    _ERROR = str(exc)


def available() -> bool:
    return _COMPONENT is not None


def error() -> str | None:
    return _ERROR


def empty_state(name: str = "我的队伍1") -> dict:
    return {"name": name, "slots": {p: None for p in _POS}}


def _hero_items(index: core.Index, ids: set[str], owned_heroes: set[str],
                img_map: dict[str, str] | None = None) -> list[dict]:
    """把武将行整理成前端要的轻量结构；重名用「#ID末三位」区分（同①页口径）。"""
    img_map = img_map or {}
    name_count: dict[str, int] = {}
    for h in index.heroes:
        name_count[h.get("name") or ""] = name_count.get(h.get("name") or "", 0) + 1
    out: list[dict] = []
    for h in index.heroes:
        if ids and h["hero_id"] not in ids:
            continue
        nm = h.get("name") or h["hero_id"]
        dup = name_count.get(nm, 0) > 1
        out.append({
            "id": h["hero_id"],
            "name": nm,
            "suffix": f" #{(h.get('hero_id') or '')[-3:]}" if dup else "",
            "star": int(core.num(h.get("star")) or 0),
            "camp": h.get("camp") or "",
            "troop": h.get("troop") or "",
            "cost": float(core.num(h.get("cost")) or 0),
            "atk": _int(h.get("atk")), "guile": _int(h.get("guile")),
            "def": _int(h.get("def")), "spd": _int(h.get("spd")),
            "owned": h["hero_id"] in owned_heroes,
            "img": img_map.get(h["hero_id"]) or "",
        })
    # 已拥有的排前面，其余按星级降序，方便直接拖
    out.sort(key=lambda x: (not x["owned"], -x["star"], x["name"]))
    return out


def _int(v) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def build_args(conn, index: core.Index, state: dict, cost_limit: float,
               owned_heroes: list[str], owned_skills: list[str]) -> dict:
    """组装组件入参：武将池、战法池、每将可用战法、当前状态。"""
    oh, os_ = set(owned_heroes), set(owned_skills)
    pool_full = not oh  # 没勾持仓时给全库，和引擎的 pool_fallback 口径一致
    from . import portraits as portrait_mod
    try:
        img_map = portrait_mod.img_map(conn)
    except Exception:      # 老库还没建 hero_portrait 表时不要拖垮页面
        img_map = {}
    heroes = _hero_items(index, oh if oh else set(), oh, img_map)

    skills: dict[str, dict] = {}
    hero_skills: dict[str, list[str]] = {}
    all_skill_ids = [s["skill_id"] for s in index.skills]
    use_ids = [i for i in all_skill_ids if (not os_) or i in os_]
    for sid in use_ids:
        s = index.skill_by_id.get(sid) or {}
        skills[sid] = {"name": s.get("name") or sid,
                       "quality": s.get("quality") or "",
                       "type": s.get("skill_type") or ""}
    for h in index.heroes:
        troop = h.get("troop") or ""
        ok = []
        for sid in use_ids:
            s = index.skill_by_id.get(sid) or {}
            lim = (s.get("troop_limit") or "").strip()
            if not troop or not lim or lim == "未获取" or troop in lim:
                ok.append(sid)
        hero_skills[h["hero_id"]] = ok

    note = []
    if pool_full:
        note.append("未勾选「我的武将」：武将库显示全库（与④页的全库回退口径一致）。")
    if not os_:
        note.append("未勾选「我的战法」：战法下拉显示全库兵种兼容战法。")
    else:
        note.append(f"战法下拉只列「我的战法」{len(os_)} 条中兵种兼容的部分。")
    return {
        "heroes": heroes,
        "skills": skills,
        "hero_skills": hero_skills,
        "cost_limit": float(cost_limit),
        "state": state,
        "note": " ".join(note),
        "reset_token": state.get("_rev"),
    }


def render(conn, index: core.Index, state: dict, cost_limit: float,
           owned_heroes: list[str], owned_skills: list[str], key: str = "lineup_board"):
    """渲染组件并返回最新状态（dict）；组件不可用时返回 None。"""
    if not available():
        return None
    args = build_args(conn, index, state, cost_limit, owned_heroes, owned_skills)
    # 注意：Streamlit 1.66 会把所有 kwargs 原样塞进 render 消息的 args 里，
    # 写成 `_COMPONENT(args=args, ...)` 会让前端收到 {"args": {...}} 而多一层嵌套
    # （实测 .pcard 一直是 0）。必须是 `**args`，把字段平铺进 args。
    return _COMPONENT(**args, default=state, key=key)


def state_to_members(state: dict, index: core.Index) -> list[dict]:
    """把组件状态转成 engine/core 需要的 members 结构（含空位）。"""
    slots = (state or {}).get("slots") or {}
    out = []
    for p in _POS:
        m = slots.get(p) or {}
        hid = m.get("hero_id")
        sks = list(m.get("skills") or [])
        sks = [x for x in sks if x] + [None, None]
        out.append({"position": p, "hero_id": hid, "skill1_id": sks[0], "skill2_id": sks[1],
                    "hero": index.hero_by_id.get(hid) if hid else None})
    return out


def summarize(state: dict, index: core.Index) -> str:
    """一行摘要，供保存按钮下方显示，便于确认状态已回传 Python。"""
    slots = (state or {}).get("slots") or {}
    parts = []
    for p in _POS:
        m = slots.get(p) or {}
        hid = m.get("hero_id")
        if not hid:
            parts.append(f"{p}：空")
            continue
        h = index.hero_by_id.get(hid) or {}
        sk = [index.skill_by_id.get(x, {}).get("name") for x in (m.get("skills") or []) if x]
        parts.append(f"{p}：{h.get('name', hid)}" + (f"（{' / '.join(sk)}）" if sk else ""))
    return "　".join(parts)
