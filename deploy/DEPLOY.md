# 部署：从「临时公网链接」到「固定网页」

三种目标的代价不同，但**程序侧只差几个环境变量**，代码不用动。当前默认走临时入口。

## 0. 现在这套：cloudflared 临时隧道（quick）

```bat
python -m stzb_app.tools.tunnel start      :: 起隧道，打印 https://xxx.trycloudflare.com
python -m stzb_app.tools.tunnel status     :: 看地址/进程/本机健康
python -m stzb_app.tools.tunnel stop       :: 关掉
```

- 前置：`downloads\cloudflared\cloudflared.exe`（`tunnel start` 找不到时会打印下载地址）。**所有下载统一放 `downloads\`，不要落 C 盘**（见 `downloads\README.md`）。
- 本机实测：cloudflared 自检 `precheck` 会报 `hard_fail=true`（UDP QUIC / TCP HTTP/2 / api.cloudflare.com 均被判失败），但隧道仍能注册成功（lax01/QUIC）；公网探测出现过瞬时 000，随即恢复 —— 这条链路不稳，重要场合别依赖它。
- 域名解析与启动日志的关系：`tunnel start` 现在**只扫本次启动之后的日志**，并会用公网 `/_stcore/health` 反验域名可用，不会再把 `api.trycloudflare.com` 误当隧道地址。
- 特性：零注册、立刻可用；**域名每次重启都变**；电脑关机即失效。
- 安全：脚本默认**拒绝在无口令时开公网**。口令来自环境变量 `STZB_PASSWORD` 或 `.streamlit/secrets.toml` 的 `password`；本机 `localhost` 访问免口令，公网必须输。

## 1. 固定地址 + 电脑可关机 → Streamlit Community Cloud（免费）

前置：GitHub 账号（授权一次）、仓库里包含 `requirements.txt`（仓库根）、`.streamlit/config.toml`、`stzb_app/data/stzb.db`、`stzb_app/components/lineup_board/img/`（这些都已就位）。

1. 把仓库推到 GitHub（根目录就是 `E:\dsh`，`requirements.txt` 已在根）。
   ⚠️ **这台机器直连 GitHub 拉大文件是被阻断的**（实测 `curl` 到 github.com / api.github.com TLS 通但 0 字节、error 28；ghproxy/jsdelivr/unpkg 也 000；npm 直连约 1KB/s，但 `cdn.npmmirror.com` 有 5.6MB/s）。所以 `git push` 前先确认这条链路能通，否则走代理，或换一台机器推。
2. share.streamlit.io → New app → 选仓库 → Main file path 填 `stzb_app/app.py` → Deploy。
3. 在 App settings → Secrets 里加 `password = "..."`（公网建议设）。
4. 注意：云端容器重启会重置文件系统，`user_hero` / `lineup` / `eval_run` 会丢；武将库/战法库/立绘在仓库里所以不丢。要长期保留个人数据，后续把 `db.py` 的 `DB_PATH` 换成外部 Postgres/Supabase 连接即可（`STZB_DB` 现在支持环境变量，接外部库属于下一步改造）。

## 2. 固定 IP/域名 + 7×24 → 自己的云服务器（Docker）

镜像与编排已写好：`deploy/Dockerfile`、`deploy/docker-compose.yml`（数据库落在 `stzb_data` 卷里，重启不丢）。

```bash
git clone <你的仓库> stzb && cd stzb
STZB_PASSWORD='换成你的口令' docker compose -f deploy/docker-compose.yml up -d --build
# 前面挂 Caddy/Nginx 做 HTTPS，或直接用 Cloudflare 代理到 8501
```

- 数据持久：`stzb_data` 卷 → `/app/stzb_app/data`。
- 健康检查：容器自带 `HEALTHCHECK` 打 `/_stcore/health`。
- 想换端口：compose 里改 `ports`。

## 2.5 固定地址 + 数据留本机（免费、最省事）→ ngrok 静态域名

前置：ngrok 免费账号（邮箱注册即可），后台 **Domains** 里会送 1 个静态域名 `xxx.ngrok-free.app`。

```bat
set NGROK_AUTHTOKEN=你的token
set STZB_PUBLIC_URL=https://xxx.ngrok-free.app
python -m stzb_app.tools.tunnel start --provider ngrok
```

- 不设 `STZB_PUBLIC_URL` 时拿到随机域名（每次变）；设了才是**固定地址**。
- 免费版有带宽/并发限制，自用够；程序侧零改动，URL/状态文件/`status`/`stop` 与 quick 模式一致。
- 二进制放 `downloads\ngrok\ngrok.exe`；启动后靠 ngrok 本地 API（`127.0.0.1:4040`）取地址，再反向 `/ _stcore/health` 验证。

## 3. 固定自有域名 + 数据留本机 → Cloudflare 命名隧道
前置：Cloudflare 账号 + 一个已托管域名（免费版够用）。

1. Zero Trust → Networks → Tunnels → Create tunnel → 复制 token。
2. Public Hostname 配 `stzb.你的域名` → `http://127.0.0.1:8501`。
3. 本机执行：

```bat
set CLOUDFLARE_TUNNEL_TOKEN=xxxxx
set STZB_PUBLIC_URL=https://stzb.你的域名
python -m stzb_app.tools.tunnel start --provider cloudflare
```

隧道进程与状态（`stzb_app\logs\tunnel.json`）和 quick 模式共用，`status` / `stop` 命令不变。

## 迁移时要动的东西（就这些）

| 目标 | 需要改的 | 不用改的 |
|---|---|---|
| Streamlit Cloud | 推 GitHub；Secrets 里写 password | 代码、数据文件、组件 |
| VPS / Docker | 拷仓库；设 `STZB_PASSWORD` | 代码、组件、立绘 |
| Cloudflare 命名隧道 | 设 `CLOUDFLARE_TUNNEL_TOKEN` / `STZB_PUBLIC_URL` / `--provider cloudflare` | 代码、数据 |

`tunnel.py` 里的 `PROVIDERS` 字典就是扩展点：新增一种入口 = 加一个 `start_xxx(port) -> dict` 并注册进去，CLI/状态文件/README 都不用动。
