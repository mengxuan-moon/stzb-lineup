# 率土之滨 · 配将推荐（规则引擎版 v1）

本地单机、不联网、不做战斗模拟。输入「我拥有的武将 + 战法 + 我自己搭的队伍」，
程序做**合法性校验 → 六维评分 → 邻域变体补齐 → Top-10 排序 → 理由/克制/替补**。

- 形态：**A 评价排序模式**（用户手动搭队，程序只校验+打分+排序），已预留 B/C 生成器的扩展点。
- 技术栈：Python 3.13（原计划 3.11，本机实测 3.13.14）+ Streamlit 1.66.0 + SQLite 3.50.4。

## 安装与启动

```bash
cd E:\dsh
python -m pip install -r stzb_app\requirements.txt   # 只有 streamlit==1.66.0，其余全是标准库
python -m stzb_app.cli --init            # 建表 + 播种（幂等；库不存在时才真写）
python -m stzb_app.ingest --rebuild      # 删库重建 + 导入（约 10~30 秒）
python -m stzb_app.cli --selftest        # 数据自检 + 端到端跑一次（断言失败 exit 1）
python -m stzb_app.apptest_smoke         # 界面冒烟（AppTest，跑的是库副本，不碰 data\stzb.db）
.\stzb_app\start.ps1                     # 一键启动：库不存在时先自动导入，再起 Streamlit
# 等价于：python -m streamlit run stzb_app/app.py
```

- **`STZB_DB` 环境变量可以换库**：`db.DB_PATH` 默认是 `stzb_app\data\stzb.db`，若设了
  `STZB_DB`（绝对路径，或相对当前目录的路径）就用它。冒烟测试就是靠这个把界面引到快照副本上的。
- **冒烟测试不碰生产库**：`apptest_smoke.py` 先用 `sqlite3.Connection.backup()` 把生产库
  快照到 `%TEMP%\stzb_smoke_<pid>\stzb.db`，再把 `STZB_DB` 指过去，AppTest 全程只写副本；
  结束时校验生产库的 `lineup / eval_run / team_score` 计数与跑前一致，并删掉副本目录。


- `start.ps1` 自己切到 `stzb_app` 的父目录当项目根，所以不需要先 `cd`；但**原始数据目录必须与
  `stzb_app` 同级**：`E:\dsh\stzb_hero_data`、`E:\dsh\stzb_skill_data`、`E:\dsh\stzb_team_eval`，
  否则导入阶段直接报「缺少数据文件」。
- **`--rebuild` 会删掉整个 `stzb_app\data\stzb.db`**：库里的「我的武将 / 我的战法 / 我搭的队伍 /
  推荐历史」全部清空，只保留能从源文件重建的数据。
- 建库前若已有 8501 端口的 Streamlit 在跑，先停掉（`netstat -ano | findstr :8501` 找 PID →
  `taskkill /PID <pid> /F`），否则 file-watcher 会在删库瞬间读到半截状态。

## 一键启动（推荐）

项目根目录（`E:\dsh`）下双击即可：

- **`启动配将.cmd`** —— 起服务并自动打开浏览器；**已经在跑就只开浏览器**，不会再起第二个实例。
- **`停止配将.cmd`** —— 停服务（按端口找 PID，`taskkill /T` 连子进程一起收）。

背后是 `stzb_app\tools\launch.py`，命令行也能细调：

| 命令 | 作用 |
| --- | --- |
| `python -m stzb_app.tools.launch` | 启动 + 开浏览器（默认动作） |
| `python -m stzb_app.tools.launch restart` | 先停再起 |
| `python -m stzb_app.tools.launch stop` | 停止 |
| `python -m stzb_app.tools.launch status` | 状态：健康检查 / PID / 库 / 日志路径 |
| `python -m stzb_app.tools.launch logs` | 打印日志尾部 |
| `python -m stzb_app.tools.launch open` | 只开浏览器 |

