# News Shorts Pipeline

Daily news → **vertical YouTube Shorts** (one Short per headline), with a local library dashboard.

Default sections: **Technology**, **Entertainment**, **Global News**, **Business**. Each run fetches **5 headlines** (`news_count`) into **one Short**. Scheduled at **10:00 PM local** per section timezone. Uses the same **Groq** narration API and **YouTube OAuth secrets** as [trends-video-pipeline](../trends-video-pipeline).

## Features

- Config-driven sections (`config/sections.yaml`) — add topics anytime
- Per-section `news_count` (default 5) → one Short covering those stories
- Google News topic RSS + related images (article og:image, Wikimedia, Openverse)
- Punchy narration via **Groq** (template fallback without a key)
- edge-tts + MoviePy 9:16 render (NVIDIA NVENC when available)
- Dashboard at `http://127.0.0.1:8081`
- YouTube upload to the **same channel** as the trends pipeline

## Prerequisites (Windows)

1. **Python 3.11+**
2. **FFmpeg**
3. **Groq API key** (optional) — [console.groq.com](https://console.groq.com/)
4. YouTube secrets from trends (see below)

## Quick Start

```powershell
cd C:\Users\USER\Projects\news-shorts-pipeline
.\scripts\setup-windows.ps1
```

Copy `.env` keys from trends (at least `GROQ_API_KEY`). YouTube OAuth clients are backed up by the personal **google-auth** skill:

```powershell
python "$env:USERPROFILE\.cursor\skills\google-auth\scripts\bootstrap.py" --from . --service youtube
python "$env:USERPROFILE\.cursor\skills\google-auth\scripts\sync.py" --project . --service youtube --write-yaml
python "$env:USERPROFILE\.cursor\skills\google-auth\scripts\status.py" --project . --service youtube
```

Upload failover: `youtube.clients` in `config/pipeline.yaml` tries OAuth clients in order when quota/auth fails. Authorize each once:

```powershell
.\.venv\Scripts\python.exe -m src.youtube.auth --client primary
.\.venv\Scripts\python.exe -m src.youtube.auth --client backup1
```

Add another later: put `secrets/clients/backup2/client_secrets.json`, append a yaml entry, then `auth --client backup2`.

Then:

```powershell
.\scripts\run.ps1
```

Open **http://127.0.0.1:8081**.

## Manual run

```powershell
.\.venv\Scripts\python.exe -m src.pipeline --section tech --mock
.\.venv\Scripts\python.exe -m src.pipeline --section tech --count 3
.\.venv\Scripts\python.exe -m src.pipeline --all
```

## Adding sections

Edit `config/sections.yaml` and restart. Set `google_topic`, `search_query`, or `rss_url`, plus `count`.

## Configuration

- `config/sections.yaml` — sections, counts, timezones
- `config/pipeline.yaml` — 1080×1920, duration, Groq, images, YouTube
- `.env` — `GROQ_API_KEY`, YouTube token (same as trends)

## Dashboard

| URL | Description |
|-----|-------------|
| `/` | Library by date + section filters |
| `/runs/{id}` | Detail: player, news, script |
| `POST /api/trigger/{code}` | Generate section batch |
| `POST /api/trigger-all` | Generate all sections |
| `POST /api/runs/{id}/upload` | Upload Short to YouTube |
| `DELETE /api/runs/{id}` | Delete run + files |
