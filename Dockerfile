FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

ENV PORT=8129
EXPOSE 8129

# timeout alto: l'elaborazione di file grandi può richiedere minuti
CMD ["gunicorn", "-w", "2", "-t", "900", "-b", "0.0.0.0:8129", "app:app"]
