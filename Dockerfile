FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Taipei

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade "pip>=26.2.2" \
    && pip install -r /app/requirements.txt

ARG BUILD_INPUT_SHA=unknown
LABEL org.etf-agent.build-input-sha="${BUILD_INPUT_SHA}"

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent

COPY --chown=agent:agent . /app

USER agent

CMD ["python3", "scripts/check_setup.py"]
