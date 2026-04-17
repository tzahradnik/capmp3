"""
CapMP3 - Extraktor zvuku z cap.so / cap.link
Spuštění: streamlit run app.py
"""

import gc
import os
import re
import time
import tempfile
import subprocess
from collections import defaultdict
from urllib.parse import urlparse

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------

MAX_VIDEO_MB = 400
FREE_CREDITS = 1          # Kreditů zdarma při registraci

# Stripe Payment Links – TODO: nahradit skutečnými URL po vytvoření produktů
STRIPE_BASIC_URL  = "https://buy.stripe.com/REPLACE_BASIC"   # Basic Pack  $4.99 / 10 kreditů
STRIPE_PRO_URL    = "https://buy.stripe.com/REPLACE_PRO"     # Pro Pack    $9.99 / 30 kreditů
CONTACT_EMAIL     = "info@tomaszahradnik.com"

# TODO: Supabase – po autentikaci přes `claude /mcp` doplnit URL + anon key
# SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
# SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_WINDOW   = 60

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

_request_log: dict = defaultdict(list)


def _get_client_ip() -> str:
    try:
        forwarded = st.context.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    except Exception:
        pass
    return "unknown"


def _is_rate_limited() -> bool:
    ip  = _get_client_ip()
    now = time.time()
    _request_log[ip] = [t for t in _request_log[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(_request_log[ip]) >= RATE_LIMIT_REQUESTS:
        return True
    _request_log[ip].append(now)
    return False


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def _get_ffmpeg() -> str:
    import shutil
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
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
# cap.so API – extrakce URL videa
# ---------------------------------------------------------------------------

def _extract_video_id(url: str) -> str | None:
    path    = urlparse(url).path.rstrip("/")
    segment = path.split("/")[-1]
    if re.match(r"^[a-z0-9]{8,}$", segment, re.IGNORECASE):
        return segment
    return None


def _cap_api_redirect(video_id: str, final_url: str, video_type: str) -> str | None:
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


def _check_video_size(url: str) -> None:
    try:
        head = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
        size = int(head.headers.get("content-length", 0))
        if size > MAX_VIDEO_MB * 1024 * 1024:
            raise ValueError(
                f"Video je příliš velké ({size // 1024 // 1024} MB). Max {MAX_VIDEO_MB} MB."
            )
    except requests.RequestException:
        pass


def get_cap_video_url(page_url: str) -> tuple[str, bool]:
    resp = requests.get(page_url, headers=HEADERS, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    final_url = resp.url

    video_id = _extract_video_id(final_url)
    if not video_id:
        raise ValueError(f"Nepodařilo se extrahovat videoId z URL: {final_url}")

    for vtype in ("audio", "segments-audio"):
        url = _cap_api_redirect(video_id, final_url, vtype)
        if url:
            try:
                head = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
                ct   = head.headers.get("content-type", "")
                size = int(head.headers.get("content-length", 0))
                if "audio" in ct or (size > 0 and size < 50 * 1024 * 1024):
                    return url, True
            except Exception:
                pass

    url = _cap_api_redirect(video_id, final_url, "master")
    if url:
        return url, False

    raise ValueError("cap.so API nevrátilo platnou URL. Video může být soukromé.")


def get_video_url_generic(page_url: str) -> str:
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "format": "bestaudio/best"}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            if info and info.get("url"):
                return info["url"]
    except Exception as e:
        raise ValueError(f"yt-dlp nepodporuje tuto platformu: {e}") from e
    raise ValueError("yt-dlp nenašel video URL.")


def find_video_url(url: str) -> tuple[str, str, bool]:
    parsed = urlparse(url)
    is_cap = any(h in parsed.netloc for h in ["cap.so", "cap.link"])
    if is_cap:
        video_url, audio_only = get_cap_video_url(url)
        return video_url, "cap.so API (audio)" if audio_only else "cap.so API (video)", audio_only
    video_url = get_video_url_generic(url)
    return video_url, "yt-dlp", False


# ---------------------------------------------------------------------------
# Stahování a konverze
# ---------------------------------------------------------------------------

def download_to_file(source_url: str, dest_path: str, bar, label: str) -> None:
    _check_video_size(source_url)
    resp  = requests.get(source_url, headers=HEADERS, stream=True, timeout=(15, None))
    resp.raise_for_status()
    total = int(resp.headers.get("Content-Length", 0))
    if total > MAX_VIDEO_MB * 1024 * 1024:
        resp.close()
        raise ValueError(f"Soubor je příliš velký ({total // 1024 // 1024} MB). Max {MAX_VIDEO_MB} MB.")
    downloaded = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=512 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    bar.progress(min(downloaded / total, 1.0),
                                 f"{label} {downloaded/1024/1024:.1f} / {total/1024/1024:.0f} MB")
                else:
                    bar.progress(0.5, f"{label} {downloaded/1024/1024:.1f} MB…")
    bar.progress(1.0, f"{label} dokončeno.")


def convert_to_mp3(input_path: str, output_path: str, bar, label: str) -> None:
    bar.progress(0.1, f"{label}…")
    cmd = [FFMPEG, "-y", "-i", input_path, "-vn",
           "-acodec", "libmp3lame", "-q:a", "2", "-ar", "44100", output_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Konverze trvala příliš dlouho (> 5 min).")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg chyba:\n{result.stderr[-600:]}")
    bar.progress(1.0, f"{label} dokončena.")


def check_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session state – kredity
# ---------------------------------------------------------------------------

def _init_session() -> None:
    defaults = {
        "registered": False,
        "email":      "",
        "credits":    0,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def _credits() -> int:
    return st.session_state.credits


def _deduct_credit() -> None:
    st.session_state.credits = max(0, st.session_state.credits - 1)


def _add_credits(amount: int) -> None:
    # TODO: Stripe Webhook validace – před voláním této funkce ověřit
    # platbu přes `stripe.Webhook.construct_event(payload, sig, secret)`
    # nebo unikátní kód z Stripe redirect URL (?session_id=...)
    st.session_state.credits += amount


# ---------------------------------------------------------------------------
# Supabase hooks (připraveno pro budoucí implementaci)
# ---------------------------------------------------------------------------

def _save_email_to_supabase(email: str) -> None:
    """
    TODO: Po nastavení Supabase MCP (`claude /mcp` → supabase → Authenticate)
    doplnit uložení emailu do tabulky `subscribers`:
        CREATE TABLE subscribers (
            id         uuid DEFAULT gen_random_uuid() PRIMARY KEY,
            email      text UNIQUE NOT NULL,
            created_at timestamptz DEFAULT now()
        );
    Pak sem přidat:
        supabase.table("subscribers").upsert({"email": email}).execute()
    """
    pass  # placeholder


def _load_credits_from_supabase(email: str) -> int | None:
    """
    TODO: Po nastavení Supabase načíst zůstatek kreditů pro daný email.
    Vrátí None pokud uživatel v DB neexistuje.
    """
    return None  # placeholder


# ---------------------------------------------------------------------------
# Registrační formulář
# ---------------------------------------------------------------------------

def _render_registration() -> None:
    st.markdown("### 👋 Začni zdarma")
    st.markdown(
        "Zadej e-mail a získej **1 kredit zdarma**. "
        "E-mail použijeme pouze pro zasílání novinek o CapMP3."
    )
    with st.form("registration_form", clear_on_submit=True):
        email = st.text_input("Tvůj e-mail", placeholder="jmeno@example.com")
        submitted = st.form_submit_button("🎁 Získat kredit zdarma", type="primary")

    if submitted:
        email = email.strip().lower()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            st.error("Zadej platnou e-mailovou adresu.")
            return

        # Ulož email (session + Supabase hook)
        st.session_state.email      = email
        st.session_state.registered = True

        # Zkus načíst kredity z DB (budoucí implementace)
        db_credits = _load_credits_from_supabase(email)
        if db_credits is not None:
            st.session_state.credits = db_credits
        else:
            # Nový uživatel → FREE_CREDITS zdarma
            st.session_state.credits = FREE_CREDITS

        _save_email_to_supabase(email)
        st.rerun()


# ---------------------------------------------------------------------------
# Pricing sekce
# ---------------------------------------------------------------------------

def _render_pricing() -> None:
    st.markdown("---")
    st.markdown("## 💳 Doplnit kredity")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown(
            """
            <div style="border:1px solid #333; border-radius:10px; padding:20px; text-align:center; height:200px;">
                <h3 style="margin:0">Basic</h3>
                <p style="font-size:28px; font-weight:bold; margin:8px 0">$4.99</p>
                <p style="color:#aaa; margin:0">10 kreditů</p>
                <p style="color:#aaa; font-size:12px">= $0.50 / stažení</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Koupit Basic", url=STRIPE_BASIC_URL, use_container_width=True)

    with col2:
        st.markdown(
            """
            <div style="border:2px solid #7c3aed; border-radius:10px; padding:20px; text-align:center; height:200px; background: #1a0a2e;">
                <p style="color:#a78bfa; font-size:11px; margin:0">NEJOBLÍBENĚJŠÍ</p>
                <h3 style="margin:4px 0">Pro</h3>
                <p style="font-size:28px; font-weight:bold; margin:8px 0">$9.99</p>
                <p style="color:#aaa; margin:0">30 kreditů</p>
                <p style="color:#aaa; font-size:12px">= $0.33 / stažení</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Koupit Pro", url=STRIPE_PRO_URL, use_container_width=True, type="primary")

    with col3:
        st.markdown(
            """
            <div style="border:1px solid #333; border-radius:10px; padding:20px; text-align:center; height:200px;">
                <h3 style="margin:0">Enterprise</h3>
                <p style="font-size:18px; font-weight:bold; margin:8px 0">Na míru</p>
                <p style="color:#aaa; margin:0">Neomezené stahování</p>
                <p style="color:#aaa; font-size:12px">Individuální řešení pro týmy</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button(
            "Kontaktovat nás",
            url=f"mailto:{CONTACT_EMAIL}?subject=CapMP3 Enterprise",
            use_container_width=True,
        )

    st.caption("Platba přes Stripe · SSL · Kredity nevyprší")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## 🎵 CapMP3")
        st.caption("Extraktor zvuku z cap.so záznamů")
        st.divider()

        # Zůstatek kreditů
        if st.session_state.registered:
            credits = _credits()
            color   = "#22c55e" if credits > 0 else "#ef4444"
            st.markdown(
                f"**👤 {st.session_state.email}**  \n"
                f"<span style='color:{color}; font-size:18px; font-weight:bold'>"
                f"💎 {credits} kreditů</span>",
                unsafe_allow_html=True,
            )
            st.divider()

        # Sponzorský banner
        st.markdown("### 🤝 Partner projektu")
        st.markdown(
            """
            <a href="https://example.com" target="_blank">
                <div style="
                    background: linear-gradient(135deg,#1a1a2e,#16213e);
                    border: 1px solid #0f3460;
                    border-radius: 8px;
                    padding: 16px;
                    text-align: center;
                    color: #e94560;
                    font-weight: bold;
                    font-size: 14px;
                ">
                    📢 Tvoje reklama zde<br>
                    <span style="color:#aaa;font-size:11px;font-weight:normal;">
                        info@example.com
                    </span>
                </div>
            </a>
            """,
            unsafe_allow_html=True,
        )


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
st.markdown("Extrahuj zvuk z libovolného záznamu na **cap.so** nebo **cap.link** jako MP3.")

if not check_ffmpeg():
    st.error(
        "⚠️ **ffmpeg nenalezen.**\n\n"
        "- **macOS:** `brew install ffmpeg`\n"
        "- **Ubuntu/Debian:** `sudo apt install ffmpeg`\n"
        "- **Windows:** stáhnout z [ffmpeg.org](https://ffmpeg.org/download.html)"
    )
    st.stop()

# ── Registrace ───────────────────────────────────────────────────────────────
if not st.session_state.registered:
    _render_registration()
    _render_pricing()
    st.stop()

# ── Hlavní formulář (jen pro registrované uživatele) ─────────────────────────
url_input = st.text_input(
    "URL záznamu",
    placeholder="https://cap.link/xxxxxxxx nebo https://cap.so/s/xxxxxxxx",
)

credits = _credits()

# Zůstatek kreditů pod inputem
if credits > 0:
    st.caption(f"💎 Váš zůstatek: **{credits} {'kredit' if credits == 1 else 'kredity' if credits < 5 else 'kreditů'}**")
else:
    st.warning(
        "Váš volný limit byl vyčerpán. "
        "Pro další stahování si prosím doplňte kredity níže."
    )
    _render_pricing()
    st.stop()

if st.button("⬇️ Stáhnout MP3", type="primary", disabled=(credits == 0)):
    url = url_input.strip()

    if not url:
        st.warning("Zadej URL záznamu.")
        st.stop()
    if not url.startswith(("http://", "https://")):
        st.error("Zadej platnou URL adresu začínající http:// nebo https://")
        st.stop()
    if _is_rate_limited():
        st.error(
            f"⛔ Příliš mnoho požadavků. "
            f"Max {RATE_LIMIT_REQUESTS} za {RATE_LIMIT_WINDOW} s. Zkus to za chvíli."
        )
        st.stop()

    with tempfile.TemporaryDirectory() as tmpdir:
        src_path   = os.path.join(tmpdir, "source")
        audio_path = os.path.join(tmpdir, "audio.mp3")
        audio_bytes = None

        try:
            with st.status("Hledám URL videa…", expanded=True) as status:
                video_url, method, audio_only = find_video_url(url)
                st.caption(f"✓ Nalezeno přes: **{method}**")
                status.update(label="URL videa nalezena ✓", state="complete")

            dl_bar = st.progress(0, "Připravuji stahování…")
            download_to_file(
                video_url, src_path, dl_bar,
                "⬇️ Stahování audio stopy z CDN" if audio_only else "⬇️ Stahování videa z CDN",
            )

            conv_bar = st.progress(0, "Připravuji konverzi…")
            convert_to_mp3(src_path, audio_path, conv_bar, "🔄 Konverze na MP3")

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

    # Kredit se odečte až po úspěšném stažení
    if audio_bytes:
        _deduct_credit()
        st.download_button(
            label="💾 Stáhnout MP3",
            data=audio_bytes,
            file_name="cap_audio.mp3",
            mime="audio/mpeg",
        )
        del audio_bytes
        gc.collect()

st.divider()
st.caption("Podporované platformy: cap.so, cap.link · Dočasné soubory jsou automaticky mazány.")
