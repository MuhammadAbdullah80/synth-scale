# Synth-Scale web front door.
# IMPORTANT: build from the REPO ROOT so the engine package is in context:
#   docker build -f web/Dockerfile -t synth-scale-web .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /srv

# Engine first (own layer: it changes less often than the web app).
# [postgres] pulls in psycopg2-binary, which powers the /api/connect
# ("Connect to Supabase") flow; without it the app still boots fine and
# /api/connect just returns a clean 503.
COPY datagen_pkg /srv/datagen_pkg
RUN pip install --no-cache-dir "/srv/datagen_pkg[postgres]"

COPY web/requirements.txt /srv/web/requirements.txt
RUN pip install --no-cache-dir -r /srv/web/requirements.txt

COPY web /srv/web

WORKDIR /srv/web
# Waitlist lives here; mount a persistent volume over it in production.
RUN mkdir -p /srv/web/data

EXPOSE 8000
# Single worker on purpose: the rate limiter and waitlist dedupe are
# in-process (see README "Scaling caveats").
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
