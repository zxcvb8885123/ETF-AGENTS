FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Taipei

WORKDIR /app

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent

COPY --chown=agent:agent . /app

USER agent

CMD ["python3", "scripts/check_setup.py"]
