#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""率土之滨 · 配将推荐 —— 服务启动器（Windows 优先，其他平台也能跑）。

为什么要单写一个启动器，而不是直接 `python -m streamlit run`：
    1. 双击启动时最怕「黑窗口一闪就没了」= 服务跟着父进程被回收。这里用
       DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 起独立进程，关掉启动窗口服务照活。
    2. 端口已经有人在听的时候不要再起第二个实例（Streamlit 会报端口占用并退出），
       本程序先探健康检查：活着就只帮你把浏览器打开。
    3. 服务起来了但页面还没就绪，直接开浏览器会看到「连接失败」。这里轮询
       /_stcore/health 直到 200 再开浏览器。
    4. 出问题时窗口里要能看见原因（日志尾巴），而不是一句「启动失败」。

用法：
    python -m stzb_app.tools.launch start          # 启动（已在跑则只开浏览器）
    python -m stzb_app.tools.launch restart        # 先停再起
    python -m stzb_app.tools.launch stop           # 停止
    python -m stzb_app.tools.launch status         # 看状态（不启动任何东西）
    python -m stzb_app.tools.launch logs           # 打印日志尾部

常用开关：
    --port 8501      换端口（也可用环境变量 STZB_PORT）
    --no-browser     只起服务，不开浏览器
    --timeout 60     等待健康检查的秒数
    --quiet          少打印（给 .cmd 用）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent            # stzb_app/tools
PKG_DIR = BASE_DIR.parent                              # stzb_app
ROOT_DIR = PKG_DIR.parent                              # 项目根（含 stzb_hero_data 等）
APP_PATH = PKG_DIR / "app.py"
DB_PATH = PKG_DIR / "data" / "stzb.db"
LOG_DIR = PKG_DIR / "logs"
LOG_PATH = LOG_DIR / "streamlit.log"
PID_PATH = LOG_DIR / "streamlit.pid"

DEFAULT_PORT = int(os.environ.get("STZB_PORT", "8501"))
IS_WIN = os.name == "nt"

# 起子进程用的标志位：脱离控制台 + 新进程组，父进程退出不影响它。
_DETACH = 0
if IS_WIN:  # pragma: no cover - 仅 Windows 有意义
    _DETACH = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- 输出
