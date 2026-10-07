# Shorts Studio

Local-first toolkit for turning long videos into polished vertical clips.

Shorts Studio combines transcription, scene analysis, clip ranking, reframing,
subtitles and export in one reproducible pipeline. Heavy processing runs locally,
and external publishing stays disabled until it is explicitly configured.

**Status: In Development.** The repository contains the processing pipeline,
web interface, desktop launchers and backend tests. The frontend TypeScript/Vite
build passed in the portfolio review. A complete Docker/media
processing run and live platform publishing were not verified in the portfolio
review; setup instructions are not a claim of production readiness.

## Highlights

- Local transcription with faster-whisper and VAD
- Scene detection and multimodal clip scoring
- Automatic 9:16 reframing with sampled face detection and a visual-detail fallback
- Word-level animated subtitles
- Profanity detection and audio masking
- H.264/AAC export with loudness normalization
- Resumable processing stages
- Optional YouTube, TikTok and Instagram connectors
- Windows desktop launcher and Docker-based web stack
- Dry-run publishing by default

## Architecture

```mermaid
flowchart LR
  UI[React UI] --> API[FastAPI]
  API --> DB[(PostgreSQL)]
  API --> Q[(Redis)]
  Q --> W[Celery worker]
  W --> T[Whisper transcription]
  T --> S[Scene and clip scoring]
  S --> E[Reframe, subtitles and export]
  E --> V[(Local media)]
  API --> P[Optional platform connectors]
```

## Stack

- Python 3.12, FastAPI, SQLAlchemy and Celery
- faster-whisper, PySceneDetect and OpenCV
- FFmpeg and yt-dlp
- React 19, TypeScript and Vite
- PostgreSQL and Redis
- Docker Compose

## Repository layout

```text
backend/
  app/       processing pipeline, API and platform connectors
  tests/     unit and integration tests
frontend/
  src/       React interface
tools/       packaging and validation helpers
desktop_app.py
docker-compose.yml
```

## Run with Docker

Requirements: Docker Desktop, at least 8 GB of RAM, and enough free disk space
for source media, models and rendered clips.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open `http://localhost:3000`. The proxied health endpoint is
`http://localhost:3000/api/health`.

FastAPI serves interactive documentation at `/docs` inside the API container.
The default nginx configuration proxies only `/api/`, so `/api/docs` is not a
documentation endpoint. To expose the docs locally, add a Compose override:

```yaml
# compose.docs.yml
services:
  api:
    ports:
      - "127.0.0.1:8000:8000"
```

```powershell
docker compose -f docker-compose.yml -f compose.docs.yml up --build
```

Then open `http://localhost:8000/docs`.

The first transcription run downloads the selected Whisper model. Set
`WHISPER_MODEL` in `.env` to balance speed and accuracy.

## Run the tests

```powershell
docker compose run --rm -e DATABASE_URL=sqlite:////tmp/shorts-tests.db -e STORAGE_ROOT=/tmp/shorts-tests -v "${PWD}/backend/tests:/app/tests:ro" api sh -lc "pip install '.[dev]' && pytest -q"
docker compose build frontend
```

The backend image does not copy `tests/`, so the test command mounts them from
the checkout. Tests drop and recreate database tables; the command overrides
the database URL with a disposable SQLite database inside the temporary
container. Never run these tests against a database containing your projects.
The frontend runtime image contains nginx and compiled assets;
its build stage runs TypeScript and Vite. For a source-level frontend check:

```powershell
Push-Location frontend
npm ci
npm run build
Pop-Location
```

## Platform credentials

Copy `.env.example` to `.env` and add credentials only on your own machine.
The `.env` file is ignored by Git. Real external publishing remains disabled
while `REAL_PUBLISHING_ENABLED=false`.

Use the software only with media you own or have permission to process. Platform
connectors do not bypass authorization, access controls or platform safeguards.

