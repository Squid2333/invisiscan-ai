FROM python:3.10-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /uvx /bin/

WORKDIR /app
COPY python/perception/pyproject.toml python/perception/uv.lock ./
RUN uv sync --frozen --no-dev

COPY python/gen ./python/gen
COPY python/perception ./perception
ENV PYTHONPATH=/app/python/gen
ENV PATH=/app/.venv/bin:$PATH
CMD ["python", "-m", "perception.mock_server"]
