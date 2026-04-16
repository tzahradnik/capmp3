"""
CapMP3 - Extraktor zvuku z cap.so / cap.link
Spuštění: streamlit run app.py
"""

import os
import re
import tempfile
import subprocess
from urllib.parse import urlparse

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# ffmpeg – použij systémový nebo bundlovaný přes imageio-ffmpeg
# ---------------------------------------------------------------------------

def _get_ffmpeg() -> str:
    import shutil
    system_ff = shutil.which("ffmpeg")
    if system_ff:
        return system_ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"

FFMPEG = _get_ffmpeg()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Extrakce video URL
# ---------------------------------------------------------------------------

def _extract_video_id(url: str) -> str | None:
    """
    Vytáhne videoId z cap.so nebo cap.link URL.
    Příklady:
      cap.link/60k465ry49jpktj  → 60k465ry49jpktj
      cap.so/s/60k465ry49jpktj  → 60k465ry49jpktj
    """
    path = urlparse(url).path.rstrip("/")
    segment = path.split("/")[-1]
    # videoId jsou alfanumerické řetězce (bez přípony)
    if re.match(r"^[a-z0-9]{8,}$", segment, re.IGNORECASE):
        return segment
    return None


def _cap_api_redirect(video_id: str, final_url: str, video_type: str) -> str | None:
    """Zavolá cap.so API a vrátí Location URL z redirectu, nebo None."""
    api_url = f"https://cap.so/api/playlist?videoId={video_id}&videoType={video_type}"
    resp = requests.get(
        api_url,
        headers={**HEADERS, "Referer": final_url, "Origin": "https://cap.so"},
        timeout=20,
        allow_redirects=False,
    )
    if resp.status_code in (301, 302, 303, 307, 308):
        return resp.headers.get("location")
    return None


def get_cap_video_url(page_url: str) -> tuple[str, bool]:
    """
    Vrátí (cdn_url, je_audio_only).
    Nejdřív zkusí audio endpoint (malý soubor), pak fallback na plné video.
    """
    resp = requests.get(page_url, headers=HEADERS, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    final_url = resp.url

    video_id = _extract_video_id(final_url)
    if not video_id:
        raise ValueError(f"Nepodařilo se extrahovat videoId z URL: {final_url}")

    # 1. Zkus audio-only endpoint (výrazně menší soubor)
    for vtype in ("audio", "segments-audio"):
        url = _cap_api_redirect(video_id, final_url, vtype)
        if url:
            # Ověř, že je to skutečně audio soubor (HEAD request)
            try:
                head = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
                ct = head.headers.get("content-type", "")
                size = int(head.headers.get("content-length", 0))
                # Pokud je soubor výrazně menší než typické video, použij ho
                if "audio" in ct or (size > 0 and size < 50 * 1024 * 1024):
                    return url, True
            except Exception:
                pass

    # 2. Fallback na plné video (audio extrahujeme přes ffmpeg stream)
    url = _cap_api_redirect(video_id, final_url, "master")
    if url:
        return url, False

    raise ValueError(
        "cap.so API nevrátilo platnou URL. "
        "Video může být soukromé nebo URL neplatná."
    )


def get_video_url_generic(page_url: str) -> str:
    """
    Fallback pro ostatní platformy přes yt-dlp.
    """
    try:
        import yt_dlp
        ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "format": "bestaudio/best"}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            if info and info.get("url"):
                return info["url"]
    except Exception as e:
        raise ValueError(f"yt-dlp nepodporuje tuto platformu: {e}") from e
    raise ValueError("yt-dlp nenašel video URL.")


def find_video_url(url: str) -> tuple[str, str, bool]:
    """
    Vrátí (video_url, metoda, je_audio_only) nebo vyvolá ValueError.
    """
    parsed = urlparse(url)
    is_cap = any(h in parsed.netloc for h in ["cap.so", "cap.link"])

    if is_cap:
        video_url, audio_only = get_cap_video_url(url)
        label = "cap.so API (audio)" if audio_only else "cap.so API (video)"
        return video_url, label, audio_only

    video_url = get_video_url_generic(url)
    return video_url, "yt-dlp", False


# ---------------------------------------------------------------------------
# Stahování + extrakce zvuku
# ---------------------------------------------------------------------------

