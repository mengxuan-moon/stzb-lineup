# 率土之滨 · 配将推荐 —— 一键启动
#
# 用法（必须在 E:\dsh 或任何包含 stzb_app 子目录的目录里执行，例如）：
#     cd E:\dsh
#     .\stzb_app\start.ps1
#     # 或者： powershell -ExecutionPolicy Bypass -File E:\dsh\stzb_app\start.ps1
#
# 脚本自己会切到「stzb_app 的父目录」当项目根，并从这里跑 `python -m stzb_app.*`，
# 所以不依赖当前所在目录；但原始数据目录（stzb_hero_data / stzb_skill_data /
# stzb_team_eval）必须与 stzb_app 同级，否则导入阶段会直接报「缺少数据文件」。

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot   # stzb_app 的父目录（如 E:\dsh）
Set-Location $root
$env:PYTHONIOENCODING = 'utf-8'

$db = Join-Path $root 'stzb_app\data\stzb.db'
if (-not (Test-Path $db)) {
    Write-Host '数据库不存在，先导入数据（约 10~30 秒）…'
    python -m stzb_app.ingest
    if ($LASTEXITCODE -ne 0) { throw '导入失败，已中止启动' }
}

Write-Host "Starting Streamlit -> http://localhost:8501 （Ctrl+C 停止）"
# 用 `python -m streamlit` 而不是裸 `streamlit`：pip 装出来的 streamlit.exe 只在
# Scripts 目录里，PATH 没配好（本机就是这样）时会直接报「无法将 streamlit 识别为 cmdlet」。
python -m streamlit run stzb_app/app.py --server.headless true
