"""公网入口开关（provider 可替换）。

v1 用 cloudflared quick tunnel：零注册、立刻得到 https 域名，但**每次重启都会换**。
后面要固定地址时不用改上层命令，只加一个 provider：

  quick      cloudflared --url（默认）
  cloudflare 命名隧道，固定自有域名；需要 CLOUDFLARE_TUNNEL_TOKEN，公网地址写在
             STZB_PUBLIC_URL（Cloudflare 后台 Public Hostname 配的那个）
  （预留）vps / streamlit-cloud 之类的部署不需要隧道，直接用 Dockerfile 跑

用法：
  python -m stzb_app.tools.tunnel start                    # 默认 quick，要求已设访问口令
  python -m stzb_app.tools.tunnel start --allow-public     # 不设口令也放行（危险，慎用）
  python -m stzb_app.tools.tunnel start --provider cloudflare
  python -m stzb_app.tools.tunnel status
  python -m stzb_app.tools.tunnel stop

口令来源：环境变量 STZB_PASSWORD 或 <仓库根>/.streamlit/secrets.toml 里的 password。
公网暴露时**默认拒绝在无口令状态下启动**，避免把「我的武将/队伍」直接挂到互联网上。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1]
ROOT = PKG_DIR.parent
LOG_DIR = PKG_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE = LOG_DIR / "tunnel.json"
LOG = LOG_DIR / "tunnel.log"
SECRETS = ROOT / ".streamlit" / "secrets.toml"

EXE_NAME = "cloudflared.exe" if os.name == "nt" else "cloudflared"
DEFAULT_PORT = 8501
# 只认「隧道分配给你的域名」。api.trycloudflare.com 是控制接口，日志里也会出现，
# 早期版本就是把它当成了隧道地址（先失败一次后重试必踩），所以这里显式排除 api. 前缀。
QUICK_URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")


# ---------------------------------------------------------------- 口令与前置
def _configured_password() -> str:
    pw = os.environ.get("STZB_PASSWORD", "")
    if pw:
        return pw
    if SECRETS.exists():
        try:
            import tomllib

            with SECRETS.open("rb") as f:
                return str(tomllib.load(f).get("password", "") or "")
        except Exception:
            return ""
    return ""


def _find_cloudflared() -> Path:
    # 下载物统一放仓库根的 downloads\（约定：不许落到 C 盘），见 downloads\README.md
    for cand in (ROOT / "downloads" / "cloudflared" / EXE_NAME,
                 PKG_DIR / "tools" / EXE_NAME,
                 ROOT / EXE_NAME):
        if cand.exists():
            return cand
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    raise SystemExit(
        "找不到 cloudflared。下载（约 55MB）后放到 downloads\\cloudflared\\cloudflared.exe：\n"
        "  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe\n"
        "或 `winget install Cloudflare.cloudflared`"
    )


def _health(port: int, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _public_ready(url: str, timeout: float = 6.0) -> bool:
    """公网域名是否已经真的通到本机服务（隧道注册完成前会连不上）。"""
    try:
        with urllib.request.urlopen(f"{url}/_stcore/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def _kill(pid: int) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, 15)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- provider
def start_quick(port: int) -> dict:
    exe = _find_cloudflared()
    log_f = LOG.open("a", encoding="utf-8", errors="replace")
    log_f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} quick tunnel :{port} ===\n")
    log_f.flush()

    flags = 0
    if os.name == "nt":
        # 脱离父进程，关掉终端也不会带走隧道
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    # 本次启动的日志起点：只扫这之后的字节。早前失败残留的控制接口 URL
    # （api.trycloudflare.com）曾经把上一次的地址扫错，所以「按 run 边界扫描」是主修法，
    # QUICK_URL_RE 里排除 api. 只是第二道保险。
    scan_from = LOG.stat().st_size

    cmd = [str(exe), "tunnel", "--url", f"http://127.0.0.1:{port}",
           "--no-autoupdate", "--loglevel", "info"]
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True)

    # cloudflared 把分配到的域名打在 stderr 里；但要等到隧道真正注册成功才算数
    # （域名先出现、连接后建立），所以找到候选后还要用公网 health 探一次。
    deadline = time.time() + 90
    url = ""
    seen: set[str] = set()
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"cloudflared 退出了（退出码 {proc.returncode}），看日志：{LOG}")
        try:
            text = LOG.read_text("utf-8", errors="replace")[scan_from:]
            for m in QUICK_URL_RE.finditer(text):
                cand = m.group(0).rstrip("/")
                if cand in seen:
                    continue
                seen.add(cand)
                if _public_ready(cand):
                    url = cand
                    break
        except Exception:
            pass
        if url:
            break
        time.sleep(1.0)
    if not url:
        _kill(proc.pid)
        raise SystemExit(f"90 秒内没拿到可访问的公网域名，看日志：{LOG}")

    state = {"provider": "quick", "url": url, "pid": proc.pid, "port": port,
             "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "log": str(LOG)}
    _write_state(state)
    return state


def start_cloudflare(port: int) -> dict:
    exe = _find_cloudflared()
    token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
    if not token:
        raise SystemExit("cloudflare provider 需要环境变量 CLOUDFLARE_TUNNEL_TOKEN"
                         "（Cloudflare Zero Trust → Networks → Tunnels → 复制 token）")
    public = os.environ.get("STZB_PUBLIC_URL", "")
    log_f = LOG.open("a", encoding="utf-8", errors="replace")
    log_f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} named tunnel ===\n")
    log_f.flush()
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    proc = subprocess.Popen([str(exe), "tunnel", "--no-autoupdate", "run", "--token", token],
                            stdout=log_f, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True)
    time.sleep(3)
    if proc.poll() is not None:
        raise SystemExit(f"cloudflared 退出了（退出码 {proc.returncode}），看日志：{LOG}")
    state = {"provider": "cloudflare", "url": public, "pid": proc.pid, "port": port,
             "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "log": str(LOG)}
    _write_state(state)
    return state


def _find_ngrok() -> Path:
    for cand in (ROOT / "downloads" / "ngrok" / ("ngrok.exe" if os.name == "nt" else "ngrok"),
                 PKG_DIR / "tools" / ("ngrok.exe" if os.name == "nt" else "ngrok")):
        if cand.exists():
            return cand
    found = shutil.which("ngrok")
    if found:
        return Path(found)
    raise SystemExit(
        "找不到 ngrok。下载后放到 downloads\\ngrok\\ngrok.exe：\n"
        "  https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip\n"
        "并到 https://dashboard.ngrok.com/get-started/your-authtoken 拿 authtoken"
    )


def start_ngrok(port: int) -> dict:
    """固定域名那条路里最省事的：ngrok 免费静态域名（注册即得 1 个）。

    需要 NGROK_AUTHTOKEN；要固定地址再加 STZB_PUBLIC_URL=https://xxx.ngrok-free.app
    （就是 ngrok 后台「Domains」里那个静态域名）。不设 STZB_PUBLIC_URL 会拿到随机域名。
    """
    exe = _find_ngrok()
    token = os.environ.get("NGROK_AUTHTOKEN", "")
    if not token:
        raise SystemExit("ngrok provider 需要环境变量 NGROK_AUTHTOKEN"
                         "（https://dashboard.ngrok.com/get-started/your-authtoken）")
    host = (os.environ.get("STZB_PUBLIC_URL", "") or "").replace("https://", "").replace("http://", "").strip("/")

    log_f = LOG.open("a", encoding="utf-8", errors="replace")
    log_f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ngrok :{port} ===\n")
    log_f.flush()
    scan_from = LOG.stat().st_size

    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    cmd = [str(exe), "http", str(port), "--log=stdout", "--log-format=logfmt",
           "--authtoken", token]
    if host:
        cmd += ["--domain", host]
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True)

    # ngrok 自带本地 API（默认 4040），比解析日志稳
    deadline = time.time() + 60
    url = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"ngrok 退出了（退出码 {proc.returncode}），看日志：{LOG}")
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                for t in json.load(r).get("tunnels", []):
                    cand = (t.get("public_url") or "").rstrip("/")
                    if cand.startswith("https://") and _public_ready(cand, timeout=5):
                        url = cand
                        break
        except Exception:
            pass
        if url:
            break
        time.sleep(1.0)
    if not url:
        _kill(proc.pid)
        raise SystemExit(f"60 秒内没拿到可访问的 ngrok 地址，看日志：{LOG}"
                         f"（尾部：{LOG.read_text('utf-8', errors='replace')[scan_from:][-800:]}）")

    state = {"provider": "ngrok", "url": url, "pid": proc.pid, "port": port,
             "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "log": str(LOG)}
    _write_state(state)
    return state


PROVIDERS = {"quick": start_quick, "cloudflare": start_cloudflare, "ngrok": start_ngrok}


# ---------------------------------------------------------------- 命令
def cmd_start(args) -> int:
    st = _read_state()
    if st.get("pid"):
        print(f"已经在跑：{st.get('url') or '(未拿到域名)'}  pid={st['pid']}")
        print("要先换一个入口就先 stop。")
        return 0
    if not _health(args.port):
        raise SystemExit(f"本机 :{args.port} 没有健康的服务，先把配将程序起起来"
                         "（桌面 exe 双击，或 python -m stzb_app.tools.launch start）")
    if not _configured_password() and not args.allow_public:
        raise SystemExit(
            "拒绝在无访问口令的情况下开公网入口。\n"
            "  设口令：环境变量 STZB_PASSWORD，或在 .streamlit/secrets.toml 写 password = \"...\"\n"
            "  确实要公开（任何人可看）：加 --allow-public"
        )

    provider = (args.provider or os.environ.get("STZB_TUNNEL_PROVIDER") or "quick").lower()
    if provider not in PROVIDERS:
        raise SystemExit(f"未知 provider：{provider}（可选 {', '.join(PROVIDERS)}）")
    state = PROVIDERS[provider](args.port)
    print(f"provider : {state['provider']}")
    print(f"公网地址 : {state['url'] or '(命名隧道按后台配置的域名访问)'}")
    print(f"本地端口 : {state['port']}   进程 {state['pid']}   日志 {state['log']}")
    if not _configured_password():
        print("提醒：没有设置口令，任何人拿到这个地址都能打开。")
    return 0


def cmd_status(args) -> int:
    st = _read_state()
    if not st:
        print("没有记录到隧道。")
        return 0
    pid = st.get("pid")
    alive = False
    if pid:
        try:
            if os.name == "nt":
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                     capture_output=True, text=True, check=False).stdout or ""
                alive = "No tasks" not in out and "没有运行" not in out and str(pid) in out
            else:
                os.kill(pid, 0)
                alive = True
        except Exception:
            alive = False
    print(f"provider : {st.get('provider')}")
    print(f"公网地址 : {st.get('url') or '(未记录)'}")
    print(f"进程     : {pid}  {'存在' if alive else '已退出（用 stop 清理）'}")
    print(f"启动时间 : {st.get('started_at')}")
    print(f"本机健康 : {'200' if _health(st.get('port') or DEFAULT_PORT) else '不可用'}")
    return 0


def cmd_stop(args) -> int:
    st = _read_state()
    pid = st.get("pid")
    if not pid:
        print("没有记录到隧道，无需处理。")
        return 0
    _kill(pid)
    STATE.unlink(missing_ok=True)
    print(f"已停止隧道（pid={pid}）。")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tunnel", description="配将程序的公网入口开关")
    p.add_argument("action", choices=["start", "status", "stop"])
    p.add_argument("--provider", default="", help="quick（默认）| ngrok | cloudflare")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--allow-public", action="store_true", help="无口令也允许开公网（危险）")
    args = p.parse_args(argv)
    return {"start": cmd_start, "status": cmd_status, "stop": cmd_stop}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
