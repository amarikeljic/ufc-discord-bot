FROM python:3.13-slim

# tzdata because the nightly job is scheduled on America/Chicago through
# zoneinfo, which reads the system database. Without it the bot still starts but
# falls back to a fixed CST offset and drifts an hour over the summer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not reinstall the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py train.py ./
COPY ufcbot ./ufcbot

# Logs go straight to `docker logs` rather than sitting in a buffer, which
# matters for a service you only ever watch through Docker.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# No USER: the container writes the database, the lock, the dataset and the
# model into the bind-mounted /app/data, and running as a uid that does not
# match the directory's owner on the host would leave it unable to start.
CMD ["python", "bot.py"]