开关：`--port 8502` 换端口（或环境变量 `STZB_PORT`）、`--no-browser` 只起服务、`--timeout 60`。

四个设计点（都是踩过的坑）：

1. **必须脱离父进程**：以前用 `start "" /B python -m streamlit ...`，启动窗口一关服务就跟着被
   回收（实测 health 直接变 000）。现在用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`，
   关掉启动窗口服务照活。
2. **先探健康再开浏览器**：轮询 `/_stcore/health` 等到 200 才 `webbrowser.open`，否则用户先看到
   的是「连接失败」页。
3. **幂等**：端口上已有 200 响应就只开浏览器；日志超 2MB 自动转成 `streamlit.log.1`；
   PID 落在 `stzb_app\logs\streamlit.pid`，`status`/`stop` 都靠它兜底。
4. **`.cmd` 必须是 CRLF、且正文只写 ASCII**：cmd.exe 按 CRLF 记账读批处理，UTF-8 无 BOM + 纯 LF
   时会按字节脱轨（实测 `setlocal EnableExtensions` 被读成 `'bleExtensions'`、`chcp 65001` 被读成
   `'ch'`，中文 echo 还会被当成命令执行）。所以这两个 `.cmd` 只留 ASCII，**所有中文提示都由
   `launch.py` 打印**；编辑保存时换行必须保持 CRLF，否则会再次踩这个坑。

想前台跑、边看日志边用（Ctrl+C 停）：`powershell -ExecutionPolicy Bypass -File stzb_app\start.ps1`。

### 桌面 exe（双击图标版）

桌面上的 **`率土配将.exe`**（208KB，带图标）是上面这套逻辑的 C# 外壳，源码 `tools/launcher_gui.cs`：

- 双击 → 探 `/_stcore/health`；没起就去调 `launch.py start --no-browser --quiet`（隐藏窗口），等到 200 再开浏览器；已在跑就只开浏览器。
- 起不来会弹窗给退出码 + stderr 片段 + 日志路径，不会静默失败。
- 换机器/换目录：exe 旁边放一个 `stzb_root.txt`（一行绝对路径）就能指过去，不用重编译；端口可用环境变量 `STZB_PORT` 改。

重新编译（Windows 自带 csc，离线、不需要 SDK）：

```bat
%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:winexe ^
  /win32icon:stzb_app\tools\stzb.ico ^
  /out:"%USERPROFILE%\Desktop\率土配将.exe" stzb_app\tools\launcher_gui.cs
```

（图标 `stzb.ico` 是从 `components/lineup_board/img/100003.jpg` 用 Pillow 生成的多尺寸 ico；不加 `/win32icon` 时产物只有 7.7KB。）

### 公网访问（手机/别的电脑打开）

```bat
python -m stzb_app.tools.tunnel start     :: 打印临时公网地址 https://xxx.trycloudflare.com
python -m stzb_app.tools.tunnel status    :: 地址 / 进程 / 本机健康
python -m stzb_app.tools.tunnel stop      :: 关闭
```

- 依赖 `downloads\cloudflared\cloudflared.exe`（约 55MB，未内置；缺了会打印官方下载地址）。**下载物统一放仓库根的 `downloads\`，不要落 C 盘**（见 `downloads\README.md`）。
- **临时域名每次重启会变**；电脑关机即失效。要固定地址见 `deploy\DEPLOY.md`（Streamlit Cloud / VPS+Docker / Cloudflare 命名隧道三种路线，代码通用）。
- **访问口令**：`.streamlit/secrets.toml` 里的 `password`（或环境变量 `STZB_PASSWORD`）。本机 `localhost` 免口令，公网访问必须输；`tunnel start` 在没设口令时会拒绝开公网（除非 `--allow-public`，不建议）。
- 换入口不改代码：`tunnel.py` 的 `PROVIDERS` 是扩展点，新增一种 = 加一个 `start_xxx(port)` 注册进去。

## 云端部署（Streamlit Community Cloud，固定网址）

线上实例：`https://stzb-lineup-dgwusnd6zfdtn65f87ocg9.streamlit.app`（仓库 `mengxuan-moon/stzb-lineup`，入口文件 `stzb_app/app.py`）。电脑关机也能访问。

