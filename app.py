"""
CapMP3 - Audio extractor for cap.so / cap.link
Run: streamlit run app.py
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
# Config
# ---------------------------------------------------------------------------

MAX_VIDEO_MB = 400
FREE_CREDITS = 1

STRIPE_BASIC_URL = "https://buy.stripe.com/REPLACE_BASIC"
STRIPE_PRO_URL   = "https://buy.stripe.com/REPLACE_PRO"
CONTACT_EMAIL    = "info@capmp3.com"

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
# cap.so API
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
                f"File is too large ({size // 1024 // 1024} MB). Maximum allowed: {MAX_VIDEO_MB} MB."
            )
    except requests.RequestException:
        pass


def get_cap_video_url(page_url: str) -> tuple[str, bool]:
    resp = requests.get(page_url, headers=HEADERS, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    final_url = resp.url

    video_id = _extract_video_id(final_url)
    if not video_id:
        raise ValueError(f"Could not extract a video ID from the URL: {final_url}")

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

    raise ValueError("cap.so API did not return a valid URL. The video may be private or unavailable.")


def get_video_url_generic(page_url: str) -> str:
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "format": "bestaudio/best"}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            if info and info.get("url"):
                return info["url"]
    except Exception as e:
        raise ValueError(f"yt-dlp does not support this platform: {e}") from e
    raise ValueError("yt-dlp could not find a video URL.")


def find_video_url(url: str) -> tuple[str, str, bool]:
    parsed = urlparse(url)
    is_cap = any(h in parsed.netloc for h in ["cap.so", "cap.link"])
    if is_cap:
        video_url, audio_only = get_cap_video_url(url)
        return video_url, "cap.so API (audio track)" if audio_only else "cap.so API (video)", audio_only
    video_url = get_video_url_generic(url)
    return video_url, "yt-dlp", False


# ---------------------------------------------------------------------------
# Download & conversion
# ---------------------------------------------------------------------------

def download_to_file(source_url: str, dest_path: str, bar, label: str) -> None:
    _check_video_size(source_url)
    resp  = requests.get(source_url, headers=HEADERS, stream=True, timeout=(15, None))
    resp.raise_for_status()
    total = int(resp.headers.get("Content-Length", 0))
    if total > MAX_VIDEO_MB * 1024 * 1024:
        resp.close()
        raise ValueError(f"File is too large ({total // 1024 // 1024} MB). Maximum allowed: {MAX_VIDEO_MB} MB.")
    downloaded = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=512 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    bar.progress(min(downloaded / total, 1.0),
                                 f"{label} — {downloaded/1024/1024:.1f} / {total/1024/1024:.0f} MB")
                else:
                    bar.progress(0.5, f"{label} — {downloaded/1024/1024:.1f} MB…")
    bar.progress(1.0, f"{label} — done.")


def convert_to_mp3(input_path: str, output_path: str, bar, label: str) -> None:
    bar.progress(0.1, f"{label}…")
    cmd = [FFMPEG, "-y", "-i", input_path, "-vn",
           "-acodec", "libmp3lame", "-q:a", "2", "-ar", "44100", output_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Conversion took too long (> 5 min). Please try again.")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error:\n{result.stderr[-600:]}")
    bar.progress(1.0, f"{label} — complete.")


def check_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _fetch_cap_metadata(url: str) -> dict:
    """Fetch og:title and og:image from a cap.so/cap.link recording page."""
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")

        title     = None
        thumbnail = None

        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = og_title.get("content", "").strip()

        og_image = soup.find("meta", property="og:image")
        if og_image:
            thumbnail = og_image.get("content", "").strip()

        if not title and soup.title and soup.title.string:
            title = soup.title.string.strip()

        # Remove cap.so branding suffixes
        if title:
            for suffix in [" | Cap", " - Cap", " | cap.so", " — Cap", " · Cap"]:
                title = title.replace(suffix, "")
            title = title.strip()

        return {
            "title":     title or "cap.so Recording",
            "thumbnail": thumbnail or "",
            "success":   bool(title or thumbnail),
        }
    except Exception:
        return {"title": "cap.so Recording", "thumbnail": "", "success": False}


def _init_session() -> None:
    defaults = {
        "registered":  False,
        "email":       "",
        "credits":     0,
        "show_gate":   False,
        "pending_url": "",
        "do_convert":  False,
        "video_meta":  None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def _credits() -> int:
    return st.session_state.credits


def _deduct_credit() -> None:
    st.session_state.credits = max(0, st.session_state.credits - 1)


def _add_credits(amount: int) -> None:
    # TODO: Validate Stripe Webhook before calling —
    # use stripe.Webhook.construct_event(payload, sig, secret)
    st.session_state.credits += amount


# ---------------------------------------------------------------------------
# Supabase stubs
# ---------------------------------------------------------------------------

def _save_email_to_supabase(email: str) -> None:
    """
    TODO: After Supabase MCP auth, persist email to `subscribers` table:
        CREATE TABLE subscribers (
            id         uuid DEFAULT gen_random_uuid() PRIMARY KEY,
            email      text UNIQUE NOT NULL,
            created_at timestamptz DEFAULT now()
        );
    """
    pass


def _load_credits_from_supabase(email: str) -> int | None:
    """TODO: Load credit balance for email. Returns None if not found."""
    return None


# ---------------------------------------------------------------------------
# CSS — clean, minimal, trustworthy
# ---------------------------------------------------------------------------

def _inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        /* ── Reset & base ─────────────────────────────────── */
        html, body, [class*="css"] {
            font-family: 'Inter', sans-serif !important;
        }
        .stApp { background: #F8FAFC !important; }

        /* ── Hide sidebar, toolbar, header ───────────────── */
        [data-testid="stSidebar"],
        [data-testid="collapsedControl"],
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        #MainMenu,
        .stAppDeployButton { display: none !important; }

        /* ── Page wrapper: center & constrain width ──────── */
        .block-container {
            max-width: 720px !important;
            padding: 48px 40px 64px !important;
            margin: 0 auto !important;
        }
        @media (max-width: 768px) {
            .block-container { padding: 32px 20px 48px !important; }
            .steps-grid { grid-template-columns: 1fr !important; }
            .use-cases  { grid-template-columns: 1fr !important; }
            .feature-list { grid-template-columns: 1fr !important; }
        }

        /* ── Logo ─────────────────────────────────────────── */
        .logo {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 48px;
        }
        .logo-mark {
            width: 32px; height: 32px;
            background: linear-gradient(135deg, #2563EB, #7C3AED);
            border-radius: 8px;
            display: flex; align-items: center; justify-content: center;
            font-size: 16px;
        }
        .logo-text {
            font-size: 17px;
            font-weight: 700;
            color: #0F172A;
            letter-spacing: -.02em;
        }

        /* ── Hero ─────────────────────────────────────────── */
        .hero-title {
            font-size: clamp(28px, 5vw, 40px) !important;
            font-weight: 800 !important;
            line-height: 1.15 !important;
            color: #0F172A !important;
            margin: 0 0 12px !important;
            letter-spacing: -.02em !important;
        }
        .grad {
            background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .hero-sub {
            font-size: 16px;
            color: #64748B;
            margin: 0 0 32px;
            line-height: 1.6;
        }

        /* ── Converter card ───────────────────────────────── */
        .converter-card {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 16px;
            box-shadow: 0 1px 4px rgba(15,23,42,.06);
        }

        /* ── Input ────────────────────────────────────────── */
        .stTextInput > div > div > input {
            background: #F8FAFC !important;
            border: 1.5px solid #CBD5E1 !important;
            border-radius: 10px !important;
            color: #0F172A !important;
            font-size: 15px !important;
            padding: 13px 16px !important;
            transition: border-color .15s, box-shadow .15s;
            font-family: 'Inter', sans-serif !important;
        }
        .stTextInput > div > div > input:focus {
            border-color: #2563EB !important;
            box-shadow: 0 0 0 3px rgba(37,99,235,.1) !important;
            outline: none !important;
        }
        .stTextInput > div > div > input::placeholder {
            color: #94A3B8 !important;
        }
        /* Remove input label space */
        .stTextInput label { display: none !important; }

        /* ── Primary button (all variants) ───────────────── */
        .stButton > button[kind="primary"],
        .stFormSubmitButton > button[kind="primary"],
        [data-testid="stFormSubmitButton"] > button,
        [data-testid="stBaseButton-primary"] {
            background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%) !important;
            border: none !important;
            border-radius: 10px !important;
            color: #fff !important;
            font-weight: 600 !important;
            font-size: 15px !important;
            padding: 13px 24px !important;
            letter-spacing: -.01em !important;
            transition: opacity .15s, transform .1s, box-shadow .15s !important;
            box-shadow: 0 2px 12px rgba(37,99,235,.25) !important;
        }
        .stButton > button[kind="primary"]:hover,
        .stFormSubmitButton > button[kind="primary"]:hover,
        [data-testid="stFormSubmitButton"] > button:hover,
        [data-testid="stBaseButton-primary"]:hover {
            opacity: .92 !important;
            transform: translateY(-1px) !important;
            box-shadow: 0 4px 20px rgba(37,99,235,.35) !important;
        }

        /* ── Secondary / link buttons ─────────────────────── */
        .stButton > button[kind="secondary"],
        .stFormSubmitButton > button[kind="secondary"],
        .stLinkButton > a {
            background: #FFFFFF !important;
            border: 1.5px solid #CBD5E1 !important;
            border-radius: 10px !important;
            color: #475569 !important;
            font-weight: 500 !important;
            transition: border-color .15s, color .15s !important;
        }
        .stButton > button[kind="secondary"]:hover,
        .stLinkButton > a:hover {
            border-color: #2563EB !important;
            color: #2563EB !important;
        }

        /* ── Trust bar ────────────────────────────────────── */
        .trust-bar {
            display: flex;
            justify-content: center;
            gap: 24px;
            flex-wrap: wrap;
            margin: 20px 0 0;
        }
        .trust-item {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            color: #94A3B8;
            font-weight: 500;
        }
        .trust-dot {
            width: 4px; height: 4px;
            border-radius: 50%;
            background: #CBD5E1;
        }

        /* ── Email gate ───────────────────────────────────── */
        .gate-card {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 16px;
            padding: 28px 24px 8px;
            margin-bottom: 4px;
            box-shadow: 0 1px 4px rgba(15,23,42,.06);
        }
        .gate-title {
            font-size: 18px;
            font-weight: 700;
            color: #0F172A;
            margin: 0 0 6px;
            letter-spacing: -.02em;
        }
        .gate-sub {
            font-size: 14px;
            color: #64748B;
            margin: 0 0 20px;
            line-height: 1.5;
        }
        .privacy-note {
            text-align: center;
            font-size: 12px;
            color: #94A3B8;
            margin: 8px 0 0;
        }

        /* ── Credit pill ──────────────────────────────────── */
        .credit-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #EFF6FF;
            border: 1px solid #BFDBFE;
            border-radius: 999px;
            padding: 5px 12px;
            font-size: 12px;
            color: #2563EB;
            font-weight: 500;
            margin-bottom: 16px;
        }
        .credit-pill.empty {
            background: #FEF2F2;
            border-color: #FECACA;
            color: #DC2626;
        }

        /* ── Pricing ──────────────────────────────────────── */
        .pricing-header {
            margin: 40px 0 24px;
            text-align: center;
        }
        .pricing-label {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: .1em;
            color: #2563EB;
            margin: 0 0 6px;
        }
        .pricing-title {
            font-size: 24px;
            font-weight: 700;
            color: #0F172A;
            margin: 0 0 6px;
            letter-spacing: -.02em;
        }
        .pricing-sub {
            font-size: 13px;
            color: #64748B;
            margin: 0;
        }
        .pricing-card {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 14px;
            padding: 24px 20px 20px;
            position: relative;
            box-shadow: 0 1px 4px rgba(15,23,42,.06);
        }
        .pricing-card--featured {
            border-color: #2563EB;
            background: #FFFFFF;
            box-shadow: 0 0 0 3px rgba(37,99,235,.08), 0 4px 16px rgba(37,99,235,.1);
        }
        .plan-badge {
            position: absolute;
            top: -11px; left: 50%;
            transform: translateX(-50%);
            background: linear-gradient(135deg, #2563EB, #7C3AED);
            color: #fff;
            font-size: 9px;
            font-weight: 700;
            letter-spacing: .1em;
            padding: 3px 10px;
            border-radius: 999px;
            white-space: nowrap;
        }
        .plan-name  { font-size: 13px; font-weight: 600; color: #64748B; margin: 0 0 8px; }
        .plan-price { font-size: 32px; font-weight: 800; color: #0F172A; margin: 0; line-height: 1; letter-spacing: -.03em; }
        .plan-credits { font-size: 13px; color: #2563EB; font-weight: 600; margin: 5px 0 2px; }
        .plan-unit  { font-size: 11px; color: #94A3B8; margin: 0 0 16px; }
        .plan-features { list-style: none; padding: 0; margin: 0 0 20px; }
        .plan-features li { font-size: 12px; color: #64748B; padding: 3px 0; }

        /* ── Hide Streamlit heading anchor icons ─────────── */
        [data-testid="stMarkdownContainer"] h1 a,
        [data-testid="stMarkdownContainer"] h2 a,
        [data-testid="stMarkdownContainer"] h3 a,
        .stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a {
            display: none !important;
        }

        /* ── Converter form card ──────────────────────────── */
        .url-label {
            font-size: 13px;
            font-weight: 600;
            color: #374151;
            margin: 0 0 8px;
            letter-spacing: -.01em;
        }
        [data-testid="stForm"] {
            background: #FFFFFF;
            border: 1.5px solid #E2E8F0;
            border-radius: 16px;
            padding: 20px !important;
            box-shadow: 0 1px 6px rgba(15,23,42,.07);
        }
        [data-testid="stForm"] [data-testid="stTextInput"] {
            background: transparent !important;
            border: none !important;
            padding: 0 !important;
            box-shadow: none !important;
            margin-bottom: 0 !important;
        }
        [data-testid="stForm"] [data-testid="stTextInput"] > div > div > input {
            background: #F8FAFC !important;
            border: 1.5px solid #E2E8F0 !important;
        }
        [data-testid="stForm"] [data-testid="stTextInput"] > div > div > input:focus {
            border-color: #2563EB !important;
            box-shadow: 0 0 0 3px rgba(37,99,235,.1) !important;
        }

        /* ── Progress bar ─────────────────────────────────── */
        .stProgress > div > div > div {
            background: linear-gradient(90deg, #2563EB, #7C3AED) !important;
        }

        /* ── Referral banner ─────────────────────────────── */
        .referral-banner {
            background: linear-gradient(135deg, #EFF6FF 0%, #EDE9FE 100%);
            border: 1.5px solid #BFDBFE;
            border-radius: 16px;
            padding: 28px 32px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 24px;
            margin: 56px 0 0;
            flex-wrap: wrap;
        }
        .referral-left { flex: 1; min-width: 200px; }
        .referral-badge {
            display: inline-block;
            background: linear-gradient(135deg, #2563EB, #7C3AED);
            color: #fff;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: .1em;
            padding: 3px 10px;
            border-radius: 999px;
            margin-bottom: 10px;
            text-transform: uppercase;
        }
        .referral-title {
            font-size: 20px;
            font-weight: 800;
            color: #0F172A;
            margin: 0 0 6px;
            letter-spacing: -.02em;
        }
        .referral-sub {
            font-size: 14px;
            color: #475569;
            margin: 0;
            line-height: 1.5;
        }
        .referral-cta {
            display: inline-block;
            background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%);
            color: #fff !important;
            font-size: 14px;
            font-weight: 600;
            padding: 12px 24px;
            border-radius: 10px;
            text-decoration: none !important;
            white-space: nowrap;
            box-shadow: 0 2px 12px rgba(37,99,235,.25);
            transition: opacity .15s, transform .1s;
        }
        .referral-cta:hover {
            opacity: .9;
            transform: translateY(-1px);
        }

        /* ── Alerts ───────────────────────────────────────── */
        .stAlert { border-radius: 10px !important; font-size: 14px !important; }

        /* ── Status box ───────────────────────────────────── */
        [data-testid="stStatusWidget"] { border-radius: 10px !important; }

        /* ── Download button ──────────────────────────────── */
        [data-testid="stDownloadButton"] > button {
            background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%) !important;
            border: none !important;
            border-radius: 10px !important;
            color: #fff !important;
            font-weight: 600 !important;
            font-size: 15px !important;
            padding: 13px 24px !important;
            box-shadow: 0 2px 12px rgba(37,99,235,.25) !important;
            width: 100% !important;
        }

        /* ── Content sections ────────────────────────────── */
        .content-section { margin: 56px 0 0; }
        .content-label {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: .1em;
            color: #2563EB;
            margin: 0 0 6px;
            text-transform: uppercase;
        }
        .content-h2 {
            font-size: 22px;
            font-weight: 700;
            color: #0F172A;
            margin: 0 0 12px;
            letter-spacing: -.02em;
        }
        .content-p {
            font-size: 14px;
            color: #475569;
            line-height: 1.7;
            margin: 0 0 12px;
        }
        .feature-list {
            list-style: none;
            padding: 0;
            margin: 0 0 0;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }
        .feature-list li {
            font-size: 13px;
            color: #374151;
            display: flex;
            align-items: flex-start;
            gap: 8px;
            line-height: 1.5;
        }
        .feature-list li::before {
            content: "✓";
            color: #2563EB;
            font-weight: 700;
            flex-shrink: 0;
            margin-top: 1px;
        }
        .steps-grid {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 16px;
            margin: 20px 0 0;
        }
        .step-card {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 12px;
            padding: 20px 16px;
            box-shadow: 0 1px 4px rgba(15,23,42,.05);
        }
        .step-num {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 28px; height: 28px;
            background: linear-gradient(135deg, #2563EB, #7C3AED);
            border-radius: 50%;
            color: #fff;
            font-size: 12px;
            font-weight: 700;
            margin-bottom: 10px;
        }
        .step-title {
            font-size: 14px;
            font-weight: 700;
            color: #0F172A;
            margin: 0 0 4px;
        }
        .step-desc {
            font-size: 12px;
            color: #64748B;
            line-height: 1.5;
            margin: 0;
        }
        .use-cases {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin: 16px 0 0;
        }
        .use-case {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 1px 4px rgba(15,23,42,.05);
        }
        .use-case-icon { font-size: 20px; margin-bottom: 6px; }
        .use-case-title {
            font-size: 13px;
            font-weight: 700;
            color: #0F172A;
            margin: 0 0 4px;
        }
        .use-case-desc {
            font-size: 12px;
            color: #64748B;
            line-height: 1.5;
            margin: 0;
        }
        .divider-line {
            border: none;
            border-top: 1px solid #F1F5F9;
            margin: 48px 0;
        }

        /* ── Video preview card ──────────────────────────── */
        .preview-card {
            background: #FFFFFF;
            border: 1.5px solid #BFDBFE;
            border-radius: 14px;
            padding: 16px;
            display: flex;
            gap: 16px;
            align-items: center;
            margin: 16px 0;
            box-shadow: 0 2px 8px rgba(37,99,235,.08);
        }
        .preview-thumb {
            width: 96px;
            height: 60px;
            object-fit: cover;
            border-radius: 8px;
            flex-shrink: 0;
            background: #EFF6FF;
        }
        .preview-thumb-placeholder {
            width: 96px;
            height: 60px;
            border-radius: 8px;
            background: linear-gradient(135deg, #EFF6FF, #E0E7FF);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
            flex-shrink: 0;
        }
        .preview-status {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: .06em;
            color: #16A34A;
            margin: 0 0 4px;
            text-transform: uppercase;
        }
        .preview-title {
            font-size: 15px;
            font-weight: 600;
            color: #0F172A;
            margin: 0 0 4px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 520px;
        }
        .preview-hint {
            font-size: 12px;
            color: #64748B;
            margin: 0;
        }

        /* ── FAQ ──────────────────────────────────────────── */
        [data-testid="stExpander"] {
            background: #FFFFFF !important;
            border: 1px solid #E2E8F0 !important;
            border-radius: 10px !important;
            margin-bottom: 8px !important;
            box-shadow: none !important;
        }
        [data-testid="stExpander"] summary {
            font-size: 14px !important;
            font-weight: 600 !important;
            color: #0F172A !important;
        }
        [data-testid="stExpanderDetails"] p {
            font-size: 13px !important;
            color: #475569 !important;
            line-height: 1.65 !important;
        }

        /* ── Footer ───────────────────────────────────────── */
        .footer {
            text-align: center;
            padding: 48px 0 0;
            font-size: 12px;
            color: #CBD5E1;
        }
        .footer a { color: #CBD5E1; text-decoration: none; }
        .footer a:hover { color: #2563EB; }

        /* ── Divider ──────────────────────────────────────── */
        hr { border-color: #E2E8F0 !important; margin: 32px 0 !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Pricing section
# ---------------------------------------------------------------------------

def _render_pricing() -> None:
    st.markdown(
        """
        <div class="pricing-header">
            <p class="pricing-label">PRICING</p>
            <h2 class="pricing-title">Top up your credits</h2>
            <p class="pricing-sub">One-time · Credits never expire · Stripe checkout</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns(3, gap="small")

    with col1:
        st.markdown(
            """
            <div class="pricing-card">
                <p class="plan-name">Starter</p>
                <p class="plan-price">$4.99</p>
                <p class="plan-credits">10 credits</p>
                <p class="plan-unit">$0.50 / download</p>
                <ul class="plan-features">
                    <li>✓ 10 MP3 downloads</li>
                    <li>✓ 190 kbps quality</li>
                    <li>✓ cap.so & cap.link</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Get Starter", url=STRIPE_BASIC_URL, use_container_width=True)

    with col2:
        st.markdown(
            """
            <div class="pricing-card pricing-card--featured">
                <span class="plan-badge">BEST VALUE</span>
                <p class="plan-name">Pro</p>
                <p class="plan-price">$9.99</p>
                <p class="plan-credits">30 credits</p>
                <p class="plan-unit">$0.33 / download</p>
                <ul class="plan-features">
                    <li>✓ 30 MP3 downloads</li>
                    <li>✓ 190 kbps quality</li>
                    <li>✓ cap.so & cap.link</li>
                    <li>✓ Priority processing</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Get Pro", url=STRIPE_PRO_URL, use_container_width=True, type="primary")

    with col3:
        st.markdown(
            """
            <div class="pricing-card">
                <p class="plan-name">Teams</p>
                <p class="plan-price" style="font-size:20px;padding-top:6px;">Custom</p>
                <p class="plan-credits">&nbsp;</p>
                <p class="plan-unit">For teams & power users</p>
                <ul class="plan-features">
                    <li>✓ Unlimited downloads</li>
                    <li>✓ API access</li>
                    <li>✓ Dedicated support</li>
                    <li>✓ SLA</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button(
            "Contact us",
            url=f"mailto:{CONTACT_EMAIL}?subject=CapMP3%20Teams",
            use_container_width=True,
        )


# ---------------------------------------------------------------------------
# Content / SEO section
# ---------------------------------------------------------------------------

def _render_content() -> None:
    st.markdown('<hr class="divider-line">', unsafe_allow_html=True)

    # ── What is CapMP3 ────────────────────────────────────────────────────────
    st.markdown(
        """
        <div class="content-section">
            <p class="content-label">About</p>
            <h2 class="content-h2">What is CapMP3?</h2>
            <p class="content-p">
                CapMP3 is a free online tool that converts <strong>cap.so</strong> and
                <strong>cap.link</strong> screen recordings to downloadable MP3 audio files.
                Paste any cap.so or cap.link URL and CapMP3 extracts the audio track,
                converts it to high-quality MP3, and delivers it straight to your browser —
                no software installation required.
            </p>
            <p class="content-p">
                cap.so is a screen recording and video-sharing platform used by product teams,
                engineers, and creators for async demos, meeting recaps, onboarding guides, and
                product walkthroughs. CapMP3 gives you the audio layer of those recordings as a
                standalone MP3 — ready for offline playback, transcription, or archiving.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Features ──────────────────────────────────────────────────────────────
    st.markdown(
        """
        <div class="content-section">
            <p class="content-label">Features</p>
            <h2 class="content-h2">Everything you need, nothing you don't</h2>
            <ul class="feature-list">
                <li>Converts cap.so and cap.link URLs to MP3</li>
                <li>190 kbps audio quality — clean and clear</li>
                <li>Runs entirely in your browser</li>
                <li>No software or extension to install</li>
                <li>Files deleted immediately after download</li>
                <li>1 free download — no credit card required</li>
                <li>Conversion completes in under 30 seconds</li>
                <li>Works on desktop and mobile browsers</li>
            </ul>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── How to use ────────────────────────────────────────────────────────────
    st.markdown(
        """
        <div class="content-section">
            <p class="content-label">How it works</p>
            <h2 class="content-h2">Convert a cap.so recording to MP3 in 3 steps</h2>
            <div class="steps-grid">
                <div class="step-card">
                    <div class="step-num">1</div>
                    <p class="step-title">Copy the URL</p>
                    <p class="step-desc">Open your cap.so or cap.link recording and copy the URL from the browser address bar.</p>
                </div>
                <div class="step-card">
                    <div class="step-num">2</div>
                    <p class="step-title">Paste &amp; convert</p>
                    <p class="step-desc">Paste the URL into CapMP3 above and click "Convert to MP3". Processing takes under 30 seconds.</p>
                </div>
                <div class="step-card">
                    <div class="step-num">3</div>
                    <p class="step-title">Download your MP3</p>
                    <p class="step-desc">Click "Save MP3" to download the file. It's saved directly to your device — no cloud storage involved.</p>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Use cases ─────────────────────────────────────────────────────────────
    st.markdown(
        """
        <div class="content-section">
            <p class="content-label">Use cases</p>
            <h2 class="content-h2">Why extract audio from cap.so recordings?</h2>
            <div class="use-cases">
                <div class="use-case">
                    <div class="use-case-icon">🎙️</div>
                    <p class="use-case-title">Transcription</p>
                    <p class="use-case-desc">Feed the MP3 into Whisper, Otter.ai, Descript, or Notion AI for automatic transcription of meetings, demos, and walkthroughs.</p>
                </div>
                <div class="use-case">
                    <div class="use-case-icon">🎧</div>
                    <p class="use-case-title">Offline listening</p>
                    <p class="use-case-desc">Play back meeting recaps and briefings without an internet connection or the cap.so app — on any device that supports MP3.</p>
                </div>
                <div class="use-case">
                    <div class="use-case-icon">🎚️</div>
                    <p class="use-case-title">Podcast &amp; video production</p>
                    <p class="use-case-desc">Use interview or presentation audio as raw material in your podcast editor, Premiere Pro, or DaVinci Resolve.</p>
                </div>
                <div class="use-case">
                    <div class="use-case-icon">🗄️</div>
                    <p class="use-case-title">Archiving</p>
                    <p class="use-case-desc">Create lightweight audio backups of important cap.so sessions — much smaller than the original video file.</p>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Referral banner ───────────────────────────────────────────────────────
    st.markdown(
        """
        <div class="referral-banner">
            <div class="referral-left">
                <span class="referral-badge">Exclusive offer</span>
                <p class="referral-title">Get cap.so with 20% off — for 12 months</p>
                <p class="referral-sub">
                    cap.so is the screen recording tool behind every recording on this page.
                    Use our link to get <strong>20% off your subscription for a full year</strong>.
                    No coupon code needed — discount is applied automatically.
                </p>
            </div>
            <a href="https://go.cap.so/tomas-zahradnik" target="_blank" rel="noopener" class="referral-cta">
                Claim 20% off cap.so →
            </a>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── FAQ ───────────────────────────────────────────────────────────────────
    st.markdown(
        """
        <div class="content-section">
            <p class="content-label">FAQ</p>
            <h2 class="content-h2">Frequently asked questions</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )

    faqs = [
        (
            "What is cap.so?",
            "cap.so is a screen recording and async video platform used by product and engineering teams. "
            "It lets users record their screen, camera, or both and share the recording via a short URL "
            "(cap.so/s/... or cap.link/...). It's widely used for product demos, bug reports, onboarding "
            "videos, and meeting summaries.",
        ),
        (
            "Can I convert any cap.so or cap.link recording to MP3?",
            "CapMP3 works with any publicly accessible cap.so or cap.link recording. Private recordings "
            "that require a password or login to view are not supported, as CapMP3 cannot authenticate "
            "on your behalf.",
        ),
        (
            "What audio quality does CapMP3 produce?",
            "CapMP3 encodes audio at 190 kbps using the MP3 format (LAME encoder, variable bitrate). "
            "This quality level is well-suited for voice recordings, meetings, presentations, and "
            "screen recording audio. The output is clean and clear for all typical cap.so use cases.",
        ),
        (
            "Are my recordings stored on CapMP3 servers?",
            "No. CapMP3 processes everything in a temporary directory that is deleted immediately after "
            "your download completes. We never store your recordings, audio files, or any personal data "
            "beyond your email address (used only to manage your free credit).",
        ),
        (
            "How long does conversion take?",
            "Most cap.so recordings convert in under 30 seconds. The exact time depends on the length "
            "of the recording and current server load. Recordings longer than 60 minutes may take up "
            "to 2 minutes.",
        ),
        (
            "Does CapMP3 work on iPhone, iPad, and Android?",
            "Yes. CapMP3 runs entirely in your browser and works on iOS Safari, Android Chrome, and all "
            "modern mobile browsers. After downloading, the MP3 is saved to your device's default "
            "Downloads folder or Files app.",
        ),
        (
            "What's the difference between the free credit and paid credits?",
            "Every new user gets 1 free download after entering their email — no credit card required. "
            "Additional downloads are available as one-time credit packs: Starter ($4.99 / 10 downloads) "
            "and Pro ($9.99 / 30 downloads). Credits never expire.",
        ),
    ]

    for question, answer in faqs:
        with st.expander(question):
            st.write(answer)


# ---------------------------------------------------------------------------
# Conversion logic
# ---------------------------------------------------------------------------

def _run_conversion(url: str) -> None:
    """Execute the full download + convert pipeline and render result."""

    if _is_rate_limited():
        st.error(
            f"Too many requests — limit is {RATE_LIMIT_REQUESTS} per {RATE_LIMIT_WINDOW}s. "
            "Please wait a moment and try again."
        )
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        src_path   = os.path.join(tmpdir, "source")
        audio_path = os.path.join(tmpdir, "audio.mp3")
        audio_bytes = None

        try:
            with st.status("Fetching audio…", expanded=True) as status:
                video_url, method, audio_only = find_video_url(url)
                st.caption(f"✓ Source located via {method}")
                status.update(label="Source located ✓", state="complete")

            dl_bar = st.progress(0, "Downloading…")
            download_to_file(
                video_url, src_path, dl_bar,
                "Downloading audio" if audio_only else "Downloading video",
            )

            conv_bar = st.progress(0, "Converting…")
            convert_to_mp3(src_path, audio_path, conv_bar, "Converting to MP3")

            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

        except requests.HTTPError as e:
            st.error(f"HTTP error: {e}")
            return
        except ValueError as e:
            st.error(str(e))
            return
        except RuntimeError as e:
            st.error(str(e))
            return
        except Exception as e:
            st.error(f"Unexpected error: {e}")
            return

    if audio_bytes:
        _deduct_credit()
        st.success("Your MP3 is ready!")
        st.download_button(
            label="⬇ Save MP3",
            data=audio_bytes,
            file_name="cap_audio.mp3",
            mime="audio/mpeg",
            use_container_width=True,
        )
        del audio_bytes
        gc.collect()


# ---------------------------------------------------------------------------
# Terms of Service page
# ---------------------------------------------------------------------------

def _render_terms() -> None:
    st.markdown(
        """
        <style>
        .tos-h1  { font-size:28px; font-weight:800; color:#0F172A; margin:0 0 4px; letter-spacing:-.02em; }
        .tos-updated { font-size:13px; color:#94A3B8; margin:0 0 40px; }
        .tos-disclaimer {
            background:#FFF7ED; border:1.5px solid #FED7AA; border-radius:12px;
            padding:20px 24px; margin-bottom:32px;
        }
        .tos-disclaimer strong { color:#C2410C; }
        .tos-disclaimer p { font-size:13px; color:#78350F; margin:4px 0 0; line-height:1.6; }
        .tos-h2  { font-size:18px; font-weight:700; color:#0F172A; margin:32px 0 8px; letter-spacing:-.01em; }
        .tos-p   { font-size:14px; color:#374151; line-height:1.75; margin:0 0 10px; }
        .tos-ul  { padding-left:20px; margin:0 0 12px; }
        .tos-ul li { font-size:14px; color:#374151; line-height:1.75; }
        .tos-risk { background:#FEF2F2; border:1px solid #FECACA; border-radius:8px; padding:12px 16px; margin:8px 0; font-size:13px; color:#991B1B; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="logo" style="margin-bottom:32px;">
            <div class="logo-mark">🎵</div>
            <span class="logo-text">CapMP3</span>
        </div>
        <h1 class="tos-h1">Terms of Service</h1>
        <p class="tos-updated">Last updated: April 19, 2026</p>

        <div class="tos-disclaimer">
            <strong>⚠ Legal Disclaimer</strong>
            <p>CapMP3 is an independent platform and is not affiliated with, endorsed by, or in any way
            officially connected to Cap Software, Inc. or the cap.so platform. The names "cap.so" and
            "cap.link" are used solely for descriptive purposes.</p>
            <p><strong>How it works:</strong> CapMP3 receives a publicly accessible URL provided by the user,
            fetches the audio stream from that URL, and converts it to MP3 format. We do not host,
            store, or cache any recording content on our servers. All files are deleted immediately
            after the download completes.</p>
            <p>The Service is provided "as is" and "as available", without warranties of uninterrupted,
            error-free, or 100% available operation. cap.so may change its platform or API at any time,
            which may affect or disable CapMP3 without notice.</p>
        </div>

        <h2 class="tos-h2">1. General Provisions</h2>
        <p class="tos-p">Welcome to CapMP3 (capmp3.com) — a browser-based tool that converts cap.so and cap.link
        screen recordings to MP3 audio files. By accessing or using the Service, you agree to be bound
        by these Terms of Service ("Terms"). If you do not agree to these Terms, do not use the Service.</p>

        <h2 class="tos-h2">2. Service Description</h2>
        <p class="tos-p">CapMP3 provides a technical conversion utility that:</p>
        <ul class="tos-ul">
            <li>Accepts a publicly accessible cap.so or cap.link URL submitted by the user</li>
            <li>Retrieves the audio stream from that URL</li>
            <li>Converts the stream to MP3 format using server-side processing</li>
            <li>Delivers the resulting MP3 file to the user for immediate download</li>
        </ul>
        <p class="tos-p">CapMP3 does not index, archive, cache, or redistribute any recording content.
        All temporary files are permanently deleted within seconds of the user's download completing.</p>

        <h2 class="tos-h2">3. User Eligibility</h2>
        <p class="tos-p">You must be at least 13 years of age (or the applicable age of digital consent in your
        jurisdiction, which may be higher) to use the Service. By using the Service, you represent and
        warrant that you meet this requirement.</p>

        <h2 class="tos-h2">4. Acceptable Use & User Responsibilities</h2>
        <p class="tos-p">By using the Service, you represent, warrant, and agree that:</p>
        <ul class="tos-ul">
            <li>You are the owner of the cap.so or cap.link recording you are converting,
            or you have obtained explicit permission from the recording's owner to download and use
            it in audio format.</li>
            <li>You will not use the Service to infringe upon the copyright, trademark, or other
            intellectual property rights of any person or entity.</li>
            <li>You will use downloaded audio files solely for personal, non-commercial purposes,
            unless you hold the rights to use the content commercially.</li>
            <li>You will comply with cap.so's own Terms of Service when accessing their platform.
            CapMP3 has no control over cap.so's terms and cannot guarantee compatibility.</li>
            <li>You will not use automated tools, bots, or scripts to make bulk requests to the Service.</li>
            <li>You will not attempt to circumvent rate limits, abuse the credit system, or otherwise
            interfere with the Service's operation.</li>
            <li>You are solely responsible for ensuring that your use of downloaded audio complies
            with all applicable laws in your jurisdiction.</li>
        </ul>

        <h2 class="tos-h2">5. Intellectual Property</h2>
        <p class="tos-p">CapMP3 does not claim ownership of any recordings processed through the Service.
        All cap.so recordings remain the property of their respective creators and rights holders.</p>
        <p class="tos-p">CapMP3 operates solely as a technical conversion utility. The responsibility
        for ensuring that the conversion and use of any audio file complies with applicable copyright
        law rests entirely with the user.</p>

        <h2 class="tos-h2">6. Credits, Payments & Refunds</h2>
        <ul class="tos-ul">
            <li><strong>Free credit:</strong> One (1) free conversion credit is provided upon email
            registration. This credit carries no monetary value and cannot be transferred or
            exchanged for cash.</li>
            <li><strong>Paid credits:</strong> Additional credits are available for purchase via
            Stripe. All prices are listed in USD. By purchasing credits, you also agree to
            <a href="https://stripe.com/legal/ssa" target="_blank">Stripe's Terms of Service</a>.</li>
            <li><strong>No refunds:</strong> Credits are non-refundable once purchased, as they
            represent access to a digital service consumed on demand. If a conversion fails due
            to a verified error on our part, the credit will be restored to your account.</li>
            <li><strong>Credit expiry:</strong> Purchased credits do not expire.</li>
        </ul>

        <h2 class="tos-h2">7. Privacy & Data Processing</h2>
        <p class="tos-p">We collect and process the following personal data:</p>
        <ul class="tos-ul">
            <li><strong>Email address</strong> — collected voluntarily at registration, used solely
            to manage your credit balance and send occasional product updates. You may opt out at
            any time by contacting us.</li>
            <li><strong>IP address</strong> — processed ephemerally for rate limiting and abuse
            prevention. Not stored beyond the current session window.</li>
        </ul>
        <p class="tos-p">We do not collect, store, or process the content of any recording submitted
        to the Service. We do not sell, rent, or share personal data with third parties.</p>
        <p class="tos-p"><strong>EU/EEA users (GDPR):</strong> You have the right to access, rectify,
        erase, and port your personal data, and to withdraw consent at any time. To exercise these
        rights, contact us at info@capmp3.com. You also have the right to lodge a complaint
        with your local supervisory authority.</p>

        <h2 class="tos-h2">8. Disclaimer of Warranties</h2>
        <p class="tos-p">TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, THE SERVICE IS PROVIDED
        "AS IS" AND "AS AVAILABLE" WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
        BUT NOT LIMITED TO WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE,
        OR NON-INFRINGEMENT. WE DO NOT WARRANT THAT THE SERVICE WILL BE UNINTERRUPTED, ERROR-FREE,
        OR THAT ANY SPECIFIC RECORDING WILL BE PROCESSABLE.</p>

        <h2 class="tos-h2">9. Limitation of Liability</h2>
        <p class="tos-p">TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, CAPMP3 AND ITS OPERATORS
        SHALL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES,
        INCLUDING BUT NOT LIMITED TO LOSS OF DATA, REVENUE, OR PROFITS, ARISING OUT OF OR IN CONNECTION
        WITH THE USE OF THE SERVICE.</p>
        <p class="tos-p">In no event shall CapMP3's total cumulative liability to any user exceed the
        total amount paid by that user for credits in the twelve (12) months immediately preceding
        the claim. Users in jurisdictions that do not allow limitation of liability for personal
        injury or consequential damages may not be subject to this limitation.</p>

        <h2 class="tos-h2">10. DMCA & Copyright Complaints</h2>
        <p class="tos-p">If you believe that a recording processed through CapMP3 infringes your
        copyright, please send a written notice to:</p>
        <p class="tos-p">
            <strong>Email:</strong> info@capmp3.com<br>
            <strong>Subject line:</strong> DMCA Takedown Request
        </p>
        <p class="tos-p">Your notice must include: (i) identification of the copyrighted work claimed
        to be infringed; (ii) the specific URL submitted to CapMP3; (iii) your full name and contact
        information; (iv) a statement that you have a good-faith belief that the use is unauthorised;
        (v) a statement, under penalty of perjury, that the information in the notice is accurate and
        that you are the rights holder or authorised to act on their behalf.</p>
        <p class="tos-p">Note: CapMP3 does not store or host any recording content. DMCA notices may
        be forwarded to cap.so or the relevant CDN provider as appropriate.</p>

        <h2 class="tos-h2">11. Third-Party Services</h2>
        <p class="tos-p">The Service integrates with:</p>
        <ul class="tos-ul">
            <li><strong>cap.so / Cap Software, Inc.</strong> — for accessing publicly shared
            recordings. CapMP3 is not affiliated with or endorsed by Cap Software, Inc.</li>
            <li><strong>Stripe</strong> — for payment processing. Credit purchases are subject to
            Stripe's Terms of Service and Privacy Policy.</li>
        </ul>

        <h2 class="tos-h2">12. Changes to Terms</h2>
        <p class="tos-p">We reserve the right to modify these Terms at any time. Material changes
        will be notified via the Service. Continued use of the Service after changes are posted
        constitutes acceptance of the revised Terms.</p>

        <h2 class="tos-h2">13. Anti-Spam Policy</h2>
        <p class="tos-p">We commit to the following:</p>
        <ul class="tos-ul">
            <li>CapMP3 will never be promoted through unsolicited messages of any kind</li>
            <li>We will not send bulk emails without prior, explicit user consent</li>
            <li>All user email communications are strictly opt-in and permission-based</li>
            <li>We will not share or sell email addresses for any marketing purpose</li>
        </ul>
        <p class="tos-p">If you receive an unsolicited message appearing to promote CapMP3, please
        contact info@capmp3.com immediately.</p>

        <h2 class="tos-h2">14. Governing Law & Dispute Resolution</h2>
        <p class="tos-p">These Terms shall be governed by and construed in accordance with the laws
        of the Czech Republic, without regard to its conflict of law provisions. Any disputes arising
        under these Terms shall be subject to the exclusive jurisdiction of the competent courts of
        the Czech Republic.</p>
        <p class="tos-p">For users in the European Union, mandatory consumer protection provisions
        of your country of residence apply in addition to the above, and nothing in these Terms
        limits rights you have under EU consumer law.</p>

        <h2 class="tos-h2">15. Contact Information</h2>
        <p class="tos-p">For questions, complaints, or requests relating to these Terms:</p>
        <p class="tos-p">
            <strong>Email:</strong> info@capmp3.com<br>
            <strong>Website:</strong> capmp3.com
        </p>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div style="margin-top:48px; padding-top:24px; border-top:1px solid #F1F5F9;
                    text-align:center; font-size:12px; color:#CBD5E1;">
            capmp3.com · <a href="/" style="color:#CBD5E1; text-decoration:none;">← Back to converter</a>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="CapMP3 — cap.so to MP3",
    page_icon="🎵",
    layout="centered",
)

_init_session()
_inject_css()

# ── Page routing ──────────────────────────────────────────────────────────────
if st.query_params.get("page") == "terms":
    _render_terms()
    st.stop()

# ── ffmpeg guard ─────────────────────────────────────────────────────────────
if not check_ffmpeg():
    st.error(
        "**ffmpeg not found.**\n\n"
        "- macOS: `brew install ffmpeg`\n"
        "- Ubuntu: `sudo apt install ffmpeg`\n"
        "- Windows: [ffmpeg.org](https://ffmpeg.org/download.html)"
    )
    st.stop()

# ── Logo ──────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <div class="logo">
        <div class="logo-mark">🎵</div>
        <span class="logo-text">CapMP3</span>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Hero ──────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <h1 class="hero-title">
        cap.so recordings<br>to <span class="grad">MP3</span>, instantly.
    </h1>
    <p class="hero-sub">
        Paste any cap.so or cap.link URL and download<br>a clean MP3 in under 30 seconds.
    </p>
    """,
    unsafe_allow_html=True,
)

# ── Converter ─────────────────────────────────────────────────────────────────
st.markdown('<p class="url-label">Paste your cap.so or cap.link recording URL</p>', unsafe_allow_html=True)

with st.form("converter_form", clear_on_submit=False):
    url_input = st.text_input(
        "url",
        placeholder="https://cap.so/s/xxxxxxxx  ·  https://cap.link/xxxxxxxx",
        label_visibility="collapsed",
    )
    clicked = st.form_submit_button("Convert to MP3 →", type="primary", use_container_width=True)

# Credit pill — only shown when registered
if st.session_state.registered:
    credits = _credits()
    if credits > 0:
        label = "credit" if credits == 1 else "credits"
        st.markdown(
            f'<div class="credit-pill">✦ {credits} {label} remaining</div>',
            unsafe_allow_html=True,
        )

# ── Trust bar ─────────────────────────────────────────────────────────────────
st.markdown(
    """
    <div class="trust-bar">
        <span class="trust-item">🔒 No account needed</span>
        <span class="trust-dot"></span>
        <span class="trust-item">⚡ ~30 second conversion</span>
        <span class="trust-dot"></span>
        <span class="trust-item">🗑 Files auto-deleted</span>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Handle Convert click ──────────────────────────────────────────────────────
if clicked:
    url = url_input.strip()
    if not url:
        st.warning("Paste a cap.so or cap.link URL above.")
    elif not url.startswith(("http://", "https://")):
        st.error("Please enter a URL that starts with https://")
    else:
        # Fetch metadata with animated spinner
        with st.spinner("Loading recording info…"):
            meta = _fetch_cap_metadata(url)
        st.session_state.video_meta  = meta
        st.session_state.pending_url = url

        if not st.session_state.registered:
            st.session_state.show_gate = True
            st.rerun()
        elif _credits() <= 0:
            st.session_state.show_gate = False
        else:
            _run_conversion(url)

# ── Video preview card ────────────────────────────────────────────────────────
if st.session_state.video_meta and (st.session_state.show_gate or st.session_state.registered):
    meta  = st.session_state.video_meta
    title = meta.get("title", "cap.so Recording")
    thumb = meta.get("thumbnail", "")

    if thumb:
        thumb_html = f'<img src="{thumb}" class="preview-thumb" onerror="this.style.display=\'none\'">'
    else:
        thumb_html = '<div class="preview-thumb-placeholder">🎵</div>'

    gate_hint = "" if st.session_state.registered else \
        '<p class="preview-hint">Enter your email below to start the free download ↓</p>'

    st.markdown(
        f"""
        <div class="preview-card">
            {thumb_html}
            <div style="min-width:0; flex:1;">
                <div class="preview-status">✓ Recording found</div>
                <p class="preview-title">{title}</p>
                {gate_hint}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ── Inline email gate ─────────────────────────────────────────────────────────
if st.session_state.show_gate and not st.session_state.registered:
    st.markdown("""
        <div class="gate-card">
            <p class="gate-title">Get your first MP3 free</p>
            <p class="gate-sub">
                Enter your email to unlock <strong style="color:#0F172A;">1 free download</strong>.
                No password, no subscription.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("email_gate_form", clear_on_submit=True):
        email_input = st.text_input(
            "email",
            placeholder="you@example.com",
            label_visibility="collapsed",
        )
        go = st.form_submit_button(
            "Claim free download →",
            type="primary",
            use_container_width=True,
        )

    st.markdown(
        '<p class="privacy-note">🔒 No spam, ever. Unsubscribe anytime.</p>',
        unsafe_allow_html=True,
    )

    if go:
        clean_email = email_input.strip().lower()
        if not clean_email or "@" not in clean_email or "." not in clean_email.split("@")[-1]:
            st.error("Please enter a valid email address.")
            st.stop()

        db_credits = _load_credits_from_supabase(clean_email)

        st.session_state.email      = clean_email
        st.session_state.registered = True
        st.session_state.credits    = db_credits if db_credits is not None else FREE_CREDITS
        st.session_state.show_gate  = False
        st.session_state.do_convert = True

        _save_email_to_supabase(clean_email)
        st.rerun()

# ── Post-registration: auto-trigger conversion ────────────────────────────────
if st.session_state.do_convert and st.session_state.registered and st.session_state.pending_url:
    st.session_state.do_convert = False
    _run_conversion(st.session_state.pending_url)

# ── Out of credits → show pricing ─────────────────────────────────────────────
if st.session_state.registered and _credits() <= 0 and not st.session_state.do_convert:
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(
        '<div class="credit-pill empty">✦ No credits remaining</div>',
        unsafe_allow_html=True,
    )
    _render_pricing()

# ── Content & FAQ ─────────────────────────────────────────────────────────────
_render_content()

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(
    f"""
    <div class="footer">
        cap.so · cap.link · Files deleted immediately after download
        · <a href="?page=terms">Terms of Service</a>
        · <a href="mailto:{CONTACT_EMAIL}">Contact</a>
    </div>
    """,
    unsafe_allow_html=True,
)
