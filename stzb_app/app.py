"""Streamlit 单机界面：我的武将 / 我的战法 / 搭队伍 / 推荐结果。

启动：  streamlit run stzb_app/app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# `streamlit run stzb_app/app.py` 会把本文件当作 __main__ 执行，无父包上下文，
# 所以这里手动把项目根目录塞进 sys.path，再用绝对包名导入。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from stzb_app import cli
from stzb_app import core
from stzb_app import db as dbmod
from stzb_app import engine
from stzb_app import ingest
from stzb_app import ui_board

st.set_page_config(page_title="率土之滨 · 配将推荐", page_icon="⚔️", layout="wide")

# 离线版（v1）：不登录、不口令。数据表底层已带 user_key 列（留给后续账号版），
# 这里固定用 'local'；界面不暴露标识，也不用 ?u=。需要临时隔离时设环境变量 STZB_USER。
# 配将时不再要求先勾持仓：③ 页武将池/战法池默认就是全库（勾过的排前面当快捷筛选）。
user_key = os.environ.get("STZB_USER") or "local"


@st.cache_resource(show_spinner="加载索引 …")
def build_index(version: int) -> core.Index:
    """索引是只读快照，可跨 rerun 复用；单独开一条临时连接，不持有会话连接。"""
    tmp = dbmod.connect()
    try:
        return engine.load_index(tmp)
    finally:
        tmp.close()


def data_version(c: object) -> int:
    """缓存键 = 武将 + 战法 + 组合 + 证据 四张表的行数之和。

    只数 hero 行时，改战法/组合/证据表不会让 build_index 的缓存失效，
    界面会继续用旧索引（改了战法数据却看不到任何变化，还会误以为没生效）。
    """
    tables = ("hero", "skill", "combo", "evidence_doc")
    return sum(int(c.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]) for t in tables)


def _normalize_top_n(c: object, user_key: str) -> None:
    """把库里的 user_settings.top_n 归一到滑杆区间 [5, 10]。

    ④ 页滑杆最小值是 5；库里若被 `cli --selftest --top 3` / `--demo --top 3` 写成 3，
    Streamlit 的 slider 会**静默**把区间扩成 3–10 并默认只出 3 条，用户看到的是
    「滑杆没动、结果却少了」。放在 app 启动时而不是 db._init_once 里，是因为
    init_db 在 user_version 一致时直接短路返回，老库永远修不到；这里的 UPDATE
    带 WHERE 越界条件，正常 rerun 只读不写，只有真越界时才写一次。
    """
    row = c.execute("SELECT top_n FROM user_settings WHERE user_key=?", (user_key,)).fetchone()
    if not row:
        return
    n = int(row["top_n"] or 10)
    if 5 <= n <= 10:
        return
    c.execute("UPDATE user_settings SET top_n=? WHERE user_key=? AND (top_n<? OR top_n>?)",
              (max(5, min(10, n)), user_key, 5, 10))
    c.commit()


try:
    conn = dbmod.connect()
    # init_db 内部先读 PRAGMA user_version 短路：版本已是最新时只做一次只读检查，
    # 不执行 schema 脚本、不写种子数据，多标签页并发打开也不会互相抢写锁。
    # 真要建库时它自己带 database-is-locked 退避重试。
    dbmod.init_db(conn)
    _normalize_top_n(conn, user_key)
    ver = data_version(conn)
except Exception as exc:  # 绝不静默吞异常：否则会伪装成「数据为空」
    st.error("数据库初始化失败，下面是原始异常（这不是「数据为空」）：")
    st.exception(exc)
    st.stop()

if ver == 0:
    st.warning("数据库为空：需要先导入武将/战法/社区阵容数据。")
    st.caption("重建会用 `stzb_hero_data`、`stzb_skill_data`、`stzb_team_eval` 里的原始文件重新导入，覆盖当前库。")
    ok = st.checkbox("我确认：重建并覆盖当前数据库（库里已有的「常用清单/我的队伍」会丢失）")
    if st.button("导入数据（约 10~30 秒）", type="primary", disabled=not ok):
        with st.spinner("导入中 …"):
            conn.close()
            ingest.run(rebuild=True)
        st.cache_resource.clear()
        st.rerun()
    st.stop()

index = build_index(ver)

# 同名多形态武将（如孙坚×4）在界面上补 ID 末三位以便区分
_name_counts: dict[str, int] = {}
for _h in index.heroes:
    _name_counts[_h["name"]] = _name_counts.get(_h["name"], 0) + 1
DUP_NAMES = {n for n, c in _name_counts.items() if c > 1}


# ---------------------------------------------------------------- helpers
def hero_label(index: core.Index, hid: str) -> str:
    h = index.hero_by_id.get(hid)
    if not h:
        return hid
    star = int(core.num(h.get("star")) or 0)
    suffix = f"　#{hid[-3:]}" if h["name"] in DUP_NAMES else ""
    return f"{h['name']}{suffix}　{star}★{h.get('camp') or '?'}·{h.get('troop') or '?'}·cost {core.num(h.get('cost'))}"


def skill_label(index: core.Index, sid: str) -> str:
    s = index.skill_by_id.get(sid)
    if not s:
        return sid
    return f"{s['name']}　[{s.get('quality') or '?'}·{s.get('skill_type') or '?'}·{s.get('troop_limit') or '通用'}]"


def _tokens(q: str) -> list[str]:
    """把输入切成小写关键词：全角空格、逗号、顿号都当分隔符，其余原样保留。"""
    raw = (q or "")
    for ch in ("　", "，", ",", "、", "；", ";"):
        raw = raw.replace(ch, " ")
    return [t for t in raw.strip().lower().split() if t]


def hero_search(index: core.Index, q: str) -> list[str] | None:
    """按关键字检索武将：子串匹配 + 忽略大小写 + 多关键词 AND。

    检索字段：名称、阵营、兵种、星级（如「5星」）、以及列表里的完整标签
    （含同名多形态武将的 `#ID末三位`），所以「孙坚」「#033」「5星 骑」都能命中。
    输入为空返回 None，调用方据此恢复默认列表。
    """
    toks = _tokens(q)
    if not toks:
        return None
    out: list[str] = []
    for h in index.heroes:
        hay = " ".join([
            str(h.get("name") or ""), str(h.get("camp") or ""), str(h.get("troop") or ""),
            f"{int(core.num(h.get('star')) or 0)}星", hero_label(index, h["hero_id"]),
        ]).lower()
        if all(t in hay for t in toks):
            out.append(h["hero_id"])
    return out


def skill_search(index: core.Index, q: str) -> list[str] | None:
    """按关键字检索战法：同名不同品质/类型时靠标签区分（名称、品质、类型、兵种限制）。"""
    toks = _tokens(q)
    if not toks:
        return None
    out: list[str] = []
    for s in index.skills:
        hay = " ".join([
            str(s.get("name") or ""), str(s.get("quality") or ""), str(s.get("skill_type") or ""),
            str(s.get("troop_limit") or ""), skill_label(index, s["skill_id"]),
        ]).lower()
        if all(t in hay for t in toks):
            out.append(s["skill_id"])
    return out


def skill_ids_for(index: core.Index, hero: dict | None) -> list[str]:
    """按兵种兼容性过滤候选战法（只是缩小下拉框，不参与判罚）。"""
    ids: list[str] = []
    troop = (hero or {}).get("troop") or ""
    for s in index.skills:
        limit = s.get("troop_limit") or ""
        if not troop or not limit or troop in limit:
            ids.append(s["skill_id"])
    return ids


def owned_sets(user_key: str) -> tuple[list[str], list[str]]:
    heroes = [r["hero_id"] for r in conn.execute(
        "SELECT hero_id FROM user_hero WHERE user_key=? AND owned=1", (user_key,))]
    skills = [r["skill_id"] for r in conn.execute(
        "SELECT skill_id FROM user_skill WHERE user_key=? AND owned=1", (user_key,))]
    return heroes, skills


def save_owned(table: str, id_col: str, ids: list[str], user_key: str) -> None:
    conn.execute(f"UPDATE {table} SET owned=0 WHERE user_key=?", (user_key,))
    for i in ids:
        conn.execute(f"INSERT INTO {table}(user_key,{id_col},owned) VALUES(?,?,1) "
                     f"ON CONFLICT(user_key,{id_col}) DO UPDATE SET owned=1", (user_key, i))
    conn.commit()


VERDICT_LABEL = {"ok": "可用", "tune": "可调", "no": "重构", "invalid": "非法"}


def _on_budget_profile() -> None:
    """「搜索预算档」下拉框即时落库（user_settings.budget_profile）。

    和「允许拆解」同一个坑：只改 session_state 的话，重开页面/换标签页又变回库里的旧档，
    而 ④ 页的 selectbox 默认值是从库里读的。回调在 rerun 的脚本体之前跑，
    模块级 conn 可能还是上一次 run 的对象，所以这里自己开一条短连接。
    """
    c = dbmod.connect()
    try:
        label = str(st.session_state.get("budget_pick") or "")
        dbmod.set_settings(c, user_key, budget_profile="deep" if label.startswith("B") else "fast")
    finally:
        c.close()


def _on_allow_dismantle() -> None:
    """「允许拆解」开关即时落库（B2 口径），避免只改 session_state、下次打开又变回去。

    回调在 rerun 的脚本体之前执行，此时模块级 conn 可能还是上一次 run 的对象，
    所以这里自己开一条短连接，写完立刻关。
    """
    c = dbmod.connect()
    try:
        dbmod.set_settings(c, user_key, allow_dismantle=int(bool(st.session_state.get("allow_dismantle"))))
    finally:
        c.close()


def hero_name(index: core.Index, hid: str | None) -> str:
    if not hid:
        return "空"
    return (index.hero_by_id.get(hid) or {}).get("name") or hid


def member_hero(m: dict | None) -> dict:
    """取 member 的 hero；旧库漂移 / 删将时是 None，返回空 dict 供调用方兜底。

    直接写 `(m or {}).get("hero", {}).get("name", "空")` 是错的：`hero` 键存在但值为
    None 时 `.get` 的默认值不生效，下一步就在 None 上取属性 → AttributeError → ④ 页白屏。
    """
    return ((m or {}).get("hero") or {})


def member_hero_name(m: dict | None) -> str:
    """成员武将名，失效时给可读兜底文案（不能静默显示成「空」，用户分不清是空位还是丢数据）。"""
    hero = member_hero(m)
    if hero.get("name"):
        return str(hero["name"])
    hid = (m or {}).get("hero_id") or hero.get("hero_id")
    return f"（失效武将 {hid}）" if hid else "空"


def load_manual_seeds(user_key: str) -> list[dict]:
    rows = conn.execute("SELECT lineup_id, name FROM lineup WHERE user_key=? AND source='user' "
                        "ORDER BY lineup_id", (user_key,)).fetchall()
    seeds = []
    for r in rows:
        members = [dict(m) if m else None for m in engine.lineup_members(conn, r["lineup_id"])]
        if members and any(m and m.get("hero_id") for m in members):
            seeds.append({"name": r["name"], "members": members, "source": "manual",
                          "lineup_id": r["lineup_id"]})
    return seeds


# ---------------------------------------------------------------- layout
st.title("⚔️ 率土之滨 · 配将推荐")
st.caption("规则引擎 + 评分表（v1，不做战斗模拟）｜数据只在本机 SQLite")

owned_heroes, owned_skills = owned_sets(user_key)
# 设置只在本次 run 开头读一次，②页开关、④页滑杆、⑤页说明共用同一份
settings = dbmod.get_settings(conn, user_key)
c1, c2, c3, c4 = st.columns(4)
c1.metric("武将库", f"{len(index.heroes)}")
c2.metric("战法库", f"{len(index.skill_by_id)}")
c3.metric("常用武将", f"{len(owned_heroes)}")
c4.metric("常用战法", f"{len(owned_skills)}")

# 保存成功后 rev+1 → 勾选框/检索框换新的 widget key，从而重新按数据库内容渲染。
# （不直接改 widget 的 session_state，避免 Streamlit「widget 已实例化后不能再改」的限制）
if "owned_rev" not in st.session_state:
    st.session_state["owned_rev"] = 0
rev = int(st.session_state["owned_rev"])

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["① 常用武将", "② 常用战法", "③ 配将界面", "④ 推荐结果", "⑤ 数据与规则"])


# ---------------------------------------------------------------- tab1
with tab1:
    st.subheader("常用武将（快捷筛选，可跳过）：勾选 或 按名称输入")
    st.caption("两种方式并存、结果合并保存：列表适合批量勾选，检索框适合只记得名字（或「5星 骑」这类特征）的时候。")

    q = st.text_input(
        "按名称检索武将",
        key="hero_search",
        placeholder="例如：孙坚、诸葛、5星 骑、#033　（子串匹配 / 忽略大小写 / 空格分隔多关键词）",
        help="检索字段：名称、阵营、兵种、星级，以及标签里的「#ID末三位」。清空输入即恢复完整列表。",
    )
    matched = hero_search(index, q)
    if matched is None:
        st.caption("检索框为空：下面是完整武将列表，勾选即可。")
    elif not matched:
        st.warning(f"未找到匹配项：{q!r}。换个更短的关键字试试（如只输姓氏），或清空检索框。")
    else:
        st.multiselect(
            f"匹配到 {len(matched)} 名武将（选中的会和下方勾选结果合并保存）",
            matched, key=f"hero_search_pick_{rev}", format_func=lambda x: hero_label(index, x),
        )

    ids_all = [h["hero_id"] for h in index.heroes]
    owned_set = set(owned_heroes)
    st.multiselect("常用武将（完整列表）", ids_all, default=[i for i in ids_all if i in owned_set],
                   format_func=lambda x: hero_label(index, x), key=f"pick_heroes_{rev}")
    col_a, col_b, _ = st.columns([1, 1, 3])
    if col_a.button("保存武将", type="primary", key="save_hero"):
        picked_ids = set(st.session_state.get(f"pick_heroes_{rev}") or [])
        if matched:  # 只有检索框此刻真的在显示匹配项时，才把它的选择算进来
            picked_ids |= set(st.session_state.get(f"hero_search_pick_{rev}") or [])
        merged = [i for i in ids_all if i in picked_ids]
        save_owned("user_hero", "hero_id", merged, user_key)
        st.session_state["owned_rev"] = rev + 1
        st.toast(f"已保存 {len(merged)} 名武将")
        st.rerun()
    if col_b.button("全选 5 星", key="all5"):
        picked = [h["hero_id"] for h in index.heroes if int(core.num(h.get("star")) or 0) >= 5]
        save_owned("user_hero", "hero_id", picked, user_key)
        st.session_state["owned_rev"] = rev + 1
        st.rerun()


# ---------------------------------------------------------------- tab2
with tab2:
    st.subheader("常用战法（快捷筛选，可跳过）：勾选 或 按名称输入")
    st.caption("「允许拆解」开关打开时，可拆战法也算已拥有；开关状态会落库（B2 口径）。同样支持检索框。")
    allow_dismantle = st.toggle(
        "允许拆解（把可拆战法视为已拥有）",
        value=bool(settings.get("allow_dismantle")),
        key="allow_dismantle",
        on_change=_on_allow_dismantle,
        help="打开后，武将对「可拆战法」的默认勾选也计入我的战法；这是输入口径，不改变任何评分公式。",
    )
    sid_all = [s["skill_id"] for s in index.skills]

    qs = st.text_input(
        "按名称检索战法",
        key="skill_search",
        placeholder="例如：战必断金、主动、S　（子串匹配 / 忽略大小写 / 空格分隔多关键词）",
        help="检索字段：名称、品质（S/A/B/C/D）、类型（主动/指挥/被动/追击）、兵种限制。清空输入即恢复完整列表。",
    )
    matched_s = skill_search(index, qs)
    if matched_s is None:
        st.caption("检索框为空：下面是完整战法列表，勾选即可。")
    elif not matched_s:
        st.warning(f"未找到匹配项：{qs!r}。换个更短的关键字试试（如只输名字里的两个字），或清空检索框。")
    else:
        st.multiselect(
            f"匹配到 {len(matched_s)} 个战法（选中的会和下方勾选结果合并保存）",
            matched_s, key=f"skill_search_pick_{rev}_{int(allow_dismantle)}",
            format_func=lambda x: skill_label(index, x),
        )

    default_ids = set(owned_skills)
    if allow_dismantle:
        for h in index.heroes:
            for k in ("dismantle_skill_id", "dismantle_skill2_id"):
                if h.get(k):
                    default_ids.add(h[k])
    st.multiselect("常用战法（完整列表）", sid_all,
                   default=[i for i in sid_all if i in default_ids],
                   format_func=lambda x: skill_label(index, x),
                   key=f"pick_skills_{rev}_{int(allow_dismantle)}")
    col_a, col_b, _ = st.columns([1, 1, 3])
    if col_a.button("保存战法", type="primary", key="save_skill"):
        picked_ids = set(st.session_state.get(f"pick_skills_{rev}_{int(allow_dismantle)}") or [])
        if matched_s:
            picked_ids |= set(st.session_state.get(f"skill_search_pick_{rev}_{int(allow_dismantle)}") or [])
        merged = [i for i in sid_all if i in picked_ids]
        save_owned("user_skill", "skill_id", merged, user_key)
        st.session_state["owned_rev"] = rev + 1
        st.toast(f"已保存 {len(merged)} 个战法")
        st.rerun()
    if col_b.button("全选 S/A", key="all_sa"):
        picked = [s["skill_id"] for s in index.skills if (s.get("quality") or "") in ("S", "A")]
        save_owned("user_skill", "skill_id", picked, user_key)
        st.session_state["owned_rev"] = rev + 1
        st.rerun()


# ---------------------------------------------------------------- tab3
with tab3:
    st.subheader("配将界面（复刻主城：底部武将画像 → 拖入大营 / 中军 / 前锋）")

    def _board_members(state: dict) -> list[dict]:
        slots = (state or {}).get("slots") or {}
        out = []
        for p in core.POSITIONS:
            m = slots.get(p) or {}
            hid = m.get("hero_id")
            sks = list(m.get("skills") or []) + [None, None]
            out.append({"position": p,
                        "hero": index.hero_by_id.get(hid) if hid else None,
                        "hero_id": hid,
                        "skill1_id": sks[0], "skill2_id": sks[1],
                        "skills": [index.skill_by_id.get(sks[0]) if sks[0] else None,
                                   index.skill_by_id.get(sks[1]) if sks[1] else None]})
        return out

    board_limit = float(settings.get("cost_limit") or 10.0)
    try:
        if ui_board.available():
            board_state = st.session_state.get("board_state") or ui_board.empty_state("我的队伍1")
            board_val = ui_board.render(conn, index, board_state, board_limit,
                                        owned_heroes, owned_skills, key="board")
            if isinstance(board_val, dict) and board_val and board_val != board_state:
                st.session_state["board_state"] = board_val
                st.rerun()
            st.caption("当前阵容：" + ui_board.summarize(board_state, index))
            bms = _board_members(board_state)
            bv = core.validate(bms, index, board_limit)
            if bv.errors:
                st.warning("；".join(e["text"] for e in bv.errors))
            elif bv.warnings:
                st.info("；".join(w["text"] for w in bv.warnings[:3]))
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("保存这支队伍", type="primary", key="board_save"):
                filled = [m["hero_id"] for m in bms if m["hero_id"]]
                dups = [h for h in dict.fromkeys(filled) if filled.count(h) > 1]
                if len(filled) != 3:
                    st.warning("三个位置都要放武将")
                elif dups:
                    st.warning("武将重复：" + "、".join(hero_name(index, h) for h in dups))
                else:
                    try:
                        engine.save_lineup(conn, board_state.get("name") or "我的队伍", bms, source="user",
                                           user_key=user_key)
                    except Exception as exc:
                        st.error(f"保存失败：{exc}")
                    else:
                        st.toast(f"已保存：{board_state.get('name')}")
                        st.rerun()
            if b2.button("清空阵容", key="board_clear"):
                st.session_state["board_state"] = ui_board.empty_state(board_state.get("name") or "我的队伍1")
                st.rerun()
        else:
            st.info("拖拽组件不可用，已回落到下方下拉框录入。" + (ui_board.error() or ""))
    except Exception as exc:  # 组件异常不能把整页打成白屏
        st.error(f"配将组件异常，请用下方备用界面：{exc}")

    st.divider()
    st.caption("备用：表格录入（无拖拽，批量微调用；默认收起，点开才占地方）")
    for slot in (1, 2, 3):
        with st.expander(f"队伍 {slot}", expanded=False):
            name = st.text_input("队伍名", value=f"我的队伍{slot}", key=f"lname{slot}")
            cols = st.columns(3)
            members: list[dict] = []
            for i, pos in enumerate(core.POSITIONS):
                with cols[i]:
                    st.markdown(f"**{pos}**")
                    hid = st.selectbox("武将", [h["hero_id"] for h in index.heroes],
                                       index=None, format_func=lambda x: hero_label(index, x),
                                       key=f"l{slot}_h{i}", placeholder="选择武将")
                    hero = index.hero_by_id.get(hid) if hid else None
                    opts = skill_ids_for(index, hero)
                    s1 = st.selectbox("战法1", opts, index=None, key=f"l{slot}_s{i}1",
                                      format_func=lambda x: skill_label(index, x), placeholder="选择战法")
                    s2 = st.selectbox("战法2", opts, index=None, key=f"l{slot}_s{i}2",
                                      format_func=lambda x: skill_label(index, x), placeholder="选择战法")
                    members.append({"position": pos, "hero_id": hid, "skill1_id": s1, "skill2_id": s2})
            b1, b2 = st.columns(2)
            if b1.button(f"保存队伍 {slot}", key=f"saveline{slot}", type="primary"):
                # 先校验再落库：三个位置缺人/同一武将占两位都会撞 schema 约束，
                # 旧版直接调 save_lineup，异常一路冒到页面顶部（或整页白屏）。
                picked_heroes = [m["hero_id"] for m in members if m.get("hero_id")]
                dups = [hid for hid in dict.fromkeys(picked_heroes) if picked_heroes.count(hid) > 1]
                if len(picked_heroes) != 3:
                    st.warning("有三个位置都要选武将")
                elif dups:
                    st.warning("武将重复：" + "、".join(hero_name(index, h) for h in dups)
                               + "　同一武将不能占两个位置")
                else:
                    try:
                        engine.save_lineup(conn, name, members, source="user", user_key=user_key)
                    except Exception as exc:
                        st.error(f"保存失败：{exc}（可能缺武将或同一武将重复）")
                    else:
                        st.toast(f"已保存：{name}")
                        st.rerun()

    existing = conn.execute("SELECT lineup_id, name, created_at FROM lineup WHERE user_key=? AND source='user' "
                            "ORDER BY lineup_id", (user_key,)).fetchall()
    if existing:
        st.markdown("#### 已保存的队伍")
        for r in existing:
            ms = engine.lineup_members(conn, r["lineup_id"])
            txt = " / ".join(f"{pos}:{hero_name(index, (m or {}).get('hero_id'))}"
                             for pos, m in zip(core.POSITIONS, ms))
            st.write(f"- **{r['name']}**　{txt}")
            if st.button("删除", key=f"del{r['lineup_id']}"):
                conn.execute("DELETE FROM lineup WHERE lineup_id=? AND user_key=?", (r["lineup_id"], user_key))
                conn.commit()
                st.rerun()


# ---------------------------------------------------------------- tab4
with tab4:
    st.subheader("推荐结果")
    q1, q2, q3, q4 = st.columns(4)
    cost_limit = q1.number_input("队伍统率上限", 1.0, 15.0, float(settings.get("cost_limit") or 10.0), 0.5)
    cur_target = settings.get("target_mode") or "打地"
    target_idx = core.TARGET_MODES.index(cur_target) if cur_target in core.TARGET_MODES else 0
    target = q2.selectbox("适用目标", core.TARGET_MODES, index=target_idx,
                          help="打地/野战/攻城/守城：权重不同，见「⑤ 数据与规则」")
    # 下拉框默认值必须读库里的 budget_profile：写死 index=0 时，库里是 deep 也永远显示 A 档，
    # 用户以为跑的是全档，实际拿的是快档预算。
    budget_labels = ["A 快（≤2 秒）", "B 全（≤30 秒）"]
    budget_idx = 0 if (settings.get("budget_profile") or "fast") == "fast" else 1
    budget = q3.selectbox("搜索预算（5-2 双档）", budget_labels, index=budget_idx,
                          key="budget_pick", on_change=_on_budget_profile,
                          help="A 快 = 每队 20 个变体 / 全局上限 300；B 全 = 每队 200 / 全局上限 3000")
    # 先夹到滑杆区间再传：库里是 3 时直接把 3 当默认值，Streamlit 会把区间静默扩成 3–10。
    n0 = max(5, min(10, int(settings.get("top_n") or 10)))
    top_n = q4.slider("输出条数", 5, 10, n0)

    seeds = load_manual_seeds(user_key)
    if seeds:
        st.success(f"使用你录入的 {len(seeds)} 支队伍作为种子")
    else:
        st.info("还没录入队伍：将用数据里的真实阵容当种子演示（结果仅供形式预览）。")

    if st.button("开始推荐", type="primary", key="run"):
        dbmod.set_settings(conn, user_key, top_n=top_n, cost_limit=cost_limit, target_mode=target,
                           budget_profile="deep" if budget.startswith("B") else "fast")
        if not seeds:
            seeds = cli.demo_seeds(conn, index, 2)
        with st.spinner("校验 + 变体 + 评分 …"):
            payload = engine.recommend(conn, seeds, user_key=user_key)
        st.session_state["payload"] = payload

    payload = st.session_state.get("payload")
    if payload:
        st.caption(f"目标 {payload['target']}｜候选 {payload['candidates']} 支｜"
                   f"run_id={payload['run_id']}｜预算 {payload['budget']}")
        if payload.get("truncated"):
            st.caption("已达预算上限，候选被截断")
        # 离线版默认就是全库评估（不要求先勾持仓），所以这里只作事实说明，不当异常警告。
        fb = payload.get("pool_fallback") or {}
        if fb.get("heroes") and fb.get("skills"):
            st.caption(f"按全库评估：{len(index.heroes)} 将 / {len(index.skill_by_id)} 战法（未用常用清单）。")
        elif fb.get("heroes"):
            st.caption(f"武将按全库 {len(index.heroes)} 位评估；战法用了你的常用清单。")
        elif fb.get("skills"):
            st.caption(f"战法按全库 {len(index.skill_by_id)} 个评估；武将用了你的常用清单。")
        for r in payload["results"]:
            head = (f"#{r['rank']}　{r['total']:.1f} 分　"
                    f"{' / '.join(member_hero_name(m) for m in r['members'])}　"
                    f"（{r['legality']}）")
            with st.expander(head, expanded=(r["rank"] <= 3)):
                k1, k2, k3, k4 = st.columns(4)
                k1.metric("总分", f"{r['total']:.1f}")
                k2.metric("系统强度", f"{r['system']:.1f}")
                k3.metric("适用性", f"{r['applicability']:.1f}")
                # 判定看 verdict（ok/tune/no/invalid），legality 只是校验三态（pass/warn/fail）
                k4.metric("判定", VERDICT_LABEL.get(r["verdict"], r["verdict"]),
                          help=r["verdict_reason"])

                for m in r["members"]:
                    if not m:
                        continue
                    hero = member_hero(m)
                    sks = "、".join(s["name"] for s in (m.get("skills") or []) if s)
                    if hero:  # 正常路径
                        st.markdown(f"**{m['position']}**　{hero['name']}　"
                                    f"（{hero.get('camp') or '?'}{hero.get('troop') or '?'}·"
                                    f"cost {core.num(hero.get('cost'))}）　战法：{sks}")
                    else:
                        # 失效武将：库里还留着 hero_id，但 hero 表里已经没有这个人了。
                        # 只提示、不抛异常，也不假装是「空位」。
                        st.markdown(f"**{m.get('position') or '?'}**　{member_hero_name(m)}　"
                                    f"（武将数据已缺失，本条队伍无法参与评分）　战法：{sks}")

                if r["battle"]:
                    b = r["battle"]
                    # 只改展示：低样本（battle_stat.sample_flag=low_sample，即 games<5）提示一句，
                    # 评分与排序完全不受影响。
                    low = b.get("sample_flag") == "low_sample"
                    st.caption(f"战报：{b['games']} 场，胜率 {((b['win_rate'] or 0) * 100):.1f}%（{b['sample_flag']}）"
                               + (f"　样本不足，仅供参考（{b['games']} 场）" if low else ""))
                st.markdown("**推荐理由**")
                for reason in r["reasons"]:
                    st.write(f"- [{reason['kind']}] {reason['text']}")
                    if reason["kind"] == "community":
                        # 社区摘录必须给得出处（title/url 在 basis 里），否则无法回原文核对
                        b = reason.get("basis") or {}
                        st.caption(f"　出处：{b.get('title') or '（无标题）'} · {b.get('url') or '（无链接）'}")
                if r["counters"]:
                    st.markdown("**克制关系**")
                    for c in r["counters"]:
                        arrow = "克制" if c["direction"] == "we_counter" else "被克制"
                        st.write(f"- {arrow}：{c['target_desc']}（{c['counter_type']}）")
                if r["substitutes"]:
                    st.markdown("**替补方案**")
                    for s in r["substitutes"]:
                        st.write(f"- [{s['kind']}] {s['reason']}")
                if r["findings"]:
                    st.markdown("**校验提示**")
                    for f in r["findings"]:
                        st.write(f"- ({f.get('level')}) {f.get('text')}")


# ---------------------------------------------------------------- tab5
with tab5:
    st.subheader("数据来源")
    for r in conn.execute("SELECT file_path, rows, cols, imported_at FROM data_source "
                          "ORDER BY file_path"):
        st.write(f"- `{r['file_path']}`　{r['rows'] or '?'} 行 / {r['cols'] or '?'} 列　{r['imported_at'] or ''}")
    st.subheader("规则参数与状态")
    st.caption("状态含义：ok=已确认；missing_source=数据源缺失；unconfirmed=待你确认；not_applicable=不适用")
    params = conn.execute("SELECT param_key, value_json, data_status, note FROM rule_param "
                          "ORDER BY param_key").fetchall()
    # Streamlit 1.66 起 use_container_width 已弃用（会在日志里刷 warning），改用 width="stretch"
    st.dataframe([dict(p) for p in params], width="stretch", hide_index=True)
    st.subheader("评分权重（读自 score_weight 表）")
    # 引擎真正用的是 score_weight / scoring_profile 表（engine.load_weights）；
    # 这里以前显示 db.py 里的常量，caption 却说「可改、立即生效」——表改了页面纹丝不动。
    prof = conn.execute("SELECT profile_id, merge_system, merge_applicability FROM scoring_profile "
                        "WHERE is_active=1").fetchone()
    pid = prof["profile_id"] if prof else dbmod.PROFILE_ID
    sys_w: dict[str, float] = {}
    tgt_w: dict[str, dict[str, float]] = {}
    for r in conn.execute("SELECT scope, target_mode, dim_code, weight FROM score_weight "
                          "WHERE profile_id=? ORDER BY scope, target_mode, dim_code", (pid,)):
        if r["scope"] == "system":
            sys_w[r["dim_code"]] = r["weight"]
        else:
            tgt_w.setdefault(r["target_mode"], {})[r["dim_code"]] = r["weight"]
    st.json({
        "profile": pid,
        "system": sys_w,
        "combat": tgt_w,
        "final": {"system": prof["merge_system"] if prof else dbmod.FINAL_WEIGHTS["system"],
                  "combat": prof["merge_applicability"] if prof else dbmod.FINAL_WEIGHTS["combat"]},
        "tradeoffs": {"cost_limit": cost_limit},
    })
    st.caption("上表来自库表 score_weight / scoring_profile，不是代码常量；改表后刷新页面即生效。")
    st.caption("「允许拆解」（user_settings.allow_dismantle）是输入口径：打开时 ② 页把武将的可拆战法"
               "默认算作已拥有，保存后进入我的战法池；它不参与任何评分公式。")
    st.caption("B1：S1 赛季白名单尚未导入（CSV 无赛季字段），当前为全量池，未做赛季过滤。")