**两个坑，实测踩过**：

1. **私有仓库 → 应用默认私有**。浏览器里登录了 Streamlit 的人能直接打开，匿名访问却被 303 踢去登录页，于是「我这儿能打开」是假象（手机、别人的电脑照样打不开）。把仓库转 Public 只是第一步，**必须再去应用里翻一次开关**：
   share.streamlit.io → 打开该应用 → 右下角 `⋮` → **Settings → Sharing → "Who can view this app"** → 选 public / anyone with the link → Save。
   判据只有一个：`curl -s -o NUL -w "%{http_code}" <地址>/_stcore/health` 返回 **200**。返回 303 就是还被鉴权挡着。
2. **`app.py` 用绝对包名 `from stzb_app import …`**，所以文件第 14-16 行把项目根塞进了 `sys.path`。入口路径按 `stzb_app/app.py` 填即可，不要改成 `streamlit_app.py`（仓库里没有这个文件）。

**口令**：本机用 `.streamlit/secrets.toml`（已 gitignore，不入库）；云端要在应用 **Advanced settings → Secrets** 里填同样的东西：

```toml
password = "换成你的口令"
```

没有口令时公网页面对所有人开放（`_access_gate()` 只在设了口令后才生效，`localhost` 永远免口令）。

**免费版的两个限制（不是 bug）**：

- 约 12 小时无访问会休眠，下次打开要等 20~40 秒唤醒。
- 容器重启/重新部署后文件系统回到仓库初始状态：**武将库 / 战法库 / 立绘不丢**（都在仓库里），但「我的武将」「我的战法」的勾选、保存的队伍、评分历史会重置。要做真正持久，需把 `db.py` 的 `DB_PATH` 换成外部库（`STZB_DB` 环境变量已预留口子），或用 `deploy/Dockerfile` 自建服务器挂数据卷。





界面五个页签：① 勾我的武将 ② 勾我的战法 ③ 搭队伍（1~3 队，每队 3 人 × 2 战法）
④ 推荐结果（Top-10，含理由/战报证据/社区摘录/克制/替补）⑤ 数据与规则（来源清单、参数四态、权重）。

## 目录

```
stzb_app/
  schema.sql       28 张业务表（基础5 / 规则6 / 输入5 / 结果6 / 证据3 / 溯源3；sqlite_sequence 不计）
  db.py            连接、建表、播种权重与参数（四态 data_status）
  core.py          纯函数规则引擎：校验（9 个规则 ID：R-INPUT-01~05、R-DIST-01~03、R-CONFLICT-01）+ 六维评分 + 适用性 + 距离几何
  ingest.py        3 个 CSV + 战报 + 社区阵容 → SQLite（幂等）
  engine.py        变体生成、证据检索、理由/克制/替补、Top-N、结果落库
  cli.py           命令行：--init / --rebuild / --selftest / --demo
  app.py           Streamlit 界面
  ui_board.py      「配将界面」自定义组件的 Python 侧（准备武将/战法池、状态↔members 互转）
  components/lineup_board/index.html   配将界面组件（率土风格布局 + HTML5 拖拽，无构建步骤）
  components/lineup_board/img/         武将立绘本地缓存（可选，见「武将立绘」一节）
  portraits.py     联网抓立绘 → 本地缓存 + hero_portrait 表（可选；不跑也能用）
  apptest_smoke.py 界面冒烟测试（streamlit.testing.v1.AppTest）
  requirements.txt 运行依赖（streamlit==1.66.0）
  start.ps1        一键启动脚本（前台运行，PATH 里没有 streamlit 也能起：调 `python -m streamlit`）
  tools/launch.py  后台启动器（脱离父进程 / 幂等 / 健康检查通过后再开浏览器；配 启动配将.cmd）
  logs/            启动器日志 streamlit.log + pid 文件（运行时生成）
  .gitignore       忽略 __pycache__ / *.pyc / WAL 边车文件
  data/stzb.db     生成的数据库
```

