FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app.py ./
COPY templates/ ./templates/

EXPOSE 8000
CMD ["uv", "run", "--no-dev", "--frozen", "fastapi", "run", "app.py", "--port", "8000"]
