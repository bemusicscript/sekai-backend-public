ARG APP_VERSION=2026.1
ARG PYTHON_VERSION=3.14-slim
ARG UV_VERSION=0.10

## 1. Install uv (python)
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS builder_uv
FROM python:${PYTHON_VERSION} AS builder_python
COPY --from=builder_uv /uv /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=2

## 2. Install cron, sudo, rsyslog, procps, mariadb etc.
RUN apt update && \
    apt upgrade -y && \
    apt install -y --no-install-recommends \
    cron \
    tzdata \
    gosu \
    tini \
    rsyslog \
    ca-certificates \
    build-essential \
    procps \
    libmariadb-dev \
    gcc && \
    apt autoclean -y && \
    rm -rf /var/lib/apt/lists/*

## 3. Download and install dependencies
WORKDIR /build
RUN apt update && \
    apt install -y --no-install-recommends
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=app/uv.lock,target=uv.lock \
    --mount=type=bind,source=app/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --compile-bytecode

## 5. downgrade rsyslog privileges and remove kernel logs
##    as we run this inside the container.
RUN sed -i '/imklog/s/^/#/' /etc/rsyslog.conf

## 6. Set timezone to Asia/Tokyo
RUN ln -fs /usr/share/zoneinfo/Asia/Tokyo /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata
ENV TZ="Asia/Tokyo"
WORKDIR /usr/src

USER root
EXPOSE 5000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/init"]