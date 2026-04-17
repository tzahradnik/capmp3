"""
CapMP3 - Extraktor zvuku z cap.so / cap.link
Spuštění: streamlit run app.py
"""

import gc
import os
import re
import tempfile
import subprocess
from urllib.parse import urlparse

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------

MAX_VIDEO_MB = 400          # Maximální velikost videa ke stažení
FREE_DOWNLOAD_LIMIT = 1     # Počet bezplatných stažení
STRIPE_PAYMENT_URL = "https://buy.stripe.com/REPLACE_ME"  # TODO: doplnit Stripe Checkout URL

# ---------------------------------------------------------------------------
# ffmpeg – systémový nebo bundlovaný přes imageio-ffmpeg
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
# Extrakce URL videa (cap.so API)
# ---------------------------------------------------------------------------

def _extract_video_id(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    segment = path.split("/")[-1]
    if re.match(r"^[a-z0-9]{8,}$", segment, re.IGNORECASE):
        return segment
    return None


def _cap_api_redirect(video_id: str, final_url: str, video_type: str) -> str | None:
    """Zavolá cap.so API a vrátí Location URL z 302 redirectu."""
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


def _check_video_size(url: str) -> int:
    """
    Zjistí velikost souboru přes HEAD request.
    Vyvolá ValueError pokud přesahuje MAX_VIDEO_MB.
    Vrátí velikost v bajtech (0 pokud neznámá).
    """
    try:
        head = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
        size = int(head.headers.get("content-length", 0))
        if size > MAX_VIDEO_MB * 1024 * 1024:
            raise ValueError(
                f"Video je příliš velké ({size // 1024 // 1024} MB). "
                f"Maximum je {MAX_VIDEO_MB} MB."
            )
        return size
    except requests.RequestException:
        return 0  # Neznámá velikost → pokračuj


def get_cap_video_url(page_url: str) -> tuple[str, bool]:
    """
    Vrátí (cdn_url, je_audio_only).
    Priorita: audio endpoint (menší soubor) → fallback na master video.
    """
    resp = requests.get(page_url, headers=HEADERS, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    final_url = resp.url

    video_id = _extract_video_id(final_url)
    if not video_id:
        raise ValueError(f"Nepodařilo se extrahovat videoId z URL: {final_url}")

    # Zkus audio-only endpoint (výrazně menší soubor)
    for vtype in ("audio", "segments-audio"):
        url = _cap_api_redirect(video_id, final_url, vtype)
        if url:
            try:
                head = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
                ct = head.headers.get("content-type", "")
                size = int(head.headers.get("content-length", 0))
                if "audio" in ct or (size > 0 and size < 50 * 1024 * 1024):
                    return url, True
            except Exception:
                pass

    # Fallback na plné video
    url = _cap_api_redirect(video_id, final_url, "master")
    if url:
        return url, False

    raise ValueError(
        "cap.so API nevrátilo platnou URL. "
        "Video může být soukromé nebo URL neplatná."
    )


def get_video_url_generic(page_url: str) -> str:
    """Fallback pro ostatní platformy přes yt-dlp."""
    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "bestaudio/best",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            if info and info.get("url"):
                return info["url"]
    except Exception as e:
        raise ValueError(f"yt-dlp nepodporuje tuto platformu: {e}") from e
    raise ValueError("yt-dlp nenašel video URL.")


def find_video_url(url: str) -> tuple[str, str, bool]:
    """Vrátí (video_url, metoda, je_audio_only)."""
    parsed = urlparse(url)
    is_cap = any(h in parsed.netloc for h in ["cap.so", "cap.link"])

    if is_cap:
        video_url, audio_only = get_cap_video_url(url)
        label = "cap.so API (audio)" if audio_only else "cap.so API (video)"
        return video_url, label, audio_only

    video_url = get_video_url_generic(url)
    return video_url, "yt-dlp", False


# ---------------------------------------------------------------------------
# Stahování a zpracování – streaming z disku, bez držení v RAM
# ---------------------------------------------------------------------------

def download_to_file(source_url: str, dest_path: str, progress_bar, label: str) -> None:
    """
    Streamingové stahování do souboru (512 KB chunky).
    Před stažením ověří velikost přes HEAD request.
    """
    _check_video_size(source_url)

    resp = requests.get(source_url, headers=HEADERS, stream=True, timeout=(15, None))
    resp.raise_for_status()

    total = int(resp.headers.get("Content-Length", 0))

    # Druhá kontrola velikosti z Content-Length streamu
    if total > MAX_VIDEO_MB * 1024 * 1024:
        resp.close()
        raise ValueError(
            f"Soubor je příliš velký ({total // 1024 // 1024} MB). "
            f"Maximum je {MAX_VIDEO_MB} MB."
        )

    downloaded = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=512 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = min(downloaded / total, 1.0)
                    mb_done = downloaded / 1024 / 1024
                    mb_total = total / 1024 / 1024
                    progress_bar.progress(pct, f"{label} {mb_done:.1f} / {mb_total:.0f} MB")
                else:
                    mb_done = downloaded / 1024 / 1024
                    progress_bar.progress(0.5, f"{label} {mb_done:.1f} MB…")

    progress_bar.progress(1.0, f"{label} dokončeno.")


def convert_to_mp3(input_path: str, output_path: str, progress_bar, label: str) -> None:
    """
    Konvertuje/extrahuje audio do MP3 přes ffmpeg subprocess.
    Timeout 300 s – zabrání zombie procesům při chybě.
    """
    progress_bar.progress(0.1, f"{label}…")
    cmd = [
        FFMPEG, "-y",
        "-i", input_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        "-ar", "44100",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,   # 5 minut – zabrání visícím procesům
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Konverze trvala příliš dlouho (> 5 min). Zkus kratší záznam.")

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg chyba:\n{result.stderr[-600:]}")

    progress_bar.progress(1.0, f"{label} dokončena.")


def check_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Session state – monetizace
# ---------------------------------------------------------------------------

def _init_session() -> None:
    """Inicializuje session state pro počítání stažení."""
    if "downloads_used" not in st.session_state:
        st.session_state.downloads_used = 0


def _has_free_download() -> bool:
    return st.session_state.downloads_used < FREE_DOWNLOAD_LIMIT


def _increment_downloads() -> None:
    st.session_state.downloads_used += 1


def _render_paywall() -> None:
    """Zobrazí platební bránu místo download tlačítka."""
    st.warning("Vyčerpal/a jsi bezplatné stažení. Pro další stažení je potřeba předplatné.")
    st.markdown("---")
    col1, col2 = st.columns([2, 1])
    with col1:
        st.markdown("### ⚡ CapMP3 Pro")
        st.markdown(
            "- ✅ Neomezené stahování\n"
            "- ✅ Prioritní zpracování\n"
            "- ✅ Podpora vývoje"
        )
    with col2:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.link_button(
            "💳 Koupit přístup – 2 €/měs",
            url=STRIPE_PAYMENT_URL,
            type="primary",
        )
    st.caption("Platba přes Stripe · Zabezpečeno SSL · Zrušení kdykoliv")


# ---------------------------------------------------------------------------
# Sidebar – sponzor
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## 🎵 CapMP3")
        st.caption("Extraktor zvuku z cap.so záznamů")
        st.divider()

        # Sponzorský banner – nahraď obsahem partnera
        st.markdown("### 🤝 Partner projektu")
        st.markdown(
            """
            <a href="https://example.com" target="_blank">
                <div style="
                    background: linear-gradient(135deg, #1a1a2e, #16213e);
                    border: 1px solid #0f3460;
                    border-radius: 8px;
                    padding: 16px;
                    text-align: center;
                    color: #e94560;
                    font-weight: bold;
                    font-size: 14px;
                ">
                    📢 Tvoje reklama zde<br>
                    <span style="color:#aaa; font-size:11px; font-weight:normal;">
                        info@example.com
                    </span>
                </div>
            </a>
            """,
            unsafe_allow_html=True,
        )

        st.divider()
        st.caption(f"Bezplatná stažení: {st.session_state.get('downloads_used', 0)} / {FREE_DOWNLOAD_LIMIT}")


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="CapMP3 – Extraktor zvuku",
    page_icon="🎵",
    layout="centered",
)

_init_session()
_render_sidebar()

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

if not _has_free_download():
    # Uživatel nemá volné stažení – zobraz paywall místo tlačítka
    _render_paywall()
elif st.button("⬇️ Stáhnout MP3", type="primary"):
    url = url_input.strip()

    if not url:
        st.warning("Zadej URL záznamu.")
        st.stop()
    if not url.startswith(("http://", "https://")):
        st.error("Zadej platnou URL adresu začínající http:// nebo https://")
        st.stop()

    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "source")       # raw stažený soubor
        audio_path = os.path.join(tmpdir, "audio.mp3")
        audio_bytes = None

        try:
            # Krok 1: Najdi URL videa
            with st.status("Hledám URL videa…", expanded=True) as status:
                video_url, method, audio_only = find_video_url(url)
                st.caption(f"✓ Nalezeno přes: **{method}**")
                status.update(label="URL videa nalezena ✓", state="complete")

            # Krok 2: Stahování (s kontrolou velikosti)
            dl_bar = st.progress(0, "Připravuji stahování…")
            if audio_only:
                download_to_file(video_url, src_path, dl_bar, "⬇️ Stahování audio stopy z CDN")
            else:
                download_to_file(video_url, src_path, dl_bar, "⬇️ Stahování videa z CDN")

            # Krok 3: Konverze do MP3
            conv_bar = st.progress(0, "Připravuji konverzi…")
            convert_to_mp3(src_path, audio_path, conv_bar, "🔄 Konverze na MP3")

            # Krok 4: Načti MP3 do paměti, okamžitě uvolni
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

        # tmpdir blok končí zde – src soubory smazány z disku

    # Nabídka ke stažení + počítání + uvolnění RAM
    if audio_bytes:
        _increment_downloads()
        st.download_button(
            label="💾 Stáhnout MP3",
            data=audio_bytes,
            file_name="cap_audio.mp3",
            mime="audio/mpeg",
        )
        # Explicitní uvolnění velkého bytearray z RAM
        del audio_bytes
        gc.collect()

st.divider()
st.caption("Podporované platformy: cap.so, cap.link · Dočasné soubory jsou automaticky mazány.")
