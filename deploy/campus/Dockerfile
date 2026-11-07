FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy DEBIAN_FRONTEND=noninteractive
WORKDIR /app
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Retries "2";\nAcquire::https::Timeout "20";\nAcquire::http::Timeout "20";\n' > /etc/apt/apt.conf.d/80-zhixu-network \
    && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl libreoffice-writer libreoffice-calc libreoffice-impress fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir uv==0.11.16
COPY pyproject.toml uv.lock README.md /app/
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/akb/config.py backend/akb/tencent_runtime.py /app/backend/akb/
COPY scripts/setup_tencent_memory.py /app/scripts/
COPY integrations/tencent-memory /app/integrations/tencent-memory
RUN --mount=type=cache,target=/var/cache/zhixu-archives --mount=type=cache,target=/root/.npm \
    PYTHONPATH=/app/backend .venv/bin/python scripts/setup_tencent_memory.py --cache-dir /var/cache/zhixu-archives
COPY . /app
RUN uv sync --frozen --no-dev \
    && useradd --uid 10001 --create-home zhixu \
    && mkdir -p /app/data /app/secrets /app/work/workbuddy-private-packages \
    && chown 10001:10001 /app/data /app/secrets /app/work/workbuddy-private-packages
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=8s --start-period=40s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import httpx; [httpx.get(u,timeout=2,trust_env=False).raise_for_status() for u in ('http://127.0.0.1:5876/healthz','http://127.0.0.1:8420/health','http://127.0.0.1:8421/health')]"]
CMD ["/app/.venv/bin/python", "scripts/dev.py"]
