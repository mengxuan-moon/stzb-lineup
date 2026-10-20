"""界面冒烟测试（streamlit.testing.v1.AppTest）。

    python -m stzb_app.apptest_smoke      # 在 E:\\dsh 下执行

覆盖：
  (a) 首轮无异常 + 标题 + 两个检索框存在
  (b) 搜「孙坚」= 4 项
  (c) 搜不存在的名称 → warning 且输入保留
  (d) 清空检索 → 恢复 391 项完整列表
  (e) 清空 pick_heroes 后选 1 个保存 → 我的武将=1；重复保存仍=1
  (f) 清空两个检索框后点「开始推荐」→ expander ≥ 10
  (g) 连跑 5 次 rerun 无异常
  (h) 跑完生产库 lineup / eval_run / team_score 计数与跑前一致，且 %TEMP% 副本目录已删

**测试不碰生产库**：开头用 `sqlite3.Connection.backup()` 把 data\\stzb.db 快照到
`%TEMP%\\stzb_smoke_<pid>\\stzb.db`，再把 `STZB_DB` 环境变量指向副本，AppTest 全程写副本。
旧版直接跑生产库、再按 MAX(lineup_id)/MAX(eval_run_id) 删掉本次新增的行来「还原」：
只要中途崩溃或删漏（比如指纹复用命中了旧 lineup），真库就留下测试垃圾，
而且「我的武将」这类被 test 改过的持仓也只能靠猜着恢复。副本用完直接删目录，
生产库连一行都不会被写。失败项逐条打印，任一失败 sys.exit(1)。
"""
from __future__ import annotations

import gc
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

# 用 `python -m stzb_app.apptest_smoke` 跑时包上下文已就绪；用 `python stzb_app/apptest_smoke.py`
# 跑时没有父包，这里兜一手（与 app.py 同样的做法）。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402  （AppTest 本身不碰数据库）

PROD_DB = _ROOT / "stzb_app" / "data" / "stzb.db"
SMOKE_DIR = Path(tempfile.gettempdir()) / f"stzb_smoke_{os.getpid()}"
COPY_DB = SMOKE_DIR / "stzb.db"


# ------------------------------------------------------------------ 副本准备 / 清理


def _make_db_copy() -> None:
    """把生产库快照成副本，并在 import stzb_app.db **之前**设好 STZB_DB。

    backup() 是 SQLite 官方的在线备份 API：WAL 里还没 checkpoint 的页也会被正确合并，
    比直接 copy 文件（漏读 -wal）安全。
    """
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(PROD_DB))
    try:
        dst = sqlite3.connect(str(COPY_DB))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    # db.py 在 import 时就算好 DB_PATH，所以环境变量必须赶在 import 之前设
    os.environ["STZB_DB"] = str(COPY_DB)


_make_db_copy()

from stzb_app import db as dbmod  # noqa: E402

# 双保险：同进程内 AppTest 复用同一个模块对象，直接改属性，免得依赖 import 顺序
dbmod.DB_PATH = COPY_DB

APP_PATH = dbmod.BASE_DIR / "app.py"


def _close_stray_connections() -> int:
    """把还开着的 sqlite 连接统统关掉。

    AppTest 的脚本线程会持有 app.py 每次 rerun 新建的连接（app 按设计不 close，靠引用计数），
    线程没退出时引用计数不归零；Windows 上文件句柄没释放，rmtree 就 PermissionError。
    """
    closed = 0
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.close()
                closed += 1
            except Exception:  # pragma: no cover - 已关闭/正在使用
                pass
    return closed


def _cleanup_copy() -> None:
    """删掉副本目录（Windows 上文件可能还被 AppTest 的连接占着，给几次机会）。"""
    for _ in range(8):
        gc.collect()
        _close_stray_connections()
        try:
            shutil.rmtree(SMOKE_DIR)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.4)
    shutil.rmtree(SMOKE_DIR, ignore_errors=True)


# ------------------------------------------------------------------ 计数


def _db_counts(conn, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) c FROM {table}" + (f" WHERE {where}" if where else "")
    return int(conn.execute(sql).fetchone()["c"])


def _prod_counts() -> dict:
    """独立连接数**生产库**：dbmod.DB_PATH 已经被指到副本，所以这里直接 sqlite3.connect。"""
    c = sqlite3.connect(str(PROD_DB))
    try:
        return {t: int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                for t in ("lineup", "eval_run", "team_score")}
    finally:
        c.close()


# ------------------------------------------------------------------ 主流程


