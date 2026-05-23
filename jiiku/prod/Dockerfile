ARG APP_VERSION=2026.1
ARG PYTHON_VERSION=3.14-slim
ARG UV_VERSION=0.10

# 1. Install uv (python)
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS builder_uv
FROM python:${PYTHON_VERSION} AS builder_python
COPY --from=builder_uv /uv /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=2

# Install project dependencies with build tools available
WORKDIR /build
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=./server/uv.lock,target=uv.lock \
    --mount=type=bind,source=./server/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev

FROM python:$PYTHON_VERSION
COPY --from=builder_python /build /build

# Install bazelisk and its dependencies for bazel builds
ENV APP_VERSION=$APP_VERSION
ENV PATH="/build/.venv/bin:$PATH"
ENV DEBIAN_FRONTEND=noninteractive
ENV BAZEL_CACHE="/home/sandbox/.cache/bazel" \
    PATH="$PATH:/home/sandbox/.local/bin" \
    TARGETARCH=amd64 \
    BAZEL_USE_CPP_ONLY_TOOLCHAIN=1
ARG BAZEL_VERSION=v1.28.1
RUN apt-get update && \
    apt-get install -y \
    zsh \
    git \
    ca-certificates \
    build-essential \
    wget \
    tini && \
    rm -rf /var/lib/apt/lists/*

# Install bazelisk
RUN wget -O /usr/local/bin/bazelisk https://github.com/bazelbuild/bazelisk/releases/download/${BAZEL_VERSION}/bazelisk-linux-$TARGETARCH && \
    chmod +x /usr/local/bin/bazelisk

# Add User
RUN groupadd --gid 1001 sandbox && \
    useradd --uid 1001 --gid sandbox -ms /bin/bash sandbox && \
    mkdir -p /home/sandbox/.cache/bazel && \
    chown -R sandbox:sandbox /home/sandbox

# Download sekai public
WORKDIR /app
RUN git clone -b main --depth=1 --single-branch https://github.com/Jiiku831/sekai-public.git && \
    chown -R sandbox:sandbox /app/sekai-public/

# Add files for sekai-public build
WORKDIR /app/sekai-public
COPY --chown=sandbox:sandbox patches/music_metas.json /app/sekai-public/music_metas.json
COPY --chown=sandbox:sandbox patches/BUILD.bazel /app/sekai-public/data/BUILD.bazel
COPY --chmod=755 --chown=sandbox:sandbox patches/build_python.sh /app/sekai-public/build_python.sh

# Build
USER sandbox

# Add files
WORKDIR /app/sekai-server
COPY ./server/ .

EXPOSE 3000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "main.py"]
