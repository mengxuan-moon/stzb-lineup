# 率土之滨 · 配将推荐 —— 通用容器镜像（VPS / NAS / 任意云主机都能跑）
# 构建： docker build -f deploy/Dockerfile -t stzb-lineup:1 .
# 运行： docker run -d --name stzb -p 8501:8501 -v stzb_data:/app/stzb_app/data stzb-lineup:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 依赖层单独一层，改代码不用重装依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# 数据库落在卷里：容器重建/升级不会丢「我的武将 / 我的战法 / 队伍 / 评分历史」
VOLUME ["/app/stzb_app/data"]

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "streamlit", "run", "stzb_app/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