def _setup_stdout() -> None:
    """GBK 控制台打中文会 UnicodeEncodeError，统一压成 utf-8/replace。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def say(msg: str = "", quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


# --------------------------------------------------------------------------- 探测
def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/_stcore/health"


def is_healthy(port: int, timeout: float = 1.5) -> bool:
    """只看 HTTP 200，不解析 body（body 是 "ok" 两个字，解析没意义）。"""
    try:
        with urllib.request.urlopen(health_url(port), timeout=timeout) as resp:
            return int(getattr(resp, "status", 0) or resp.getcode() or 0) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pid_on_port(port: int) -> int | None:
    """从 netstat 找 LISTENING 的 PID（Windows 的 netstat -ano 最省依赖）。"""
    if not IS_WIN:
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        ).stdout
    except Exception:
        return None
    suffix = f":{port}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if local.endswith(suffix) and state.upper() == "LISTENING" and pid.isdigit():
            return int(pid)
    return None


def read_pid_file() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def process_alive(pid: int) -> bool:
    if not IS_WIN:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             encoding="utf-8", errors="replace").stdout
        return str(pid) in out
    except Exception:
        return False


def tail_log(lines: int = 20) -> str:
    if not LOG_PATH.exists():
        return "（还没有日志文件）"
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return "（日志读取失败）"
    return "\n".join(text[-lines:]) or "（日志是空的）"


# --------------------------------------------------------------------------- 动作
def do_status(port: int, quiet: bool = False) -> int:
    healthy = is_healthy(port)
    pid = pid_on_port(port) or read_pid_file()
    say(f"端口      : {port}", quiet)
    say(f"健康检查  : {'200 OK（服务在跑）' if healthy else '无响应（服务没在跑）'}", quiet)
    say(f"进程 PID  : {pid if pid else '—'}", quiet)
    say(f"数据库    : {DB_PATH} {'（存在）' if DB_PATH.exists() else '（缺失，首次启动会自动导入）'}", quiet)
    say(f"日志      : {LOG_PATH}", quiet)
    say(f"页面      : http://localhost:{port}", quiet)
    return 0 if healthy else 1


def ensure_database(quiet: bool = False) -> int:
    """第一次用（或换了机器）库是空的，先跑一次 ingest；已有库就跳过。"""
    if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        return 0
    say("数据库不存在，先导入 CSV/战报（约 10~30 秒）…", quiet)
    rc = subprocess.call([sys.executable, "-m", "stzb_app.ingest"], cwd=str(ROOT_DIR))
    if rc != 0:
        say("数据导入失败，启动中止。先单独跑： python -m stzb_app.ingest", quiet)
    return rc


def stop(port: int, quiet: bool = False) -> int:
    pid = pid_on_port(port)
    if pid is None and IS_WIN:
        # 端口没监听但 pid 文件还在的残留情况
        stale = read_pid_file()
        if stale and process_alive(stale):
            pid = stale
    if pid is None:
        say(f"端口 {port} 上没有在跑的服务，无需停止。", quiet)
        if PID_PATH.exists():
            PID_PATH.unlink(missing_ok=True)
        return 0

    say(f"停止 PID {pid} …", quiet)
    if IS_WIN:
        subprocess.call(["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:  # pragma: no cover
        subprocess.call(["kill", "-TERM", str(pid)])
    for _ in range(20):                    # 最多等 4 秒让端口释放
        if not is_healthy(port) and pid_on_port(port) is None:
            break
        time.sleep(0.2)
    if is_healthy(port):
        say("停止失败：健康检查仍有响应，可能被别的进程接管了。", quiet)
        return 1
    PID_PATH.unlink(missing_ok=True)
    say("已停止。", quiet)
    return 0


def open_page(port: int, quiet: bool = False) -> None:
    url = f"http://localhost:{port}"
    say(f"打开浏览器：{url}", quiet)
    try:
        webbrowser.open(url)
    except Exception as exc:  # pragma: no cover
        say(f"自动打开浏览器失败（{exc}），请手动访问 {url}", quiet)


def start(port: int, *, restart: bool = False, browser: bool = True,
          timeout: float = 60.0, quiet: bool = False) -> int:
    if restart:
        stop(port, quiet)

    if is_healthy(port):
        pid = pid_on_port(port)
        say(f"服务已经在小跑（PID {pid or '未知'}），不重复启动。", quiet)
        if browser:
            open_page(port, quiet)
        return 0

    # 端口被占但健康检查不过 = 半死状态（常见于上次被 kill 到一半、或别的程序霸占端口）。
    # 这时候硬起第二个实例只会报「端口占用」，不如直接告诉用户怎么做。
    held = pid_on_port(port)
    if held is not None:
        say(f"端口 {port} 被 PID {held} 占用，但 /_stcore/health 不响应（半死状态）。", quiet)
        say(f"先跑这个再来： python -m stzb_app.tools.launch restart --port {port}", quiet)
        return 1

    rc = ensure_database(quiet)
    if rc != 0:
        return rc

    if not APP_PATH.exists():
        say(f"找不到 {APP_PATH}，目录结构不对。", quiet)
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:                                    # 日志超过 2MB 就转存一份，别无限长
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    except Exception:
        pass

    cmd = [
        sys.executable, "-m", "streamlit", "run", str(APP_PATH),
        "--server.headless", "true",
        "--server.port", str(port),
        "--server.address", "127.0.0.1",
        "--browser.gatherUsageStats", "false",
    ]
    say(f"启动服务：port={port}（独立进程，本窗口关掉也不会停）", quiet)
    log_f = open(LOG_PATH, "ab")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT_DIR), stdout=log_f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=_DETACH, close_fds=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false"},
        )
    finally:
        log_f.close()
    PID_PATH.write_text(str(proc.pid), encoding="utf-8")

    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if is_healthy(port):
            say(f"就绪（PID {proc.pid}，耗时 {int(time.time() - (deadline - timeout))}s）", quiet)
            if browser:
                open_page(port, quiet)
            return 0
        if proc.poll() is not None:
            say(f"服务进程已退出（退出码 {proc.returncode}），日志尾部：", quiet)
            say(tail_log(25), quiet)
            return 1
        if not quiet:
            dots += 1
            print(".", end="", flush=True)
        time.sleep(0.5)
    say(f"\n等待 {int(timeout)}s 仍未就绪。日志尾部：", quiet)
    say(tail_log(25), quiet)
    say(f"进程 PID {proc.pid} 可能还在启动中，可稍后跑 status 查看。", quiet)
    return 1


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m stzb_app.tools.launch",
        description="率土之滨·配将推荐 启动器（默认 start）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python -m stzb_app.tools.launch            # 启动并开浏览器\n"
            "  python -m stzb_app.tools.launch restart    # 重启\n"
            "  python -m stzb_app.tools.launch stop       # 停止\n"
            "  python -m stzb_app.tools.launch status     # 状态\n"
        ),
    )
    p.add_argument("action", nargs="?", default="start",
                   choices=["start", "stop", "restart", "status", "logs", "open"])
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--timeout", type=float, default=60.0, help="等待就绪的秒数")
    p.add_argument("--quiet", action="store_true", help="精简输出")
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    args = build_parser().parse_args(argv)
    port, quiet = args.port, args.quiet

    if args.action == "status":
        return do_status(port, quiet)
    if args.action == "logs":
        print(tail_log(60))
        return 0
    if args.action == "stop":
        return stop(port, quiet)
    if args.action == "open":
        open_page(port, quiet)
        return 0
    if args.action == "restart":
        return start(port, restart=True, browser=not args.no_browser,
                     timeout=args.timeout, quiet=quiet)
    return start(port, browser=not args.no_browser, timeout=args.timeout, quiet=quiet)


if __name__ == "__main__":
    raise SystemExit(main())