## 配将界面（③ 页，复刻主城配将界面）

布局对齐游戏内主城配将界面：**上方三格阵容（大营 / 中军 / 前锋）、下方武将画像条**，把画像拖进格子即入位。

- 交互：拖拽入位 / 先点武将再点格子 / 点已上阵的卡改两个战法 / ✕ 移出；顶部实时算队伍统率（超限变红）。
- 技术：`streamlit.components.v1.declare_component` 挂 `components/lineup_board/index.html`，
  **无 npm 构建、不联网**；组件用自己的 postMessage 协议回传 `{name, slots}`，Python 侧只准备数据 + 落库。
- 立绘：已抓取 **391 张 240×348（约 14MB）** 落在 `components/lineup_board/img/`，卡面是真图；
  缺图/抓取失败的武将会自动回退成「阵营/兵种配色渐变 + 星级 + 末字大字」占位，不影响任何功能。
  换图只需替换该目录下同名 `{武将ID}.jpg`（映射见 `hero_portrait` 表）。
- 降级：`declare_component` 失败时 `app.py` 自动回落到原「表格录入」下拉框界面（同页下方，一直保留），不会白屏。
- 该页**只做录入**，评分/排序仍在 ④ 页；保存时走 `engine.save_lineup(source='user')`，与表格录入同一套校验。

## 武将立绘（可选，需联网；不抓也能用）

默认离线：没抓过图时界面用「阵营/兵种配色 + 末字大字」占位。允许联网后跑一次，之后**永久本地**
（本机已导入 391 张，所以现在直接看到真立绘）：

```bash
python -m stzb_app.portraits --all           # 391 张，14MB，约 2 秒（6 线程）
python -m stzb_app.portraits --limit 20      # 先试 20 张
python -m stzb_app.portraits --report        # 只看现状（不联网）
python -m stzb_app.portraits --import-dir 某目录   # 导入已下好的图并登记入库
```

> 三条 CLI 都会先 `init_db()`（自动建表/迁移），**不用先开页面**；老库（`user_version=3`）上直接跑
> 曾经会崩在 `no such table: hero_portrait`，已修。

口径（实测记录，改代码前先读）：

- 图片 URL 不是「武将ID 直接拼」：`card_medium_{iconId}.jpg`，`iconId` 来自官方
  `hero_extra.json`（391 个里有 **57 个 iconId ≠ 武将ID**），一次请求拿全并缓存到 `data/hero_extra.json`。
- 240×348 是公开最大尺寸（`card_large/big/max/original/full` 全部 404）；404 返回的是 XML 错误体，
  所以落盘前必须校验 **JPEG 魔数 `ffd8` + 体积 >10KB**，否则会把错误页当图存下来。
- 兜底链：`card_medium` 失败重试 3 次（0.5/1.5/3s 退避）→ `card_small`（98×98）→ 记 `status='failed'`
  （文件不删、原图不动，界面自动回落占位）。
- 图片版权归网易/游戏方，仅本地个人使用；本仓库不随代码分发图片。

## 数据来源（只读复用，不改原始文件）

| 文件 | 规模 | 用途 |
|---|---|---|
| `stzb_hero_data/率土之滨_武将属性表.csv` | 391 行 × 20 列 | 武将库 |
| `stzb_skill_data/率土之滨_战法表.csv` | 642 行（636 唯一 ID） | 战法库 |
| `stzb_team_eval/战报采集/battles.json` | 源 3433 条 → 入库 3239 场（194 条非三人记录跳过）｜247 将｜1097 组 | 战报胜率证据 |
| `stzb_team_eval/战报采集/war_reports.json` | 1649 条 | 阵容出处 + 推荐理由摘录 |