def main() -> int:
    prod_before = _prod_counts()
    conn = dbmod.connect()
    dbmod.init_db(conn)
    hero_ids = [r["hero_id"] for r in conn.execute("SELECT hero_id FROM hero ORDER BY hero_id")]
    checks: list[tuple[str, bool, str]] = []
    ran_to_end = False

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    print(f"生产库 {PROD_DB}")
    print(f"测试副本 {COPY_DB}（测试全程只写它）")
    print(f"跑前生产库计数 {prod_before}")

    try:
        at = AppTest.from_file(str(APP_PATH), default_timeout=180)
        at.run()

        # (a) 首轮渲染
        check("(a) 首轮无异常", not at.exception, f"{len(at.exception)} 个异常")
        titles = [str(t.value) for t in at.title]
        check("(a) 标题存在", any("配将推荐" in t for t in titles), repr(titles))
        input_keys = {e.key for e in at.text_input}
        check("(a) 两个检索框存在", {"hero_search", "skill_search"} <= input_keys,
              f"实际 key={sorted(k for k in input_keys if k)}")

        # (b) 检索「孙坚」：库里 4 个同名多形态
        at.text_input(key="hero_search").input("孙坚").run()
        check("(b) 检索无异常", not at.exception, f"{len(at.exception)} 个异常")
        opts = list(at.multiselect(key="hero_search_pick_0").options)
        check("(b) 搜「孙坚」=4 项", len(opts) == 4, f"实际 {len(opts)} 项")

        # (c) 无匹配：warning + 输入保留
        at.text_input(key="hero_search").input("不存在的武将zzz").run()
        warns = [str(w.value) for w in at.warning]
        check("(c) 无匹配时给 warning", any("未找到匹配项" in w for w in warns),
              repr(warns[:2]))
        check("(c) 输入保留", at.text_input(key="hero_search").value == "不存在的武将zzz",
              repr(at.text_input(key="hero_search").value))

        # (d) 清空 → 恢复完整列表
        at.text_input(key="hero_search").input("").run()
        n_all = len(list(at.multiselect(key="pick_heroes_0").options))
        check("(d) 清空检索恢复 391 项", n_all == 391, f"实际 {n_all} 项")

        # (e) 清空 → 选 1 个 → 保存 → 我的武将=1；重复保存仍=1
        at.multiselect(key="pick_heroes_0").set_value([]).run()
        check("(e) 清空 pick_heroes 无异常", not at.exception, f"{len(at.exception)} 个异常")
        at.multiselect(key="pick_heroes_0").set_value([hero_ids[0]]).run()
        at.button(key="save_hero").click().run()
        check("(e) 保存无异常", not at.exception, f"{len(at.exception)} 个异常")
        n_owned = _db_counts(conn, "user_hero", "owned=1")
        check("(e) 保存后 我的武将=1", n_owned == 1, f"实际 {n_owned}")
        at.button(key="save_hero").click().run()
        n_owned2 = _db_counts(conn, "user_hero", "owned=1")
        check("(e) 重复保存仍=1", n_owned2 == 1, f"实际 {n_owned2}")

        # (f) 清空检索框 → 10 条 → 开始推荐 → expander ≥ 10
        at.text_input(key="hero_search").input("").run()
        at.text_input(key="skill_search").input("").run()
        at.slider[0].set_value(10).run()
        at.button(key="run").click().run()
        check("(f) 推荐无异常", not at.exception, f"{len(at.exception)} 个异常")
        n_exp = len(at.expander)
        check("(f) expander ≥ 10", n_exp >= 10, f"实际 {n_exp} 个")

        # (g) 连跑 5 次
        for i in range(5):
            at.run()
            check(f"(g) 第 {i + 1} 次 rerun 无异常", not at.exception, f"{len(at.exception)} 个异常")

        ran_to_end = True
    except BaseException:
        traceback.print_exc()
        check("用例执行过程中抛异常", False, "见上方 traceback（不吞异常）")
    finally:
        try:
            end_owned = (_db_counts(conn, "user_hero", "owned=1"),
                         _db_counts(conn, "user_skill", "owned=1"))
            conn.close()
        except BaseException:
            traceback.print_exc()
            check("关闭副本连接失败", False, "见上方 traceback")
        at = None  # 放掉 AppTest：它持有 app 脚本模块里的 sqlite 连接，不放就删不掉副本文件
        _cleanup_copy()

    # 收尾断言：生产库一行都不能变（跑测试前它已被完整快照过），副本目录也必须删干净
    prod_after = _prod_counts()
    check("(h) 生产库计数不变且副本已删",
          prod_after == prod_before and not SMOKE_DIR.exists(),
          f"跑前 {prod_before} / 跑后 {prod_after} / 副本目录存在={SMOKE_DIR.exists()}")

    failed = [(n, d) for n, ok, d in checks if not ok]
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"　{detail}" if detail else ""))
    print(f"\n结束状态（副本内）：user_hero owned={end_owned[0]} / user_skill owned={end_owned[1]}")
    print(f"生产库计数：跑前 {prod_before} → 跑后 {prod_after}")
    print(f"副本目录：{SMOKE_DIR}（存在于磁盘={SMOKE_DIR.exists()}）")
    print(f"共 {len(checks)} 项：通过 {len(checks) - len(failed)} / 失败 {len(failed)}")

    if failed or not ran_to_end:
        print("[FAIL] 冒烟测试未通过：" + "、".join(n for n, _ in failed))
        return 1
    print("[PASS] 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
