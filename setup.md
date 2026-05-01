# Chart Cyanvas — Personal Setup Guide

> Notes compiled from setup research. Covers running the server locally/on a VPS,
> how charts get into the system, and how to bulk-import downloaded charts.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Running Locally (Development)](#running-locally-development)
3. [Running on a Server (Production)](#running-on-a-server-production)
4. [How Charts Get Into the System](#how-charts-get-into-the-system)
5. [Bulk Importing Charts](#bulk-importing-charts)

---

## Architecture Overview

This is a multi-service monorepo. Every piece needs to be running for the server to work:

| Service | Directory | Tech | Dev Port |
|---|---|---|---|
| Frontend | `frontend/` | Remix + Tailwind | 3100 |
| Backend | `backend/` | Ruby on Rails | 3000 |
| Background jobs | `backend/` | Sidekiq | — |
| Audio processing | `sub-audio/` | Python + FastAPI + ffmpeg | 3202 |
| Image processing | `sub-image/` | Rust + axum | 3203 |
| Chart processing | `sub-chart/` | TypeScript + Hono | 3201 |
| Temp storage | `sub-temp-storage/` | Rust + axum | 3204 |
| Wiki | `wiki/` | Vitepress | 3101 |

External services (run via Docker):
- **PostgreSQL** — main database
- **Redis** — background job queue + session cache
- **MinIO** — S3-compatible file storage (dev only; use real S3 in prod)
- **OpenObserve** — observability/logging (optional)

---

## Running Locally (Development)

### Step 1 — Install Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Linux / WSL2 | Any | macOS may not work |
| Ruby | 3.4 | Use `rbenv` or `mise` |
| Bundler | latest | `gem install bundler` |
| Python | 3.12 | [python.org](https://python.org) |
| uv | latest | `curl -Ls https://astral.sh/uv/install.sh \| sh` |
| Node.js | 22 | Use `nvm` |
| pnpm | latest | `npm install -g pnpm` |
| Rust | ≥ 1.71 | [rustup.rs](https://rustup.rs) |
| Docker | latest | [docker.com](https://docker.com) |
| goreman | latest | `go install github.com/mattn/goreman@latest` (needs Go) |
| make | latest | `sudo apt install make` |

---

### Step 2 — Configure the App

```bash
cp config.dev.yml config.yml
```

Open `config.yml` and set at minimum:

- **`admin_handle`** — your Sonolus username (gives you admin access on your instance)
- **`final_host`** — the public URL this will be reachable from (e.g. a cloudflared tunnel like `https://xxxx.trycloudflare.com`). Must NOT be `localhost` — Sonolus connects from your phone.
- S3/Redis/Postgres settings can stay as-is (Docker handles them in dev)

> **Discord integration is optional.** Leave the `discord:` block commented out in `config.yml` to skip it.

---

### Step 3 — Generate a Secret Key

```bash
rake secret
```

Copy the output line (e.g. `secret_key_base: "abc123..."`) and paste it into `config.yml`.

---

### Step 4 — Generate the `.env` File

```bash
rake configure
```

Reads `config.yml` and produces a `.env` file with all environment variables. **Re-run this every time you change `config.yml`.**

---

### Step 5 — Install All Dependencies

```bash
rake install
```

Runs `bundle install` (Ruby), `uv sync` (Python), and `pnpm install` (Node.js) across all sub-projects.

---

### Step 6 — Start External Services (Docker)

```bash
cp docker-compose.dev.yml docker-compose.yml
docker compose up -d
```

Starts PostgreSQL, Redis, MinIO, and OpenObserve.

---

### Step 7 — Run Database Migrations

```bash
cd backend && bundle exec rails db:migrate
cd ..
```

---

### Step 8 — Start All Dev Servers

```bash
goreman start
```

Launches all 8 services at once from the `Procfile`. Access the site at **http://localhost:3100**.

---

### Making It Accessible to Sonolus

Sonolus connects from your phone, so it needs a public HTTPS URL. Use a tunnel:

```bash
# Option A: cloudflared (free, no account needed)
cloudflared tunnel --url http://localhost:3100

# Option B: ngrok
ngrok http 3100
```

Then update `final_host` in `config.yml` with the tunnel URL and re-run `rake configure`.

---

## Running on a Server (Production)

### Step 1 — Configure

```bash
cp config.prod.yml config.yml
# Edit config.yml with your real domain, S3 credentials, etc.
rake secret       # paste result into config.yml
rake configure
```

### Step 2 — Set Up Docker Compose

```bash
cp docker-compose.prod.yml docker-compose.yml
```

> **Note:** The default `docker-compose.prod.yml` pulls images from `ghcr.io` (the original author's registry).
> If you forked the repo you need to build your own images using `docker-compose.build.yml` and update the image URLs.

### Step 3 — Launch Everything

```bash
docker compose up -d
```

All services (backend, frontend, sub-services) start as Docker containers.

---

## How Charts Get Into the System

**There is no folder to drop files into.** Charts are uploaded through the web UI or API.

### Normal Upload Flow

1. Someone logs into your instance (via Sonolus handle auth)
2. They go to the frontend and click **Upload Chart**
3. They fill out a form and upload three files:
   - **Chart file** — `.sus`, `.mmws`, `.chs`, `.vusc`, or `.ccmmws`
   - **Cover image** — jacket art
   - **BGM** — audio file
4. The backend kicks off processing jobs:
   - `ChartConvertJob` → processes chart via **sub-chart** (TypeScript/Sonolus engine)
   - `BgmConvertJob` → converts audio via **sub-audio** (Python/ffmpeg)
   - `ImageConvertJob` → generates backgrounds via **sub-image** (Rust)
5. Processed files are stored in MinIO/S3 and the chart appears in the listing

### Supported Chart Formats

| Format | Extension |
|---|---|
| SUS | `.sus` |
| MMWS | `.mmws` |
| CHS | `.chs` |
| VUSC / USC | `.vusc` |
| CCMMWS | `.ccmmws` |

### Who Can Upload?

- Anyone who logs in with a Sonolus handle (unless Discord integration is enabled, which requires linking a Discord account)
- The handle set as `admin_handle` in `config.yml` gets admin privileges

---

## Bulk Importing Charts

Use `bulk_chart_importer.py` to automatically import a folder of downloaded charts.

### What the Script Expects

Each chart folder (e.g. `chcy-3226CK2pAdmcg2SMPFHpvxA/`) should contain:

| File | Purpose |
|---|---|
| `level.json` | Metadata (title, rating, composer, artist, tags) |
| `jacket.jpg` | Cover image |
| `music.mp3` | BGM audio |
| `score.usc` | Chart file (also detects `.sus`, `.mmws`, `.chs`, `.ccmmws`) |

### About the URLs in `level.json`

**You do not need to rewrite any URLs.** The script uploads the actual local binary files
(`jacket.jpg`, `music.mp3`, `score.usc`) directly. The `https://cc-cdn.sevenc7c.com/...`
URLs inside `level.json` are completely ignored — they were only needed by the downloader
to fetch those files, which already happened. Your server re-stores everything in its own
MinIO bucket and generates its own URLs.

---

### Step 1 — Get Your Session Cookie

The script authenticates using the same session cookie as your browser (since Sonolus
auth can't be automated headlessly).

1. Start your Chart Cyanvas server and log in via Sonolus in your browser
2. Open **DevTools** (F12) → **Application** → **Cookies** → select your localhost URL
3. Find the cookie named `_session_id` and **copy its value**

---

### Step 2 — Dry Run First (Recommended)

With 17,000+ charts, test a small batch before doing the full import:

```bash
python3 bulk_chart_importer.py \
  --base-url http://localhost:3100 \
  --session "YOUR_SESSION_COOKIE_VALUE" \
  --author-handle "YOUR_SONOLUS_HANDLE" \
  --limit 5 \
  --dry-run
```

---

### Step 3 — Full Import

```bash
python3 bulk_chart_importer.py \
  --base-url http://localhost:3100 \
  --session "YOUR_SESSION_COOKIE_VALUE" \
  --author-handle "YOUR_SONOLUS_HANDLE" \
  --charts-dir "/run/media/carter/AC6C576C6C573076/random projects/chart-downloader/out/chcy/" \
  --visibility public \
  --genre others \
  --delay 2
```

---

### All Options

| Flag | Default | Description |
|---|---|---|
| `--base-url` | `http://localhost:3100` | URL of your Chart Cyanvas instance |
| `--session` | *(required)* | Value of the `_session_id` cookie |
| `--author-handle` | *(required)* | Your Sonolus handle (set as chart author) |
| `--charts-dir` | *(the chcy path)* | Directory containing `chcy-XXXX` folders |
| `--visibility` | `public` | `public`, `private`, or `scheduled` |
| `--genre` | `others` | Default genre for all charts |
| `--delay` | `1.5` | Seconds between uploads (be gentle on the server) |
| `--dry-run` | off | Preview without uploading |
| `--limit` | `0` (no limit) | Stop after N charts |
| `--resume-after` | — | Skip all folders up to this folder name, then start |
| `--log-file` | `import_log.jsonl` | JSON-lines log of every result |

---

### Resuming After a Crash

The log file (`import_log.jsonl`) records every result. If the import stops (session
expired, error, etc.), find the last successful folder name in the log and resume:

```bash
python3 bulk_chart_importer.py \
  --resume-after "chcy-LAST_SUCCESSFUL_FOLDER_NAME" \
  ... (other flags)
```

---

### Time Estimate

| Delay | Time for ~18,000 charts |
|---|---|
| 2.0s | ~10 hours |
| 1.5s | ~7.5 hours |
| 0.5s | ~2.5 hours |

Run inside `tmux` or `screen` so it keeps going if you disconnect:

```bash
tmux new -s import
# run the script
# Ctrl+B then D to detach
# tmux attach -t import to come back
```

---

### Session Expiry

If the session cookie expires mid-import, the script will print an `AUTH ERROR` and stop
cleanly. Re-login in your browser, grab a fresh cookie, and re-run with `--resume-after`
pointing to the last successful chart folder.
