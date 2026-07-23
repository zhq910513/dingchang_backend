FROM mcr.microsoft.com/playwright:v1.49.1-jammy

ARG APT_MIRROR=
ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    REMOTE_DEBUGGING_PORT=9222

RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu/|$APT_MIRROR|g; s|http://security.ubuntu.com/ubuntu/|$APT_MIRROR|g" /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      fonts-noto-cjk \
      fonts-wqy-microhei \
      fonts-wqy-zenhei \
      tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /data/profile /data/artifacts

COPY entrypoint.sh /usr/local/bin/quote-browser-entrypoint
RUN chmod +x /usr/local/bin/quote-browser-entrypoint

EXPOSE 9222
ENTRYPOINT ["quote-browser-entrypoint"]

