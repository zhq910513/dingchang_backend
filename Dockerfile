FROM docker.m.daocloud.io/library/python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 基础工具：curl(健康检查用) + tzdata(时区)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# 安装依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 拷贝代码（只需要后端代码目录 app）
COPY app /app/app

# 确保存储/日志目录存在（也方便挂载）
RUN mkdir -p /app/storage /app/logs

EXPOSE 8000

# 生产启动：Gunicorn + Uvicorn Worker
# 注意：app.main:app 必须与你的 main.py 中 FastAPI 实例名一致（你现在就是 app=FastAPI(...) ✅）
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--graceful-timeout", "30", "--keep-alive", "5"]
