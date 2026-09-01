# News Shorts Pipeline

Daily news → **vertical YouTube Shorts** (9:16) with deep, calm, synchronized narration and a local dashboard.

Default sections: **Technology**, **Entertainment**, **Global News**, **Business**. Each run fetches **5 headlines** (`news_count`) into **one Short**.

---

## 🎙️ Narration & Synchronization

* **Primary TTS**: **Google Cloud Text-to-Speech (Chirp 3 HD)**
  * Voice: `en-US-Chirp3-HD-Fenrir` (Deep, calm, cinematic male voice).
  * High fidelity studio-quality speech with natural pacing.
* **Backup TTS**: **Microsoft Edge-TTS**
  * Voice: `en-US-ChristopherNeural` (Pitch: `-8Hz`, Rate: `-4%`).
  * Automatic zero-cost fallback if GCP credentials/quota are unavailable.
* **Synchronization**: Frame-accurate visual duration matching via `narration_segments.json`.

---

## 📦 Installation & Setup

### 1. Prerequisites
* **Python 3.11+**
* **FFmpeg & ffprobe** (`sudo pacman -S ffmpeg` or `sudo apt install ffmpeg`)

### 2. Environment Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure API Keys

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Ensure your `GOOGLE_API_KEY` is present in `.env`:
```ini
GOOGLE_API_KEY=AIzaSyAbVP...
GROQ_API_KEY=gsk_...
```

---

## 🚀 Running the Pipeline

### Start Web Dashboard
```bash
# Run server at http://127.0.0.1:8081
python3 -m src.main
```

### Manual Trigger CLI
```bash
# Generate single section (e.g. Technology)
python3 -m src.pipeline --section tech

# Generate all sections
python3 -m src.pipeline --all

# Test run with mock assets
python3 -m src.pipeline --section tech --mock
```

---

## ⚙️ Configuration Files

* **`config/sections.yaml`**: Configures news sections, regions, headline counts, and schedules.
* **`config/pipeline.yaml`**: Video dimensions (1080x1920), TTS voice presets, and YouTube upload settings.
* **`.env`**: API keys for Google Cloud TTS, LLMs, and YouTube tokens.
