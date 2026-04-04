FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY bot.py config.py db.py ./
COPY cogs/ cogs/
COPY assets/ assets/

CMD ["uv", "run", "--no-sync", "python", "bot.py"]