def download_audio_direct(source_url: str, audio_path: str, progress_bar) -> None:
    """
    Stáhne audio-only soubor (malý) přímo.
    """
    resp = requests.get(source_url, headers=HEADERS, stream=True, timeout=60)
    resp.raise_for_status()
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    tmp_audio = audio_path.replace(".mp3", "_raw")
    with open(tmp_audio, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    mb = downloaded / 1024 / 1024
                    progress_bar.progress(min(downloaded / total, 0.9), f"Stahuji audio… {mb:.1f} MB")
    progress_bar.progress(0.95, "Konvertuji do MP3…")
    cmd = [FFMPEG, "-y", "-i", tmp_audio, "-acodec", "libmp3lame", "-q:a", "2", "-ar", "44100", audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    os.remove(tmp_audio)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg chyba:\n{result.stderr[-600:]}")
    progress_bar.progress(1.0, "Hotovo!")


def download_video(video_url: str, dest_path: str, progress_bar) -> None:
    """Stáhne video soubor se zobrazením průběhu (streaming, bez omezení rychlosti)."""
    resp = requests.get(video_url, headers=HEADERS, stream=True, timeout=(15, None))
    resp.raise_for_status()

    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0

    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=512 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = min(downloaded / total, 1.0)
                    mb = downloaded / 1024 / 1024
                    progress_bar.progress(pct, f"Stahuji… {mb:.1f} / {total/1024/1024:.0f} MB")

    progress_bar.progress(1.0, "Video staženo.")


def extract_audio_local(video_path: str, audio_path: str, progress_bar) -> None:
    """Extrahuje MP3 z lokálního video souboru přes ffmpeg."""
    progress_bar.progress(0.1, "Extrahuji audio…")
    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        "-ar", "44100",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg chyba:\n{result.stderr[-600:]}")
    progress_bar.progress(1.0, "Hotovo!")


def check_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="CapMP3 – Extraktor zvuku", page_icon="🎵", layout="centered")

st.title("🎵 CapMP3")
st.markdown("Zadej URL záznamu z **cap.so** nebo **cap.link** a stáhni zvuk jako MP3.")

if not check_ffmpeg():
    st.error(
        "⚠️ **ffmpeg nenalezen.**\n\n"
        "- **macOS:** `brew install ffmpeg`\n"
        "- **Ubuntu/Debian:** `sudo apt install ffmpeg`\n"
        "- **Windows:** stáhnout z [ffmpeg.org](https://ffmpeg.org/download.html)"
    )
    st.stop()

url_input = st.text_input(
    "URL záznamu",
    placeholder="https://cap.link/xxxxxxxx nebo https://cap.so/s/xxxxxxxx",
)

if st.button("⬇️ Stáhnout MP3", type="primary"):
    url = url_input.strip()

    if not url:
        st.warning("Zadej URL záznamu.")
        st.stop()
    if not url.startswith(("http://", "https://")):
        st.error("Zadej platnou URL adresu začínající http:// nebo https://")
        st.stop()

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "video.mp4")
        audio_path = os.path.join(tmpdir, "audio.mp3")
        audio_bytes = None

        try:
            with st.status("Hledám URL videa…", expanded=True) as status:
                video_url, method, audio_only = find_video_url(url)
                st.caption(f"✓ Nalezeno přes: **{method}**")
                status.update(label="URL videa nalezena ✓", state="complete")

            if audio_only:
                st.write("**Stahuji audio stopu…**")
                bar = st.progress(0, "Připravuji…")
                download_audio_direct(video_url, audio_path, bar)
            else:
                st.write("**Stahuji video…**")
                dl_bar = st.progress(0, "Připravuji…")
                download_video(video_url, video_path, dl_bar)

                st.write("**Extrahuji audio do MP3…**")
                audio_bar = st.progress(0, "Spouštím ffmpeg…")
                extract_audio_local(video_path, audio_path, audio_bar)

            st.success("✅ MP3 je připraveno ke stažení!")
            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

        except requests.HTTPError as e:
            st.error(f"HTTP chyba: {e}")
            st.stop()
        except ValueError as e:
            st.error(str(e))
            st.stop()
        except RuntimeError as e:
            st.error(str(e))
            st.stop()
        except Exception as e:
            st.error(f"Neočekávaná chyba: {e}")
            st.stop()

    if audio_bytes:
        st.download_button(
            label="💾 Stáhnout MP3",
            data=audio_bytes,
            file_name="cap_audio.mp3",
            mime="audio/mpeg",
        )

st.divider()
st.caption("Podporované platformy: cap.so, cap.link · Dočasné soubory jsou automaticky mazány.")
