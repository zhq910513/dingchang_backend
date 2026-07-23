ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.10-slim
FROM ${PYTHON_IMAGE}

ARG DEBIAN_MIRROR=http://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app \
    DEBIAN_FRONTEND=noninteractive \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

# Base tools: curl, tzdata, and CJK fonts for result images.
RUN set -eux; \
    . /etc/os-release; \
    codename="${VERSION_CODENAME:-bookworm}"; \
    rm -f /etc/apt/sources.list.d/debian.sources; \
    printf 'deb %s %s main\n' "${DEBIAN_MIRROR}" "${codename}" > /etc/apt/sources.list; \
    printf 'deb %s %s-updates main\n' "${DEBIAN_MIRROR}" "${codename}" >> /etc/apt/sources.list; \
    printf 'deb %s %s-security main\n' "${DEBIAN_SECURITY_MIRROR}" "${codename}" >> /etc/apt/sources.list; \
    apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Shared env loader used by app startup and maintenance scripts.
COPY env_loader.py /app/env_loader.py

# Backend application code.
COPY app /app/app
COPY scripts /app/scripts

# Ensure runtime directories exist.
RUN mkdir -p /app/storage /app/logs

EXPOSE 8000

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--graceful-timeout", "30", "--keep-alive", "5"]
