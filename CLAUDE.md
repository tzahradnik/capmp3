# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Spuštění lokálně
streamlit run app.py

# Instalace závislostí
pip3 install -r requirements.txt

# Build Docker obrazu
docker build -t capmp3 .

# Deploy na Railway
railway up --detach
```

> ffmpeg musí být přítomen: `brew install ffmpeg` (macOS) nebo `sudo apt install ffmpeg` (Linux). Alternativně se automaticky použije bundlovaný binár z `imageio-ffmpeg`, pokud je nainstalovaný.

## Architektura

Celá aplikace je v jediném souboru `app.py` (~300 řádků). Neexistují žádné testy ani více modulů.

### Tok dat

```
URL vstup → find_video_url() → download → ffmpeg → MP3 bytes → st.download_button
```

### cap.so API – klíčový detail

cap.so používá React Server Components – HTML stránka **neobsahuje** `__NEXT_DATA__`. Video URL se získá voláním interního API:

```
GET https://cap.so/api/playlist?videoId={id}&videoType=master
Referer: https://cap.so/s/{id}   ← povinný header, bez něj vrátí 400
→ HTTP 302 → Location: https://v.cap.so/{orgId}/{id}/result.mp4?Policy=...
```

Dostupné `videoType` hodnoty: `audio`, `segments-audio`, `master`, `mp4`, `video`, `raw-preview`, `segments-master`, `segments-video`. Aplikace zkouší nejdřív `audio`/`segments-audio` (menší soubor), pak fallback na `master`.

### Dvě větve zpracování

- **`audio_only = True`** (audio endpoint < 50 MB): `download_audio_direct()` → stáhne raw audio → ffmpeg konverze do MP3
- **`audio_only = False`** (plné video): `download_video()` → stáhne MP4 → `extract_audio_local()` ffmpeg

### ffmpeg detekce při startu

`FFMPEG = _get_ffmpeg()` se vyhodnotí jednou při importu modulu: systémový `shutil.which("ffmpeg")` → `imageio_ffmpeg.get_ffmpeg_exe()` → fallback string `"ffmpeg"`.

### Dočasné soubory

Vše probíhá uvnitř `tempfile.TemporaryDirectory()`. MP3 se před smazáním složky načte do `bytes` a předá do `st.download_button(data=audio_bytes)`.

## Deploy

- **Hosting:** Railway, Dockerfile builder
- **PORT:** CMD používá `sh -c "... --server.port=${PORT:-8080}"` – nutná shell forma pro expanzi env var
- **ffmpeg v produkci:** instalován přes `apt` v Dockerfile, `imageio-ffmpeg` v produkci není potřeba
- **GitHub repo:** `git@github.com:tzahradnik/capmp3.git` – každý push na `main` spustí nový deploy přes `railway up`
