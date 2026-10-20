"""武将立绘：联网抓取 → 本地缓存 ``components/lineup_board/img/<武将ID>.jpg``。

v1 默认离线：没有图就用文字占位卡面（``index.html`` 里 ``onerror`` 回落）。
只有在用户显式允许联网后跑这个模块，产物是**本地文件 + hero_portrait 一行**，
之后界面只读文件，不再联网。

抓取口径（2026-10 实测，来自官方卡片图 CDN）：

- 图片地址不是「武将ID 直接拼」，而是 ``card_medium_{iconId}.jpg``；
  ``iconId`` 要从 ``hero_extra.json`` 取（391 个武将里有 57 个 iconId ≠ 武将ID）。
- 240×348 JPEG 是公开最大尺寸（card_large 等全部 404，404 返回的是 XML 错误体，
  所以落盘前必须校验 JPEG 魔数 ``\\xff\\xd8`` 且体积 > 10KB）。
- 站点不校验 UA/Referer，8 线程抓 391 张约 2 秒，无 429。仍然默认 limit=6 线程、
  失败重试 3 次（0.5/1.5/3s），并从 ``card_medium`` 退到 ``card_small``。

用法::

    python -m stzb_app.portraits --report
    python -m stzb_app.portraits --all
    python -m stzb_app.portraits --limit 20
    python -m stzb_app.portraits --import-dir E:\\dsh\\_tmp_portrait\\all   # 导入已下好的图
"""
from __future__ import annotations

import json
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import db as dbmod

BASE_DIR = Path(__file__).resolve().parent
IMG_DIR = BASE_DIR / "components" / "lineup_board" / "img"
CACHE_DIR = BASE_DIR / "data"
MAPPING_FILE = CACHE_DIR / "hero_extra.json"
MAPPING_URL = ("https://g0.gph.netease.com/ngsocial/community/stzb/cfg/"
               "hero_extra.json?gameid=g10")
IMG_TMPL = ("https://g0.gph.netease.com/ngsocial/community/stzb/cn/cards/cut/"
            "card_medium_{icon}.jpg?gameid=g10")
SMALL_TMPL = ("https://g0.gph.netease.com/ngsocial/community/stzb/cn/cards/cut/"
              "card_small_{icon}.jpg?gameid=g10")
DETAIL_TMPL = "https://stzb.163.com/herolist/{hid}.html"

MIN_BYTES = 10_000     # < 10KB 基本都是 404 的 XML 错误体
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get(url: str, timeout: int = 20) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 固定 https 站点
        return int(getattr(resp, "status", 200)), resp.read(4 << 20)


def load_icon_map(force: bool = False) -> dict[str, str]:
    """武将ID → iconId。缓存到 data/hero_extra.json，只联网一次。"""
    if MAPPING_FILE.exists() and not force:
        raw = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
    else:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        status, body = _get(MAPPING_URL)
        if status != 200 or not body:
            raise RuntimeError(f"hero_extra.json HTTP {status}")
        MAPPING_FILE.write_bytes(body)
        raw = json.loads(body.decode("utf-8", "ignore"))
    items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("heroes") or [])
    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        hid, icon = it.get("id"), it.get("iconId")
        if hid is None:
            continue
        out[str(hid)] = str(icon if icon is not None else hid)
    return out


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" and len(data) >= MIN_BYTES


def download_one(hero_id: str, icon_id: str, dest_dir: Path) -> dict:
    """下载一名武将的立绘，返回 {ok, bytes, url} 或 {ok:False, reason}。"""
    dest = dest_dir / f"{hero_id}.jpg"
    for attempt in range(3):
        for tmpl in (IMG_TMPL, SMALL_TMPL):
            url = tmpl.format(icon=icon_id)
            try:
                status, data = _get(url)
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            if status == 200 and _is_jpeg(data):
                dest.write_bytes(data)
                return {"ok": True, "bytes": len(data), "url": url}
            last = f"HTTP {status} / {len(data)}B / 非 JPEG"
        time.sleep(0.5 * (2 ** attempt))
    return {"ok": False, "reason": last}


def _upsert(conn, hero_id: str, file_name: str, url: str, nbytes: int) -> None:
    w, h = (240, 348) if "medium" in url else (0, 0)
    conn.execute(
        "INSERT INTO hero_portrait(hero_id,file_name,source_url,bytes,width,height,fetched_at,status,note) "
        "VALUES(?,?,?,?,?,?,?,'ok',NULL) ON CONFLICT(hero_id) DO UPDATE SET "
        "file_name=excluded.file_name, source_url=excluded.source_url, bytes=excluded.bytes, "
        "width=excluded.width, height=excluded.height, fetched_at=excluded.fetched_at, "
        "status='ok', note=NULL",
        (hero_id, file_name, url, nbytes, w, h, _now()))


