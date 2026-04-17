FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy application configs first
COPY pyproject.toml README.md alembic.ini ./

# Create a minimal directory structure to trick pip into installing dependencies only
RUN mkdir -p src/telemon && touch src/telemon/__init__.py && \
    pip install --upgrade pip && \
    pip install -e .

# NOW copy the actual source code (changes to src won't invalidate the pip cache above)
COPY src/ ./src/
COPY data/ ./data/
COPY alembic/ ./alembic/

# Create non-root user
RUN useradd --create-home --shell /bin/bash telemon && \
    chown -R telemon:telemon /app
USER telemon

# Run the bot
CMD ["python", "-m", "telemon.main"]
