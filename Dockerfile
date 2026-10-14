FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# Runs as your user id (PUID, set by the installer; 1000 by default) so files the
# file manager creates are owned by you.
ARG PUID=1000
RUN uid="${PUID}"; [ "$uid" = "0" ] && uid=1000; \
    useradd -m -o -u "$uid" helm && mkdir -p /data && chown -R helm /app /data
USER helm

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
