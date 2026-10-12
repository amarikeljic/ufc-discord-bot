FROM python:3.13-slim

# tzdata because the hourly pass does its scheduled-event sync when it lands at
# midnight America/Chicago, read through zoneinfo from the system database.
# Without it the bot still starts but falls back to a fixed CST offset and
# drifts an hour over the summer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not reinstall the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# careers.py too: it fills the professional-career cache the model trains on,
# and it has to be runnable in the container that owns the data directory.
COPY bot.py train.py careers.py ./
COPY ufcbot ./ufcbot

# Logs go straight to `docker logs` rather than sitting in a buffer, which
# matters for a service you only ever watch through Docker.
# The database belongs on the mounted volume. The application default is
# relative, which inside this image resolves to /app/ufcbot.sqlite3 -- a path
# the next `docker rm` takes with it, along with every pick anybody has made.
# Setting it here means the trap cannot be fallen into by forgetting a line in
# .env, which is the only thing that was standing between it and happening.
ENV DATABASE_PATH=/app/data/ufcbot.sqlite3 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# No USER: the container writes the database, the lock, the dataset and the
# model into the bind-mounted /app/data, and running as a uid that does not
# match the directory's owner on the host would leave it unable to start.
CMD ["python", "bot.py"]
