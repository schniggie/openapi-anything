FROM python:3.11-slim

WORKDIR /app

# Install system deps for git/curl (docker SDK uses host socket, no daemon needed inside)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY README.md .
COPY src ./src
COPY registry.json* ./
COPY docker-compose.yml ./
RUN pip install --no-cache-dir .

# Expose gateway port
EXPOSE 8000

CMD ["python", "-m", "openapi_anything.cli", "serve"]
