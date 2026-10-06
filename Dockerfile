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

# 依赖热层：requirements.txt 是 pyproject 的 freeze 快照，只有它变化时才重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 源码冷层：改代码不触发依赖重装，--no-deps 保证以快照版本为准
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && addgroup --system tradepilot \
    && adduser --system --ingroup tradepilot --home /home/tradepilot tradepilot \
    && mkdir -p /app/data /app/output /var/lib/tradepilot \
    && chown -R tradepilot:tradepilot /app /home/tradepilot /var/lib/tradepilot

# 自动网格再拟合（tradepilot heartbeat）不在镜像内排程：默认停用，
# 须人工显式 --allow-refit / TRADEPILOT_AUTOFIT=1 才执行——无人工审批的
# 线上参数自优化会持续制造过拟合，见 docs/project-analysis.md
COPY --chown=tradepilot:tradepilot monitor_brief.py ./
COPY --chown=tradepilot:tradepilot docker-entrypoint.sh /usr/local/bin/tradepilot-entrypoint

RUN chmod +x /usr/local/bin/tradepilot-entrypoint

USER tradepilot

EXPOSE 8000

ENTRYPOINT ["tradepilot-entrypoint"]
CMD ["uvicorn", "ripple_tradePilot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
