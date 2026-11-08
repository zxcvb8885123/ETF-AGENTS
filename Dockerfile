FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Taipei

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 只安裝鎖定檔（版本與 sha256 皆固定，與本機 .venv 相同）；requirements.txt 是人維護的版本範圍，
# 改動後要先重產 requirements.lock（見 docs/docker.md「依賴與重建」）。
COPY requirements.lock /app/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade "pip>=26.2.2" \
    && pip install --require-hashes -r /app/requirements.lock

ARG BUILD_INPUT_SHA=unknown
LABEL org.etf-agent.build-input-sha="${BUILD_INPUT_SHA}"

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent

COPY --chown=agent:agent . /app

USER agent

CMD ["python3", "scripts/check_setup.py"]
