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
    st.markdown(
        """
        <div class="reg-card">
            <div class="reg-icon">🎁</div>
            <h3 class="reg-title">Začni zdarma</h3>
            <p class="reg-sub">Zadej e-mail a získej <strong>1 kredit zdarma</strong>.<br>
            Použijeme ho pouze pro novinky o CapMP3.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("registration_form", clear_on_submit=True):
        email = st.text_input("", placeholder="jmeno@example.com", label_visibility="collapsed")
        submitted = st.form_submit_button("Získat kredit zdarma →", type="primary", use_container_width=True)

    if submitted:
        email = email.strip().lower()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            st.error("Zadej platnou e-mailovou adresu.")
            return

        st.session_state.email      = email
        st.session_state.registered = True

        db_credits = _load_credits_from_supabase(email)
        if db_credits is not None:
            st.session_state.credits = db_credits
        else:
            st.session_state.credits = FREE_CREDITS

        _save_email_to_supabase(email)
        st.rerun()


# ---------------------------------------------------------------------------
# Pricing sekce
# ---------------------------------------------------------------------------

def _render_pricing() -> None:
    st.markdown(
        """
        <div style="margin: 48px 0 24px;">
            <p class="section-label">CENÍK</p>
            <h2 class="section-title">Doplnit kredity</h2>
            <p class="section-sub">Jednorázový nákup · Kredity nevyprší · Platba přes Stripe</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns(3, gap="medium")

    with col1:
        st.markdown(
            """
            <div class="pricing-card">
                <p class="plan-name">Basic</p>
                <p class="plan-price">$4.99</p>
                <p class="plan-credits">10 kreditů</p>
                <p class="plan-unit">= $0.50 / stažení</p>
                <ul class="plan-features">
                    <li>✓ 10 MP3 stažení</li>
                    <li>✓ Plná kvalita 190 kbps</li>
                    <li>✓ cap.so & cap.link</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Koupit Basic", url=STRIPE_BASIC_URL, use_container_width=True)

    with col2:
        st.markdown(
            """
            <div class="pricing-card pricing-card--featured">
                <span class="plan-badge">NEJOBLÍBENĚJŠÍ</span>
                <p class="plan-name">Pro</p>
                <p class="plan-price">$9.99</p>
                <p class="plan-credits">30 kreditů</p>
                <p class="plan-unit">= $0.33 / stažení</p>
                <ul class="plan-features">
                    <li>✓ 30 MP3 stažení</li>
                    <li>✓ Plná kvalita 190 kbps</li>
                    <li>✓ cap.so & cap.link</li>
                    <li>✓ Prioritní zpracování</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Koupit Pro", url=STRIPE_PRO_URL, use_container_width=True, type="primary")

    with col3:
        st.markdown(
            """
            <div class="pricing-card">
                <p class="plan-name">Enterprise</p>
                <p class="plan-price" style="font-size:22px; padding-top:8px;">Na míru</p>
                <p class="plan-credits"> </p>
                <p class="plan-unit">Individuální řešení pro týmy</p>
                <ul class="plan-features">
                    <li>✓ Neomezené stahování</li>
                    <li>✓ API přístup</li>
                    <li>✓ Dedikovaná podpora</li>
                    <li>✓ SLA garance</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button(
            "Kontaktovat nás",
            url=f"mailto:{CONTACT_EMAIL}?subject=CapMP3%20Enterprise",
            use_container_width=True,
        )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

        /* ── Base ─────────────────────────────────────────── */
        html, body, [class*="css"] { font-family: 'Inter', sans-serif !important; }
        .stApp { background: #030712; }

        /* ── Hero ─────────────────────────────────────────── */
        .hero-badge {
            display: inline-block;
            background: rgba(59,130,246,0.12);
            border: 1px solid rgba(59,130,246,0.3);
            color: #93C5FD;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: .08em;
            padding: 4px 14px;
            border-radius: 999px;
            margin-bottom: 20px;
        }
        .hero-title {
            font-size: clamp(32px, 5vw, 52px);
            font-weight: 800;
            line-height: 1.15;
            color: #F1F5F9;
            margin: 0 0 16px;
        }
        .hero-gradient {
            background: linear-gradient(135deg, #3B82F6 0%, #8B5CF6 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .hero-sub {
            font-size: 16px;
            color: #64748B;
            margin: 0 0 40px;
            line-height: 1.6;
        }

        /* ── Input ────────────────────────────────────────── */
        .stTextInput > div > div > input {
            background: #0F172A !important;
            border: 1px solid #1E3A5F !important;
            border-radius: 12px !important;
            color: #F1F5F9 !important;
            font-size: 15px !important;
            padding: 14px 18px !important;
            transition: border-color .2s;
        }
        .stTextInput > div > div > input:focus {
            border-color: #3B82F6 !important;
            box-shadow: 0 0 0 3px rgba(59,130,246,.15) !important;
        }
        .stTextInput > div > div > input::placeholder { color: #334155 !important; }

        /* ── Primary button ───────────────────────────────── */
        .stButton > button[kind="primary"],
        .stFormSubmitButton > button[kind="primary"] {
            background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%) !important;
            border: none !important;
            border-radius: 12px !important;
            color: #fff !important;
            font-weight: 600 !important;
            font-size: 15px !important;
            padding: 14px 28px !important;
            transition: opacity .2s, transform .1s !important;
            box-shadow: 0 4px 24px rgba(59,130,246,.25) !important;
        }
        .stButton > button[kind="primary"]:hover,
        .stFormSubmitButton > button[kind="primary"]:hover {
            opacity: .9 !important;
            transform: translateY(-1px) !important;
        }

        /* ── Secondary button ─────────────────────────────── */
        .stButton > button[kind="secondary"],
        .stFormSubmitButton > button[kind="secondary"],
        .stLinkButton > a {
            background: transparent !important;
            border: 1px solid #1E3A5F !important;
            border-radius: 12px !important;
            color: #94A3B8 !important;
            font-weight: 500 !important;
            transition: border-color .2s, color .2s !important;
        }
        .stButton > button[kind="secondary"]:hover,
        .stLinkButton > a:hover {
            border-color: #3B82F6 !important;
            color: #3B82F6 !important;
        }

        /* ── Credit badge ─────────────────────────────────── */
        .credit-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: rgba(59,130,246,.1);
            border: 1px solid rgba(59,130,246,.25);
            border-radius: 10px;
            padding: 10px 16px;
            margin-bottom: 20px;
            font-size: 14px;
            color: #93C5FD;
            font-weight: 500;
        }
        .credit-badge .credit-num {
            font-size: 20px;
            font-weight: 700;
            color: #3B82F6;
        }
        .credit-badge.empty {
            background: rgba(239,68,68,.1);
            border-color: rgba(239,68,68,.25);
            color: #FCA5A5;
        }
        .credit-badge.empty .credit-num { color: #EF4444; }

        /* ── Registration card ────────────────────────────── */
        .reg-card {
            background: #0F172A;
            border: 1px solid #1E3A5F;
            border-radius: 20px;
            padding: 40px;
            text-align: center;
            margin: 32px 0 24px;
        }
        .reg-icon { font-size: 40px; margin-bottom: 12px; }
        .reg-title { font-size: 24px; font-weight: 700; color: #F1F5F9; margin: 0 0 8px; }
        .reg-sub { color: #64748B; font-size: 15px; line-height: 1.6; margin: 0 0 24px; }

        /* ── Section labels ───────────────────────────────── */
        .section-label {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: .12em;
            color: #3B82F6;
            margin: 0 0 8px;
        }
        .section-title {
            font-size: 28px;
            font-weight: 700;
            color: #F1F5F9;
            margin: 0 0 8px;
        }
        .section-sub {
            color: #64748B;
            font-size: 14px;
            margin: 0 0 32px;
        }

        /* ── Pricing cards ────────────────────────────────── */
        .pricing-card {
            background: #0F172A;
            border: 1px solid #1E3A5F;
            border-radius: 16px;
            padding: 28px 24px 24px;
            height: 100%;
            position: relative;
        }
        .pricing-card--featured {
            border-color: #3B82F6;
            background: linear-gradient(160deg, #0F172A 0%, #0D1F3C 100%);
            box-shadow: 0 0 40px rgba(59,130,246,.12);
        }
        .plan-badge {
            position: absolute;
            top: -12px;
            left: 50%;
            transform: translateX(-50%);
            background: linear-gradient(135deg, #2563EB, #7C3AED);
            color: #fff;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: .1em;
            padding: 3px 12px;
            border-radius: 999px;
            white-space: nowrap;
        }
        .plan-name {
            font-size: 16px;
            font-weight: 600;
            color: #94A3B8;
            margin: 0 0 8px;
        }
        .plan-price {
            font-size: 36px;
            font-weight: 800;
            color: #F1F5F9;
            margin: 0;
            line-height: 1;
        }
        .plan-credits {
            font-size: 14px;
            color: #3B82F6;
            font-weight: 600;
            margin: 6px 0 2px;
        }
        .plan-unit {
            font-size: 12px;
            color: #334155;
            margin: 0 0 20px;
        }
        .plan-features {
            list-style: none;
            padding: 0;
            margin: 0 0 24px;
        }
        .plan-features li {
            font-size: 13px;
            color: #64748B;
            padding: 4px 0;
        }

        /* ── Status / progress ────────────────────────────── */
        .stProgress > div > div > div { background: linear-gradient(90deg, #2563EB, #7C3AED) !important; }

        /* ── Alerts ───────────────────────────────────────── */
        .stAlert { border-radius: 12px !important; }

        /* ── Divider ──────────────────────────────────────── */
        hr { border-color: #1E293B !important; margin: 40px 0 !important; }

        /* ── Footer caption ───────────────────────────────── */
        .stCaption { color: #334155 !important; }

        /* ── Sidebar ──────────────────────────────────────── */
        [data-testid="stSidebar"] {
            background: #0A0F1E !important;
            border-right: 1px solid #1E293B !important;
        }
        [data-testid="stSidebar"] * { color: #94A3B8; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown(
            """
            <div style="padding: 8px 0 16px;">
                <span style="font-size:22px; font-weight:800; color:#F1F5F9;">🎵 CapMP3</span><br>
                <span style="font-size:12px; color:#334155;">Extraktor zvuku z cap.so</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.divider()

        # Zůstatek kreditů
        if st.session_state.registered:
            credits = _credits()
            cls = "" if credits > 0 else "empty"
            label = "kredit" if credits == 1 else "kredity" if credits < 5 else "kreditů"
            st.markdown(
                f"""
                <div class="credit-badge {cls}">
                    <span>💎</span>
                    <span><span class="credit-num">{credits}</span> {label}</span>
                </div>
                <p style="font-size:12px; color:#334155; margin:4px 0 0;">{st.session_state.email}</p>
                """,
                unsafe_allow_html=True,
            )
            st.divider()

        # Sponzorský banner
        st.markdown(
            "<p style='font-size:11px; font-weight:700; letter-spacing:.1em; color:#1E3A5F; margin:0 0 10px;'>PARTNER PROJEKTU</p>",
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <a href="https://example.com" target="_blank" style="text-decoration:none;">
                <div style="
                    background: linear-gradient(135deg,#0F172A,#0D1F3C);
                    border: 1px solid #1E3A5F;
                    border-radius: 12px;
                    padding: 18px;
                    text-align: center;
                ">
                    <p style="color:#3B82F6; font-weight:700; font-size:13px; margin:0 0 4px;">📢 Tvoje reklama zde</p>
                    <p style="color:#334155; font-size:11px; margin:0;">info@example.com</p>
                </div>
            </a>
            """,
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="CapMP3 – Audio Extractor",
    page_icon="🎵",
    layout="centered",
)

_init_session()
_inject_css()
_render_sidebar()

st.markdown(
    """
    <div style="text-align:center; padding: 48px 0 32px;">
        <div class="hero-badge">🎵 cap.so · cap.link Audio Extractor</div>
        <h1 class="hero-title">
            Extrahuj zvuk jako<br>
            <span class="hero-gradient">MP3 za vteřiny</span>
        </h1>
        <p class="hero-sub">
            Vlož odkaz na záznam z cap.so nebo cap.link.<br>
            Stáhneme audio, převedeme do MP3 a podáme ti ke stažení.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

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

if credits > 0:
    label = "kredit" if credits == 1 else "kredity" if credits < 5 else "kreditů"
    st.markdown(
        f'<div class="credit-badge">💎 &nbsp;Váš zůstatek: <span class="credit-num">&nbsp;{credits}</span>&nbsp;{label}</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div class="credit-badge empty">⚠️ &nbsp;Váš volný limit byl vyčerpán. Doplňte kredity níže.</div>',
        unsafe_allow_html=True,
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

st.markdown(
    """
    <div style="text-align:center; padding: 40px 0 16px;">
        <hr style="border-color:#1E293B; margin-bottom:24px;">
        <p style="font-size:13px; color:#1E3A5F; margin:0;">
            cap.so &nbsp;·&nbsp; cap.link &nbsp;·&nbsp; Dočasné soubory jsou automaticky mazány
            &nbsp;·&nbsp; <a href="mailto:info@tomaszahradnik.com" style="color:#1E3A5F; text-decoration:none;">Kontakt</a>
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)