战报**不含战法信息**，只覆盖 391 将里的 247 将、1097 个三人组合；因此战报只用于
「同分排序 + 证据数字」，不参与评分权重（Q2 选项 b）。场次口径：源文件 3433 条里 194 条
不是三人记录被跳过，其余 3239 场入 `battle_stat`。`battle_stat` 的真实列是
`trio_key / hero_ids_json / games / wins / losses / pve_games / win_rate / sample_flag`
（**没有** `ties` 列）：`games = wins + losses + 平局`，平局由 `games - wins - losses` 得到
（实测 wins 1542 + losses 896 + 平局 801 = 3239）；`pve_games`（297）是**重叠维度**，
表示这 3239 场里有 297 场是 PvE，不另外单列、也不能再加进场次。胜率 = 胜 / 全部场次。


## 评分口径（复用旧版 JS 校验器，已与 `stzb_team_eval/src/core.js` 对齐）

- 系统强度 = 合法性 20 + cost 效率 15 + 协同 15 + 战法适配 20 + 距离覆盖 10 + 战法结构 20（四目标共用）
- 适用性 = 4 目标 × 5 维，权重**按目标不同**（`score_weight` 表，引擎版本 1.1.0 起）：
  - 打地：输出 32 / 生存 28 / 控制 15 / 速度 20 / 攻城 5
  - 野战：输出 32 / 控制 28 / 速度 22 / 生存 18 / 攻城 0
  - 攻城：输出 25 / 生存 18 / 控制 7 / 速度 5 / 攻城 45
  - 守城：输出 26 / 生存 34 / 控制 24 / 速度 16 / 攻城 0
  - 依据：打地看持续输出与前锋损兵（生存）、攻城以攻城值为主判据；老库由 `db._migrate()` 按「旧值→新值」逐格迁移，不重建库。
- 综合 = 系统 × 0.6 + 适用性 × 0.4
- 品质分：S100 / A85 / B70 / C55 / D40；判定：≥75 可用，≥55 可调，否则重构
- 距离覆盖 = 0.6 × 战法有效距离覆盖 + 0.4 × 站位适性（大营取攻击/谋略、中军取 0.6 控制覆盖 + 0.4 速度、前锋取防御）
- 战法结构 = 构成分 − 冲突扣分（每对 R-CONFLICT-01 冲突 −8，单队上限 −16）
- 适用性的「攻城」= 0.7 × 攻城属性 + 0.3 × 攻城向战法数（`siege_skill_keywords` 命中，2 条封顶）
- 相克链（骑→步→弓→骑）在适用性里**不计分**（敌方兵种未知，无法算减伤期望），但 `rule_param.troop_counter_damage`
  的实测减伤（骑克步 30% / 步克弓 25% / 弓克骑 20%）用于克制关系文案。
- 协同的阵营/兵种加成数值取自 `rule_param.camp_bonus_table`（3 同阵营 5.5%）与 `troop_bonus_table`
  （3 同兵种 10%、2 同 5%），协同分换算基准仍为旧版 55/30/10 与 25/12/0。
- 新增规则参数（`rule_param` 表，全部可在库里改，不用改代码）：`troop_counter_damage`、`camp_bonus_table`、
  `troop_bonus_table`、`synergy_points`、`distance_split`、`conflict_penalty`、`siege_skill_keywords`。

### 规则 ID 语义（`core.validate`）

| ID | 级别 | 判据 |
|---|---|---|
| R-INPUT-01 | error | 三将齐全、站位不重复、战法已选且存在于数据 |
| R-INPUT-02 | error | 武将/战法队内重复 |
| R-INPUT-03 | error | 统率总和超上限 |
| R-INPUT-04 | warn | 战法标注兵种不含该武将兵种（非硬限制） |
| R-INPUT-05 | info | 有效距离 / 发动几率 / 可拆战法2 / 组合 等字段缺失，跳过该值 |
| R-DIST-01 | warn | 战法有效距离对位次覆盖不足（旧版启发式） |
| R-DIST-02 | warn | 战法有效距离打不到该位置的主要目标（旧版启发式） |
| R-DIST-03 | warn | 普攻攻击距离打不到敌方前锋（大营 3 / 中军 2 / 前锋 1） |
| R-CONFLICT-01 | warn | 同类战法同种增益/减益/控制效果冲突（控制后释放失效、数值替换取高），结构分每对 −8、单队上限 −16 |

