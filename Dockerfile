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

# Copy application code and configs first!
COPY pyproject.toml README.md alembic.ini ./
COPY src/ ./src/
COPY alembic/ ./alembic/

# NOW install Python dependencies and the bot itself
RUN pip install --upgrade pip && \
    pip install .

# Create non-root user
RUN useradd --create-home --shell /bin/bash telemon && \
    chown -R telemon:telemon /app
USER telemon

# Run the bot
CMD ["python", "-m", "telemon.main"]
