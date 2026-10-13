FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# Runs as uid 1000 (the first user on most Linux installs) so files the file manager
# creates are owned by you. Change 1000 if your user's uid differs (`id -u`).
RUN useradd -m -u 1000 helm && mkdir -p /data && chown -R helm /app /data
USER helm

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
