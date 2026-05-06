ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.10-slim
FROM ${PYTHON_IMAGE}

ARG DEBIAN_MIRROR=http://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

# 基础工具：curl(健康检查用) + tzdata(时区)。默认使用国内 Debian 镜像源加速构建。
RUN set -eux; \
    . /etc/os-release; \
    codename="${VERSION_CODENAME:-bookworm}"; \
    rm -f /etc/apt/sources.list.d/debian.sources; \
    printf 'deb %s %s main\n' "${DEBIAN_MIRROR}" "${codename}" > /etc/apt/sources.list; \
    printf 'deb %s %s-updates main\n' "${DEBIAN_MIRROR}" "${codename}" >> /etc/apt/sources.list; \
    printf 'deb %s %s-security main\n' "${DEBIAN_SECURITY_MIRROR}" "${codename}" >> /etc/apt/sources.list; \
    apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# 安装依赖。PIP_INDEX_URL / PIP_TRUSTED_HOST 可在 docker build 时覆盖。
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# 拷贝代码（只需要后端代码目录 app）
COPY app /app/app

# 确保存储/日志目录存在（也方便挂载）
RUN mkdir -p /app/storage /app/logs

EXPOSE 8000

# 生产启动：Gunicorn + Uvicorn Worker
# 注意：app.main:app 必须与你的 main.py 中 FastAPI 实例名一致（你现在就是 app=FastAPI(...) ✅）
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--graceful-timeout", "30", "--keep-alive", "5"]