## 已知限制（等你补数据/口径）

- **B1 赛季白名单**：武将 CSV 无赛季字段 → `season_pool` 表已建但为空，当前全量池、未做 S1 过滤。
- **B2 拆解口径**：`user_settings.allow_dismantle` 这个「允许拆解」开关**会落库**（旧版写死 True 且从不保存，
  等于没实现）。打开时 ② 页把武将的可拆战法默认算作已拥有，保存后进入我的战法；它是输入口径，
  不改变任何评分公式。若要按持有张数算，改 `user_skill` 加 `owned_count` 即可。
- **B3 冲突组语义**：`conflict_group` 未标注的战法**不参与**冲突判定（`rule_param.conflict_group_semantics` = unconfirmed）。
- **Q4-6 组合（缘分）加成**：`combo.bonus_text` 保存文字、不参与算分（数值待你补）；阵营/兵种加成已按
  `camp_bonus_table` / `troop_bonus_table` 计入协同分（点将台 1 级口径）。
- 无兵种适性（用户确认本作无此机制）。
- **PvE 混入胜率**：战报里 PvE 297 场与平局 801 场都计入场次，`win_rate` = 胜 / 全部场次，
  不是纯 PvP 胜率；UI 与 CLI 的战报文案已注明「PvP x / PvE y，平局 z」。
- **demo 种子的战法配对是占位**：`cli.demo_seeds()` 取 `evidence_doc.skill_ids_json` 的前 6 个战法，
  而那是 `ingest.match_names()` 按正文出现顺序抽出来的战法名，与具体武将**没有对应关系**
  （源数据里根本没有「哪个将带哪个战法」）。所以社区阵容的真实战法配对无法还原，**不做推断**，
  demo 结果只代表队伍骨架能跑通。
- **低样本占比高**：`battle_stat` 里 `sample_flag=low_sample`（games < 5）的组占 **87.5%（960/1097）**，
  低样本的胜率只作参考；④ 页对低样本卡片会标「样本不足，仅供参考（N 场）」，评分不受影响。

## 实现注意（踩过的坑，别改回去）

1. **SQLite 连接必须跨线程可用**：Streamlit 1.66 的 `runner.fastReruns` 默认 `True`，每次 rerun 会换脚本线程；Python 的 `sqlite3.connect()` 默认 `check_same_thread=True`，所以 `db.connect()` 固定用
   `sqlite3.connect(path, timeout=30, check_same_thread=False)` + `busy_timeout=30000` + `journal_mode=WAL`。
   之前把连接塞进 `@st.cache_resource` 时，第二次交互就会抛
   `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`，
   而 `data_version()` 当时用 `except Exception: return 0` 把它吞掉，界面因此伪装成「数据库为空」。
   现在 `app.py` 每次 run 新建连接，初始化失败会 `st.error + st.exception + st.stop()`，不再静默。
2. **`streamlit run stzb_app/app.py` 没有父包上下文**：脚本被当 `__main__` 执行，`from . import x` 会 ImportError。
   `app.py` 先把项目根塞进 `sys.path`，再用 `from stzb_app import …` 绝对导入。
3. **WAL 边车文件**：`ingest.run(rebuild=True)` 必须同时删 `stzb.db-wal` / `stzb.db-shm`，否则留下陈旧 WAL。
4. **同名多形态武将**：`hero.name` 不能加 UNIQUE（391 行只有 299 个名字，孙坚×4、夏侯渊×4…），
   一律用 `hero_id` 作主键；界面给重名武将补 `#ID末三位` 区分。
