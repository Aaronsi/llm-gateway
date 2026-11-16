FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_CONFIG=/app/config.yaml

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

COPY config.yaml ./config.yaml
COPY prompts ./prompts
RUN mkdir -p /app/data && chown -R 10001:10001 /app

USER 10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# 分层版为工厂函数：uvicorn --factory 零参调用 create_app()，
# 配置从 GATEWAY_CONFIG 环境变量加载（见 app/app.py 进程引导模式）。
CMD ["uvicorn", "--factory", "app.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
