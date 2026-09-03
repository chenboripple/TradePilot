FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    HOME=/tmp \
    TRADEPILOT_CONFIG=/app/config.yaml

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && addgroup --system tradepilot \
    && adduser --system --ingroup tradepilot --home /home/tradepilot tradepilot \
    && mkdir -p /app/data /app/output /var/lib/tradepilot \
    && chown -R tradepilot:tradepilot /app /home/tradepilot /var/lib/tradepilot

# heartbeat_tradepilot.py（自动网格再拟合）不进生产镜像：
# 无人工审批的线上参数自优化会持续制造过拟合，见 docs/project-analysis.md
COPY --chown=tradepilot:tradepilot monitor_brief.py ./
COPY --chown=tradepilot:tradepilot docker-entrypoint.sh /usr/local/bin/tradepilot-entrypoint

RUN chmod +x /usr/local/bin/tradepilot-entrypoint

USER tradepilot

EXPOSE 8000

ENTRYPOINT ["tradepilot-entrypoint"]
CMD ["uvicorn", "ripple_tradePilot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