5. **lineup 复用**：`lineup.fingerprint` 唯一索引 + `engine._persist` 先按指纹查再插入，保证重复跑推荐
   不会让 lineup 表倍数膨胀（`db._migrate()` 给老库补列）。指纹**按位置拼**
   （`位置:武将:该将战法` 段间 `#`），因为旧版把武将、战法各自排序后拼接会丢掉「谁带哪个战法」，
   不同队伍撞指纹后 `team_score` 会互相覆盖 —— 改过指纹算法后必须 `ingest --rebuild`，
   否则历史指纹对不上会重复插行。
6. **保存必须 commit，但是「整批一次」**：`engine.save_lineup()` 默认 `commit=True` 自己提交
   （Streamlit 每次交互都是独立 rerun、连接靠引用计数关闭，不 commit 的 INSERT 会被回滚，
   表现就是「点了保存、列表还是空」）。但 `engine._persist()` 一次推荐要写 lineup / team_score /
   reason_item / substitute 等多张表，它调用时传 `commit=False`，全部写完后自己 `conn.commit()`：
   中途提交的话，后面某条写失败就会在库里留下「有 lineup 没 team_score」的半截结果。
7. **手输检索框与勾选框并存**（`app.py` 的 `hero_search()` / `skill_search()`）：
   检索是纯内存子串匹配（直接 `in`，不走 SQL），多关键词空格分隔取 AND；检索字段含
   `名称/阵营/兵种/星级/标签（含 #ID末三位）`。两种选择方式在保存时取并集。
   保存后用 `owned_rev += 1` 换 widget key 重新渲染，**不要**去改 widget 的 `session_state`
   （Streamlit 不允许 widget 实例化后再改它的值）。
   检索框为空时不渲染「匹配项」控件，保存时也不会把上一次的隐藏选择算进来。
8. **`database is locked` 的根因是「每次 rerun 都重跑建表 + 种子」**：老版 `init_db()` 无条件
   `executescript(schema.sql)` + `DELETE FROM score_weight` + 重插 26 条权重，于是每开一个标签页、
   每次交互都在抢写锁；再加上 `PRAGMA journal_mode=WAL` 本身要短暂独占锁，多会话同时
   `connect()` 就会直接抛 `sqlite3.OperationalError: database is locked`（这个小延时
   `busy_timeout` 不一定兜得住）。现在的做法：
   - `PRAGMA user_version` 存 `SCHEMA_VERSION`（db.py），版本一致就**一次只读检查后直接返回**；
   - 真正建库时 `_retry_locked()` 指数退避重试 8 次；
   - `journal_mode` 先读再决定切不切，`busy_timeout=30000`，`synchronous=NORMAL`；
   - 种子权重改成「缺了才插」，不再每次 DELETE 重插。
   改 schema/种子后记得手动把 `SCHEMA_VERSION` +1（或跑 `ingest --rebuild`，它走 `force=True`）。
9. **连接不显式 close 是安全的，不是泄漏**：`app.py` 每次 rerun 新建连接、不调 `close()`，靠
   CPython 引用计数在旧连接对象被替换时立刻回收（`sqlite3.Connection` 的 `__del__` 会关闭底层句柄）。
   加 `weakref.finalize` / `atexit` 反而可能和 GC 抢着关。**结论：不改代码**；若换 PyPy（延迟 GC）
   或出现 `PermissionError: [WinError 32]`（文件被占用）时再改成显式 `close()`。

## 扩展点（B/C 形态）

`engine.expand_variants()` 是唯一产生候选的地方，A 模式用「换 1 将 / 换 1 战法」的邻域变体，
未来 B（按将自动配战法）/ C（全自动组队）只需替换这个函数（或新增 provider），
评分、校验、理由、持久化层都不用改。全空间约 4×10¹¹ 支，C 必须用 cost 上界剪枝 + beam search。
