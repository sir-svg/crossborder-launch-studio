FROM ghcr.io/astral-sh/uv:0.12.17 AS uv
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project \
    && useradd --uid 10001 --create-home studio
COPY studio studio
ENV PATH="/app/.venv/bin:$PATH"
USER studio
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "studio.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
