"""命令行入口。

    python -m stzb_app.cli --init             # 建库 + 导入
    python -m stzb_app.cli --rebuild          # 删库重建后导入
    python -m stzb_app.cli --selftest         # 自检：表数/样例队伍/端到端跑一次
    python -m stzb_app.cli --demo --top 10    # 用数据里的真实阵容当种子跑推荐
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core
from . import db as dbmod
from . import engine
from . import ingest


def ensure_data(conn):
    """数据库为空时先导入，导入后返回新连接（保证 --selftest 一次跑完）。"""
    n = conn.execute("SELECT COUNT(*) c FROM hero").fetchone()["c"]
    if n == 0:
        print("数据库为空，先导入数据 …")
        conn.close()
        ingest.run(rebuild=False)
        return dbmod.connect()
    return conn


def demo_seeds(conn, index: core.Index, limit: int = 3) -> list[dict]:
    """从社区阵容里取代价最低的真实三人组当种子（保证能跑）。"""
    seeds: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT message_id, hero_ids_json, skill_ids_json, likes FROM evidence_doc "
            "WHERE json_valid(hero_ids_json) AND json_array_length(hero_ids_json)=3 "
            "AND json_valid(skill_ids_json) AND json_array_length(skill_ids_json)>=6 "
            "ORDER BY likes DESC LIMIT 40"
        ).fetchall()
    except Exception:
        rows = []
    for r in rows:
        if len(seeds) >= limit:
            break
        hero_ids = json.loads(r["hero_ids_json"])
        skill_ids = json.loads(r["skill_ids_json"])[:6]
        if any(h not in index.hero_by_id for h in hero_ids) or any(s not in index.skill_by_id for s in skill_ids):
            continue
        total_cost = sum(core.num(index.hero_by_id[h].get("cost")) or 0 for h in hero_ids)
        if total_cost > 10.0:
            continue
        members = []
        for i, pos in enumerate(core.POSITIONS):
            members.append({"position": pos, "hero_id": hero_ids[i],
                            "skill1_id": skill_ids[i * 2], "skill2_id": skill_ids[i * 2 + 1]})
        seeds.append({"name": f"社区阵容{r['message_id'][-6:]}", "members": members, "source": "manual"})
    if seeds:
        return seeds
    # 兜底：挑三只 5 星且总 cost ≤ 10
    heroes = sorted(index.heroes, key=lambda h: (h.get("star") or 0, -(core.num(h.get("cost")) or 0)), reverse=True)
    picked = []
    for h in heroes:
        if len(picked) >= 3:
            break
        if sum(core.num(x.get("cost")) or 0 for x in picked) + (core.num(h.get("cost")) or 0) > 10.0:
            continue
        picked.append(h)
    members = []
    for i, (pos, h) in enumerate(zip(core.POSITIONS, picked)):
        sks = [index.skill_by_id.get(h.get("innate_skill_id"))]
        for s in index.skills:
            if len(sks) >= 2:
                break
            if s["skill_id"] not in {x["skill_id"] for x in sks if x}:
                sks.append(s)
        members.append({"position": pos, "hero_id": h["hero_id"],
                        "skill1_id": sks[0]["skill_id"] if sks[0] else None,
                        "skill2_id": sks[1]["skill_id"] if len(sks) > 1 and sks[1] else None})
    return [{"name": "兜底阵容", "members": members, "source": "manual"}]


def render(payload: dict) -> str:
    lines = [f"目标：{payload['target']}   候选 {payload['candidates']} 支   预算 {payload['budget']}"]
    lines.append("=" * 78)
    for r in payload["results"]:
        heroes = " / ".join(
            f"{m['hero']['name']}({m['position']}·{m['hero'].get('troop')}·cost {core.num(m['hero'].get('cost'))})"
            for m in r["members"] if m
        )
        lines.append(f"#{r['rank']:>2}  {r['total']:>6.1f} 分  系统 {r['system']:.1f} + 适用 {r['applicability']:.1f}  "
                     f"[{r['legality']}] {r['verdict_reason']}")
        lines.append(f"    {heroes}")
        for m in r["members"]:
            if not m:
                continue
            sks = "、".join(s["name"] for s in (m.get("skills") or []) if s)
            lines.append(f"      {m['position']}：{m['hero']['name']}  战法 {sks}")
        if r["battle"]:
            lines.append(f"      战报：{r['battle']['games']} 场，胜率 {(r['battle']['win_rate'] or 0) * 100:.1f}%"
                         f"（{r['battle']['sample_flag']}）")
        for reason in r["reasons"][:6]:
            lines.append(f"      · [{reason['kind']}] {reason['text']}")
        for c in r["counters"]:
            arrow = "克制" if c["direction"] == "we_counter" else "被克制"
            lines.append(f"      · 关系：{arrow} {c['target_desc']}（{c['counter_type']}）")
        for s in r["substitutes"]:
            lines.append(f"      · 替补[{s['kind']}] {s['reason']}")
        if r["findings"]:
            for f in r["findings"][:3]:
                lines.append(f"      · 校验[{f.get('level')}] {f.get('text')}")
        lines.append("-" * 78)
    return "\n".join(lines)


def cmd_selftest(conn, target: str | None = None, budget: str = "fast", top: int = 5) -> int:
    index = engine.load_index(conn)
    print("== 数据自检 ==")
    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("hero", "hero_attr", "skill", "combo", "evidence_doc", "battle_stat",
                        "user_hero", "user_skill", "lineup", "eval_run")}
    print("表行数:", counts)
    missing = [h["name"] for h in index.heroes if not h.get("innate_skill_id")]
    print(f"缺自带战法解析的武将: {len(missing)}")
    bad_combos = conn.execute("SELECT COUNT(*) c FROM combo WHERE hero_ids_json IN ('[]','')").fetchone()["c"]
    print(f"空组合: {bad_combos}")
    print(f"索引规模: heroes={len(index.heroes)} skills={len(index.skill_by_id)} combos={len(index.combos)}")

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    check("hero 表 391 行", counts["hero"] == 391, f"实际 {counts['hero']}")
    check("skill 表 636 行", counts["skill"] == 636, f"实际 {counts['skill']}")
    check("缺自带战法的武将为 0", len(missing) == 0, f"实际 {len(missing)}")
    check("索引 heroes=391", len(index.heroes) == 391, f"实际 {len(index.heroes)}")

    seeds = demo_seeds(conn, index, 2)
    print(f"\n== 端到端：{len(seeds)} 支种子队伍 ==")
    # 自检会改 top_n/budget/target 来跑端到端，结束后必须还原，
    # 否则「跑一次自检就把用户 ④ 页的输出条数/目标模式改了」。
    orig = dbmod.get_settings(conn)
    try:
        dbmod.set_settings(conn, top_n=top, budget_profile=budget,
                           **({"target_mode": target} if target else {}))
        payload = engine.recommend(conn, seeds)
    finally:
        dbmod.set_settings(conn, top_n=orig.get("top_n", 10),
                           budget_profile=orig.get("budget_profile", "fast"),
                           target_mode=orig.get("target_mode", "打地"))
    print(render(payload))
    print(f"run_id={payload['run_id']}")

    results = payload.get("results") or []
    check("端到端 results 非空", bool(results), f"实际 {len(results)} 条")
    first_total = float(results[0]["total"]) if results else 0.0
    check("第一名总分在 50~100", 50.0 <= first_total <= 100.0, f"实际 {first_total:.1f}")

    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}（{detail}）")
    failed = [name for name, ok, _ in checks if not ok]
    if failed:
        print(f"[FAIL] 自检 {len(failed)}/{len(checks)} 项未通过：{'、'.join(failed)}")
        return 1
    print(f"[PASS] 自检 {len(checks)} 项全部通过")
    return 0


def cmd_demo(conn, top: int, target: str | None = None, budget: str = "fast") -> int:
    index = engine.load_index(conn)
    seeds = demo_seeds(conn, index, 2)
    # demo 和 selftest 一样会改 top_n/budget/target 来跑推荐，结束后必须还原：
    # 否则 `--demo --top 3` 会把 ④ 页的输出条数永久改成 3（滑杆静默扩区间）。
    orig = dbmod.get_settings(conn)
    try:
        dbmod.set_settings(conn, top_n=top, budget_profile=budget,
                           **({"target_mode": target} if target else {}))
        payload = engine.recommend(conn, seeds)
    finally:
        dbmod.set_settings(conn, top_n=orig.get("top_n", 10),
                           budget_profile=orig.get("budget_profile", "fast"),
                           target_mode=orig.get("target_mode", "打地"))
    print(render(payload))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="率土之滨配将推荐（规则引擎版）")
    ap.add_argument("--init", action="store_true", help="建表 + 导入（幂等）")
    ap.add_argument("--rebuild", action="store_true", help="删库重建 + 导入")
    ap.add_argument("--selftest", action="store_true", help="自检 + 端到端跑一次")
    ap.add_argument("--demo", action="store_true", help="用社区真实阵容当种子跑推荐")
    ap.add_argument("--top", type=int, default=None, help="输出条数（selftest 默认 5，demo 默认 10）")
    ap.add_argument("--target", choices=core.TARGET_MODES, help="适用目标，默认用库里的设置")
    ap.add_argument("--budget", choices=["fast", "deep"], default="fast", help="5-2 双档：fast= A 档，deep= B 档")
    args = ap.parse_args()

    if args.rebuild:
        ingest.run(rebuild=True)
        return 0
    if args.init:
        conn = dbmod.connect()
        dbmod.init_db(conn)
        print("数据库已初始化:", dbmod.DB_PATH)
        conn.close()
        return 0

    conn = dbmod.connect()
    dbmod.init_db(conn)
    conn = ensure_data(conn)
    if args.selftest:
        return cmd_selftest(conn, args.target, args.budget, args.top or 5)
    if args.demo:
        return cmd_demo(conn, args.top or 10, args.target, args.budget)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