def register_local(conn, src_dir: Path | None = None) -> dict:
    """把已有的图片文件登记进库（不联网）。src_dir 给了就先拷进 IMG_DIR。"""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    if src_dir and src_dir.exists():
        for p in src_dir.glob("*"):
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                dst = IMG_DIR / f"{p.stem}.jpg"
                if not dst.exists() or dst.stat().st_size < MIN_BYTES:
                    shutil.copyfile(p, dst)
                    copied += 1
    heroes = [r["hero_id"] for r in conn.execute("SELECT hero_id FROM hero")]
    ok = miss = 0
    for hid in heroes:
        f = IMG_DIR / f"{hid}.jpg"
        if f.exists() and f.stat().st_size >= MIN_BYTES:
            _upsert(conn, hid, f.name, IMG_TMPL.format(icon="?"), f.stat().st_size)
            ok += 1
        else:
            miss += 1
    conn.commit()
    return {"heroes": len(heroes), "registered": ok, "missing": miss, "copied": copied}


def run(limit: int | None = None, force: bool = False, workers: int = 6,
        conn=None, verbose: bool = True) -> dict:
    """联网抓取；已存在且 >10KB 的文件默认跳过（可断点续传）。"""
    own = conn is None
    conn = conn or dbmod.connect()
    try:
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        icon_map = load_icon_map()
        heroes = [r["hero_id"] for r in conn.execute("SELECT hero_id FROM hero ORDER BY hero_id")]
        todo = []
        for hid in heroes:
            f = IMG_DIR / f"{hid}.jpg"
            if not force and f.exists() and f.stat().st_size >= MIN_BYTES:
                continue
            todo.append(hid)
        if limit:
            todo = todo[:limit]
        stats = {"heroes": len(heroes), "todo": len(todo), "ok": 0, "failed": 0,
                 "no_icon": 0, "mapped_diff": 0}
        if verbose:
            print(f"武将 {len(heroes)}，已有 {len(heroes) - len(todo)}，本轮抓 {len(todo)}")

        def work(hid: str):
            icon = icon_map.get(hid)
            if not icon:
                return hid, {"ok": False, "reason": "hero_extra.json 里没有 iconId", "no_icon": True}
            if icon != hid:
                stats["mapped_diff"] += 1
            return hid, download_one(hid, icon, IMG_DIR)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (hid, res) in enumerate(pool.map(work, todo), 1):
                if res.get("ok"):
                    _upsert(conn, hid, f"{hid}.jpg", res["url"], res["bytes"])
                    stats["ok"] += 1
                else:
                    stats["failed"] += 1
                    if res.get("no_icon"):
                        stats["no_icon"] += 1
                    conn.execute(
                        "INSERT INTO hero_portrait(hero_id,file_name,source_url,fetched_at,status,note) "
                        "VALUES(?,?,?,?,'failed',?) ON CONFLICT(hero_id) DO UPDATE SET "
                        "fetched_at=excluded.fetched_at, status='failed', note=excluded.note",
                        (hid, "", DETAIL_TMPL.format(hid=hid), _now(), res.get("reason") or ""))
                if i % 25 == 0 or i == len(todo):
                    conn.commit()
                    if verbose:
                        print(f"  {i}/{len(todo)} ok={stats['ok']} failed={stats['failed']}")
        conn.commit()
        return stats
    finally:
        if own:
            conn.close()


def report(conn=None) -> dict:
    """现状统计（不联网）。"""
    own = conn is None
    conn = conn or dbmod.connect()
    try:
        rows = conn.execute("SELECT status, COUNT(*) n FROM hero_portrait GROUP BY status").fetchall()
        by = {r["status"]: r["n"] for r in rows}
        total = conn.execute("SELECT COUNT(*) n FROM hero").fetchone()["n"]
        size = sum(p.stat().st_size for p in IMG_DIR.glob("*") if p.is_file()) if IMG_DIR.exists() else 0
        return {"hero_total": total, "img_dir_mb": round(size / 1048576, 2), **by}
    finally:
        if own:
            conn.close()


def img_map(conn) -> dict[str, str]:
    """hero_id → 文件名，给界面用。"""
    return {r["hero_id"]: r["file_name"] for r in conn.execute(
        "SELECT hero_id, file_name FROM hero_portrait WHERE status='ok' AND file_name<>''")}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    import sys
    args = list(sys.argv[1:] if argv is None else argv)

    def opt(name: str, default: str | None = None) -> str | None:
        return args[args.index(name) + 1] if name in args else default

    conn = dbmod.connect()
    try:
        # CLI 也要先建表/迁移：否则在老库（user_version<4、还没有 hero_portrait）上
        # 直接跑 --import-dir 会崩在 `no such table: hero_portrait`。
        # 这个坑是验证代理实测出来的，别删。
        dbmod.init_db(conn)
        if "--report" in args:
            print(json.dumps(report(conn), ensure_ascii=False))
            return 0
        if "--import-dir" in args:
            print(json.dumps(register_local(conn, Path(opt("--import-dir"))), ensure_ascii=False))
            return 0
        limit = int(opt("--limit")) if "--limit" in args else None
        stats = run(limit=limit, force="--force" in args,
                    workers=int(opt("--workers", "6")), conn=conn)
        print(json.dumps(stats, ensure_ascii=False))
        return 0 if stats["failed"] == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
