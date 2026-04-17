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
CONTACT_EMAIL    = "info@tomaszahradnik.com"

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

def _init_session() -> None:
    defaults = {
        "registered":      False,
        "email":           "",
        "credits":         0,
        "show_gate":       False,
        "pending_url":     "",
        "do_convert":      False,
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
        .stApp { background: #0A0F1C !important; }

        /* ── Hide sidebar & header clutter ───────────────── */
        [data-testid="stSidebar"],
        [data-testid="collapsedControl"],
        header[data-testid="stHeader"] { display: none !important; }

        /* ── Page wrapper: center & constrain width ──────── */
        .block-container {
            max-width: 560px !important;
            padding: 48px 24px 64px !important;
            margin: 0 auto !important;
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
            color: #F1F5F9;
            letter-spacing: -.02em;
        }

        /* ── Hero ─────────────────────────────────────────── */
        .hero-title {
            font-size: clamp(28px, 5vw, 40px);
            font-weight: 800;
            line-height: 1.15;
            color: #F1F5F9;
            margin: 0 0 12px;
            letter-spacing: -.02em;
        }
        .grad {
            background: linear-gradient(135deg, #3B82F6 0%, #8B5CF6 100%);
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
            background: #0F172A;
            border: 1px solid #1E293B;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 16px;
        }

        /* ── Input ────────────────────────────────────────── */
        .stTextInput > div > div > input {
            background: #020917 !important;
            border: 1.5px solid #1E293B !important;
            border-radius: 10px !important;
            color: #F1F5F9 !important;
            font-size: 15px !important;
            padding: 13px 16px !important;
            transition: border-color .15s, box-shadow .15s;
            font-family: 'Inter', sans-serif !important;
        }
        .stTextInput > div > div > input:focus {
            border-color: #3B82F6 !important;
            box-shadow: 0 0 0 3px rgba(59,130,246,.12) !important;
            outline: none !important;
        }
        .stTextInput > div > div > input::placeholder {
            color: #334155 !important;
        }
        /* Remove input label space */
        .stTextInput label { display: none !important; }

        /* ── Primary button ───────────────────────────────── */
        .stButton > button[kind="primary"],
        .stFormSubmitButton > button[kind="primary"] {
            background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%) !important;
            border: none !important;
            border-radius: 10px !important;
            color: #fff !important;
            font-weight: 600 !important;
            font-size: 15px !important;
            padding: 13px 24px !important;
            letter-spacing: -.01em !important;
            transition: opacity .15s, transform .1s, box-shadow .15s !important;
            box-shadow: 0 2px 16px rgba(59,130,246,.2) !important;
        }
        .stButton > button[kind="primary"]:hover,
        .stFormSubmitButton > button[kind="primary"]:hover {
            opacity: .92 !important;
            transform: translateY(-1px) !important;
            box-shadow: 0 4px 24px rgba(59,130,246,.35) !important;
        }
        .stButton > button[kind="primary"]:active,
        .stFormSubmitButton > button[kind="primary"]:active {
            transform: translateY(0) !important;
        }

        /* ── Secondary / link buttons ─────────────────────── */
        .stButton > button[kind="secondary"],
        .stFormSubmitButton > button[kind="secondary"],
        .stLinkButton > a {
            background: transparent !important;
            border: 1.5px solid #1E293B !important;
            border-radius: 10px !important;
            color: #94A3B8 !important;
            font-weight: 500 !important;
            transition: border-color .15s, color .15s !important;
        }
        .stLinkButton > a[data-featured="true"] {
            background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%) !important;
            border: none !important;
            color: #fff !important;
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
            color: #475569;
            font-weight: 500;
        }
        .trust-dot {
            width: 5px; height: 5px;
            border-radius: 50%;
            background: #1E3A5F;
        }

        /* ── Email gate ───────────────────────────────────── */
        .gate-card {
            background: #0F172A;
            border: 1px solid #1E293B;
            border-radius: 16px;
            padding: 28px 24px 8px;
            margin-bottom: 4px;
        }
        .gate-title {
            font-size: 18px;
            font-weight: 700;
            color: #F1F5F9;
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
            color: #334155;
            margin: 8px 0 0;
        }

        /* ── Credit pill ──────────────────────────────────── */
        .credit-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: rgba(59,130,246,.08);
            border: 1px solid rgba(59,130,246,.18);
            border-radius: 999px;
            padding: 5px 12px;
            font-size: 12px;
            color: #93C5FD;
            font-weight: 500;
            margin-bottom: 16px;
        }
        .credit-pill.empty {
            background: rgba(239,68,68,.08);
            border-color: rgba(239,68,68,.2);
            color: #FCA5A5;
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
            color: #3B82F6;
            margin: 0 0 6px;
        }
        .pricing-title {
            font-size: 24px;
            font-weight: 700;
            color: #F1F5F9;
            margin: 0 0 6px;
            letter-spacing: -.02em;
        }
        .pricing-sub {
            font-size: 13px;
            color: #475569;
            margin: 0;
        }
        .pricing-card {
            background: #0F172A;
            border: 1px solid #1E293B;
            border-radius: 14px;
            padding: 24px 20px 20px;
            position: relative;
        }
        .pricing-card--featured {
            border-color: #2563EB;
            background: linear-gradient(160deg, #0F172A 0%, #0D1F3C 100%);
            box-shadow: 0 0 32px rgba(37,99,235,.1);
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
        .plan-price { font-size: 32px; font-weight: 800; color: #F1F5F9; margin: 0; line-height: 1; letter-spacing: -.03em; }
        .plan-credits { font-size: 13px; color: #3B82F6; font-weight: 600; margin: 5px 0 2px; }
        .plan-unit  { font-size: 11px; color: #334155; margin: 0 0 16px; }
        .plan-features { list-style: none; padding: 0; margin: 0 0 20px; }
        .plan-features li { font-size: 12px; color: #475569; padding: 3px 0; }

        /* ── Progress bar ─────────────────────────────────── */
        .stProgress > div > div > div {
            background: linear-gradient(90deg, #2563EB, #7C3AED) !important;
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
            box-shadow: 0 2px 16px rgba(59,130,246,.2) !important;
            width: 100% !important;
        }

        /* ── Footer ───────────────────────────────────────── */
        .footer {
            text-align: center;
            padding: 48px 0 0;
            font-size: 12px;
            color: #1E3A5F;
        }
        .footer a { color: #1E3A5F; text-decoration: none; }
        .footer a:hover { color: #3B82F6; }

        /* ── Divider ──────────────────────────────────────── */
        hr { border-color: #0F172A !important; margin: 32px 0 !important; }
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
# App entry point
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="CapMP3 — cap.so to MP3",
    page_icon="🎵",
    layout="centered",
)

_init_session()
_inject_css()

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

# ── Converter card ────────────────────────────────────────────────────────────
st.markdown('<div class="converter-card">', unsafe_allow_html=True)

url_input = st.text_input(
    "url",
    placeholder="https://cap.so/s/...  or  https://cap.link/...",
    label_visibility="collapsed",
    key="url_input",
)

# Credit pill — only shown when registered
if st.session_state.registered:
    credits = _credits()
    if credits > 0:
        label = "credit" if credits == 1 else "credits"
        st.markdown(
            f'<div class="credit-pill">✦ {credits} {label} remaining</div>',
            unsafe_allow_html=True,
        )

clicked = st.button("Convert to MP3 →", type="primary", use_container_width=True, key="convert_btn")

st.markdown("</div>", unsafe_allow_html=True)

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
    elif not st.session_state.registered:
        # Not registered yet — show inline email gate
        st.session_state.pending_url = url
        st.session_state.show_gate   = True
        st.rerun()
    elif _credits() <= 0:
        # Registered but out of credits — show pricing
        st.session_state.show_gate = False
        pass  # falls through to pricing section below
    else:
        # Registered and has credits — convert
        _run_conversion(url)

# ── Inline email gate ─────────────────────────────────────────────────────────
if st.session_state.show_gate and not st.session_state.registered:
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(
        """
        <div class="gate-card">
            <p class="gate-title">Get your first MP3 free</p>
            <p class="gate-sub">
                Enter your email to unlock <strong style="color:#F1F5F9;">1 free download</strong>.
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

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(
    f"""
    <div class="footer">
        cap.so · cap.link · Files deleted immediately after download
        · <a href="mailto:{CONTACT_EMAIL}">Contact</a>
    </div>
    """,
    unsafe_allow_html=True,
)
