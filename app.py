"""
CapMP3 - Audio extractor for cap.so / cap.link
Run: streamlit run app.py
"""

import gc
import os
import re
import socket
import time
import tempfile
import subprocess
from collections import defaultdict
from urllib.parse import urlparse, urlencode

import hashlib
import html as html_lib
import requests
import streamlit as st
import streamlit.components.v1 as _components

# Load .env for local development (no-op in production where env vars are set directly)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_VIDEO_MB = 1500
FREE_CREDITS = 1

STRIPE_BASIC_URL = "https://buy.stripe.com/5kQeVdaHV20cd871mvdMI00"
STRIPE_PRO_URL   = "https://buy.stripe.com/8x26oHaHV48kecbaX5dMI01"
CONTACT_EMAIL    = "info@capmp3.com"

RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_WINDOW   = 60

# ---------------------------------------------------------------------------
# Analytics — public IDs (safe to commit, appear in HTML source anyway)
# ---------------------------------------------------------------------------
GA4_MEASUREMENT_ID = "G-JE9JPT9V86"
CLARITY_PROJECT_ID = "wgaot1z1em"

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

# In-memory sets — persist for the lifetime of the server process.
# Blocks same email or same browser fingerprint from claiming a second free credit.
_used_emails: set        = set()
_used_fingerprints: set  = set()

_request_log: dict = defaultdict(list)


def _get_client_ip() -> str:
    try:
        forwarded = st.context.headers.get("X-Forwarded-For", "")
        if forwarded:
            # Railway sets the real client IP as the leftmost entry
            return forwarded.split(",")[0].strip()
    except Exception:
        pass
    return "unknown"


def _make_fingerprint() -> str:
    """Hash of IP + User-Agent — stable per browser/device, resets on server restart."""
    ip = _get_client_ip()
    try:
        ua = st.context.headers.get("User-Agent", "")
    except Exception:
        ua = ""
    raw = f"{ip}:{ua[:120]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _get_cookie_device_id() -> str:
    """
    Read the capmp3_did cookie injected by client-side JS.
    Cookie persists across page refreshes and IP changes, making it a
    more reliable device identifier than IP+UA alone.
    Returns empty string if cookie is absent or malformed.
    """
    try:
        cookie_header = st.context.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == "capmp3_did":
                val = v.strip()
                if re.match(r"^[0-9a-f]{32}$", val):
                    return val
    except Exception:
        pass
    return ""


def _has_used_free_credit(email: str) -> bool:
    """
    Check whether this email address or device already consumed a free credit.
    Two independent signals:
      - IP+UA fingerprint  (fast, works even without cookie)
      - Cookie device ID   (reliable across IP changes, set by JS)
    Each is checked in-memory first, then in Supabase.
    """
    fp  = _make_fingerprint()
    did = _get_cookie_device_id()

    if email in _used_emails:
        return True
    if fp in _used_fingerprints:
        return True
    if did and ("c:" + did) in _used_fingerprints:
        return True

    # Persistent DB checks (survive server restarts and multi-worker deployments)
    if _is_fingerprint_used_in_supabase(fp):
        return True
    if did and _is_fingerprint_used_in_supabase("c:" + did):
        return True

    return False


def _mark_free_credit_used(email: str) -> None:
    """
    Persist both the IP+UA fingerprint and the cookie device ID so that
    subsequent attempts from the same device — even with a different email
    or after an IP change — are blocked.
    """
    fp  = _make_fingerprint()
    did = _get_cookie_device_id()

    _used_emails.add(email.lower().strip())
    _used_fingerprints.add(fp)
    _save_fingerprint_to_supabase(fp, email)

    if did:
        _used_fingerprints.add("c:" + did)
        _save_fingerprint_to_supabase("c:" + did, email)


def _is_rate_limited() -> bool:
    ip  = _get_client_ip()
    now = time.time()
    _request_log[ip] = [t for t in _request_log[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(_request_log[ip]) >= RATE_LIMIT_REQUESTS:
        return True
    _request_log[ip].append(now)
    return False


def _email_domain_is_real(email: str) -> bool:
    """
    Verify that the email domain exists in DNS (A/AAAA or MX record).
    Catches nonsense domains like khjbr@khtr.khe that have no DNS entry.
    Returns True (allow) on any timeout/network error so legit users
    are never blocked by a flaky DNS server.
    """
    try:
        domain = email.split("@", 1)[1].lower().rstrip(".")
        tld = domain.rsplit(".", 1)[-1]
        # Reject structurally impossible TLDs (< 2 or > 13 chars)
        if len(tld) < 2 or len(tld) > 13:
            return False
        socket.setdefaulttimeout(3)
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        # NXDOMAIN or similar — domain does not exist
        return False
    except Exception:
        # Network error, timeout, etc. — fail open (don't block real users)
        return True
    finally:
        socket.setdefaulttimeout(None)


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
# URL security — SSRF prevention
# ---------------------------------------------------------------------------

# Domains the user is permitted to submit
_USER_URL_ALLOWED_DOMAINS: frozenset[str] = frozenset({"cap.so", "cap.link"})

# Regex patterns that match private / loopback / link-local IP ranges
_PRIVATE_IP_PATTERNS: list[re.Pattern] = [
    re.compile(r"^127\."),           # loopback
    re.compile(r"^10\."),            # RFC-1918
    re.compile(r"^172\.(1[6-9]|2\d|3[01])\."),  # RFC-1918
    re.compile(r"^192\.168\."),      # RFC-1918
    re.compile(r"^169\.254\."),      # link-local / AWS metadata
    re.compile(r"^0\."),             # "this" network
    re.compile(r"^::1$"),            # IPv6 loopback
    re.compile(r"^fc", re.IGNORECASE),  # IPv6 ULA
    re.compile(r"^fe80", re.IGNORECASE),  # IPv6 link-local
    re.compile(r"^localhost$", re.IGNORECASE),
]


def _validate_user_url(url: str) -> None:
    """
    Enforce that a user-submitted URL targets only cap.so / cap.link.
    Raises ValueError with a safe, user-visible message on violation.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid URL format.")

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only https:// URLs are supported.")

    host = (parsed.hostname or "").lower().strip(".")

    # Block raw private-IP URLs (e.g. http://192.168.1.1/...)
    for pattern in _PRIVATE_IP_PATTERNS:
        if pattern.search(host):
            raise ValueError("Only cap.so and cap.link URLs are supported.")

    # Domain whitelist
    allowed = any(
        host == domain or host.endswith("." + domain)
        for domain in _USER_URL_ALLOWED_DOMAINS
    )
    if not allowed:
        raise ValueError("Only cap.so and cap.link URLs are supported.")

    # DNS rebinding protection — verify the resolved IP is not private.
    # Validates AFTER whitelist so we only resolve whitelisted domains.
    try:
        addrs = socket.getaddrinfo(host, None)
        for (_, _, _, _, sockaddr) in addrs:
            resolved_ip = sockaddr[0]
            for pattern in _PRIVATE_IP_PATTERNS:
                if pattern.search(resolved_ip):
                    raise ValueError("Only cap.so and cap.link URLs are supported.")
    except ValueError:
        raise  # re-raise our own error
    except Exception:
        pass  # DNS failure — let the downstream request fail naturally


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
    """Convert to MP3 using Popen so we can update the progress bar while ffmpeg runs."""
    cmd = [FFMPEG, "-y", "-i", input_path, "-vn",
           "-acodec", "libmp3lame", "-q:a", "2", "-ar", "44100", output_path]
    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found. Please install ffmpeg.")

    start   = time.time()
    timeout = 300  # 5 minutes

    while proc.poll() is None:
        elapsed = time.time() - start
        if elapsed > timeout:
            proc.kill()
            raise RuntimeError("Conversion took too long (> 5 min). Please try again.")
        # Smooth progress: 0 → 90 % over ~60 s, capped at 90 % until ffmpeg exits
        pct = min(elapsed / 60.0 * 0.90, 0.90)
        bar.progress(pct, f"{label}…  {int(elapsed)}s")
        time.sleep(0.5)

    _, stderr_bytes = proc.communicate()
    if proc.returncode != 0:
        err = (stderr_bytes or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg error:\n{err[-600:]}")
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
        resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True,
                            stream=True)
        # Guard against huge responses — read max 1 MB
        raw = b""
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            raw += chunk
            if len(raw) > 1_000_000:
                break
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")

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
        "registered":       False,
        "email":            "",
        "credits":          0,
        "show_gate":        False,
        "pending_url":      "",
        "do_convert":       False,
        "video_meta":       None,
        "_scroll_pricing":  False,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def _credits() -> int:
    return st.session_state.credits


def _deduct_credit() -> None:
    st.session_state.credits = max(0, st.session_state.credits - 1)
    email = st.session_state.get("email", "")
    if email:
        _mark_free_credit_used(email)           # in-memory + fingerprint
        _deduct_credit_in_supabase(email)       # persistent DB decrement


def _refund_credit() -> None:
    """Restore one credit when conversion fails after deduction."""
    st.session_state.credits = st.session_state.credits + 1
    email = st.session_state.get("email", "")
    if email:
        try:
            db = _get_supabase()
            if db:
                db.rpc("increment_credits", {"user_email": email}).execute()
        except Exception:
            pass


def _add_credits(amount: int) -> None:
    # TODO: Validate Stripe Webhook before calling —
    # use stripe.Webhook.construct_event(payload, sig, secret)
    st.session_state.credits += amount


# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

_supa_client = None   # module-level singleton


def _get_supabase():
    """Lazy-init Supabase client using service_role key (bypasses RLS)."""
    global _supa_client
    if _supa_client is not None:
        return _supa_client
    try:
        from supabase import create_client
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_SERVICE_KEY", "")
        if url and key and not key.startswith("your_"):
            _supa_client = create_client(url, key)
    except Exception:
        pass
    return _supa_client


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _save_email_to_supabase(email: str) -> None:
    """Upsert user — preserves existing credits_balance on conflict."""
    db = _get_supabase()
    if not db:
        return
    try:
        db.table("users").upsert(
            {"email": email, "credits_balance": FREE_CREDITS},
            on_conflict="email",
            ignore_duplicates=True,   # INSERT ... ON CONFLICT DO NOTHING
        ).execute()
    except Exception:
        pass


def _load_credits_from_supabase(email: str) -> int | None:
    """Return credits_balance for email, or None if not found."""
    db = _get_supabase()
    if not db:
        return None
    try:
        res = (
            db.table("users")
            .select("credits_balance")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        if res.data:
            return int(res.data[0].get("credits_balance") or 0)
    except Exception:
        pass
    return None


def _deduct_credit_in_supabase(email: str) -> None:
    """Atomically decrement credits_balance (floor 0) via DB function."""
    db = _get_supabase()
    if not db or not email:
        return
    try:
        db.rpc("decrement_credits", {"user_email": email}).execute()
    except Exception:
        pass


def _is_fingerprint_used_in_supabase(fp_hash: str) -> bool:
    """Check whether this browser fingerprint already consumed a free credit."""
    db = _get_supabase()
    if not db:
        return False
    try:
        res = (
            db.table("fingerprints")
            .select("id")
            .eq("fp_hash", fp_hash)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:
        return True   # fail-closed: block on DB error


def _save_fingerprint_to_supabase(fp_hash: str, email: str) -> None:
    """Persist fingerprint → email mapping."""
    db = _get_supabase()
    if not db:
        return
    try:
        db.table("fingerprints").upsert(
            {"fp_hash": fp_hash, "email": email}
        ).execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CSS — clean, minimal, trustworthy
# ---------------------------------------------------------------------------
# Analytics injection
# ---------------------------------------------------------------------------

def _inject_analytics() -> None:
    """
    Inject GA4 + Microsoft Clarity into the PARENT frame (top-level window).

    Streamlit components run inside a same-origin iframe so we can reach
    window.parent.document and append <script> tags directly to <head>.
    Guards (__ga4_injected / __clarity_injected) prevent duplicate injection
    on Streamlit reruns.
    """
    ga4_ready     = GA4_MEASUREMENT_ID     and not GA4_MEASUREMENT_ID.endswith("XXXXXXXXXX")
    clarity_ready = CLARITY_PROJECT_ID     and not CLARITY_PROJECT_ID.endswith("XXXXXXXXXX")

    if not ga4_ready and not clarity_ready:
        return  # neither configured — skip silently

    ga4_block = ""
    if ga4_ready:
        ga4_block = f"""
                // ── Google Analytics 4 ──────────────────────────────────────
                if (!p.__ga4_injected) {{
                    p.__ga4_injected = true;
                    var ga = p.document.createElement('script');
                    ga.async = true;
                    ga.src = 'https://www.googletagmanager.com/gtag/js?id={GA4_MEASUREMENT_ID}';
                    p.document.head.appendChild(ga);
                    p.dataLayer = p.dataLayer || [];
                    p.gtag = function() {{ p.dataLayer.push(arguments); }};
                    p.gtag('js', new Date());
                    p.gtag('config', '{GA4_MEASUREMENT_ID}', {{
                        send_page_view: true,
                        anonymize_ip: true
                    }});
                }}"""

    clarity_block = ""
    if clarity_ready:
        clarity_block = f"""
                // ── Microsoft Clarity ────────────────────────────────────────
                if (!p.__clarity_injected) {{
                    p.__clarity_injected = true;
                    (function(c,l,a,r,i,t,y){{
                        c[a]=c[a]||function(){{(c[a].q=c[a].q||[]).push(arguments)}};
                        t=l.createElement(r);t.async=1;
                        t.src='https://www.clarity.ms/tag/'+i;
                        y=l.getElementsByTagName(r)[0];y.parentNode.insertBefore(t,y);
                    }})(p,p.document,'clarity','script','{CLARITY_PROJECT_ID}');
                }}"""

    _components.html(
        f"""
        <script>
        (function() {{
            try {{
                var p = window.parent;
                {ga4_block}
                {clarity_block}
            }} catch(e) {{}}
        }})();
        </script>
        """,
        height=0,
    )


# ---------------------------------------------------------------------------

_SEO_TITLE       = "CapMP3 – Download cap.so & cap.link Recordings as MP3"
_SEO_DESCRIPTION = (
    "Convert any cap.so or cap.link screen recording to MP3 in one click. "
    "Free to try — no software install needed. Just paste the URL and download."
)
_SEO_URL         = "https://capmp3.com"
_SEO_OG_IMAGE    = "https://capmp3.com/og-image.png"   # replace once image exists


def _inject_seo_meta() -> None:
    """
    Inject <meta> / <link> SEO tags into the parent document <head>.
    Streamlit doesn't expose head injection natively; we reach it via
    window.parent.document from a same-origin component iframe.
    """
    _components.html(
        f"""
        <script>
        (function() {{
            try {{
                var p = window.parent;
                var d = p.document;
                if (p.__seo_injected) return;
                p.__seo_injected = true;

                function meta(attrs) {{
                    var el = d.createElement('meta');
                    for (var k in attrs) el.setAttribute(k, attrs[k]);
                    d.head.appendChild(el);
                }}
                function link(attrs) {{
                    var el = d.createElement('link');
                    for (var k in attrs) el.setAttribute(k, attrs[k]);
                    d.head.appendChild(el);
                }}

                // Primary meta
                meta({{"name":"description", "content":{_SEO_DESCRIPTION!r}}});

                // Open Graph
                meta({{"property":"og:type",        "content":"website"}});
                meta({{"property":"og:url",         "content":{_SEO_URL!r}}});
                meta({{"property":"og:title",       "content":{_SEO_TITLE!r}}});
                meta({{"property":"og:description", "content":{_SEO_DESCRIPTION!r}}});
                meta({{"property":"og:image",       "content":{_SEO_OG_IMAGE!r}}});

                // Twitter card
                meta({{"name":"twitter:card",        "content":"summary_large_image"}});
                meta({{"name":"twitter:title",       "content":{_SEO_TITLE!r}}});
                meta({{"name":"twitter:description", "content":{_SEO_DESCRIPTION!r}}});

                // Canonical
                link({{"rel":"canonical", "href":{_SEO_URL!r}}});

            }} catch(e) {{}}
        }})();
        </script>
        """,
        height=0,
    )


# ---------------------------------------------------------------------------

def _inject_effects() -> None:
    """
    Inject aurora blobs + particle canvas into the parent frame.
    Pure visual layer — no functional impact.
    """
    _components.html(
        """
        <script>
        (function() {
            try {
                var p = window.parent, d = p.document;
                if (p.__effects_injected) return;
                p.__effects_injected = true;

                /* ── Aurora blobs ─────────────────────────── */
                var style = d.createElement('style');
                style.textContent = [
                  '@keyframes aurora1{0%{transform:translate(0,0) scale(1)}33%{transform:translate(60px,-40px) scale(1.12)}66%{transform:translate(-30px,50px) scale(.93)}100%{transform:translate(0,0) scale(1)}}',
                  '@keyframes aurora2{0%{transform:translate(0,0) scale(1)}40%{transform:translate(-70px,30px) scale(1.08)}75%{transform:translate(40px,-55px) scale(.95)}100%{transform:translate(0,0) scale(1)}}',
                  '@keyframes aurora3{0%{transform:translate(0,0) scale(1)}30%{transform:translate(50px,60px) scale(1.15)}70%{transform:translate(-40px,-30px) scale(.9)}100%{transform:translate(0,0) scale(1)}}',
                  '#cap-aurora{position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;overflow:hidden;}',
                  '#cap-aurora .a1{position:absolute;top:-10%;left:-5%;width:60vw;height:60vw;border-radius:50%;background:radial-gradient(ellipse,rgba(124,58,237,.38) 0%,transparent 70%);filter:blur(60px);animation:aurora1 18s ease-in-out infinite;}',
                  '#cap-aurora .a2{position:absolute;top:30%;right:-10%;width:50vw;height:50vw;border-radius:50%;background:radial-gradient(ellipse,rgba(6,182,212,.28) 0%,transparent 70%);filter:blur(60px);animation:aurora2 22s ease-in-out infinite;}',
                  '#cap-aurora .a3{position:absolute;bottom:-15%;left:25%;width:45vw;height:45vw;border-radius:50%;background:radial-gradient(ellipse,rgba(124,58,237,.22) 0%,transparent 70%);filter:blur(60px);animation:aurora3 26s ease-in-out infinite;}',
                  '#cap-grid{position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;background-image:linear-gradient(rgba(148,163,184,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.04) 1px,transparent 1px);background-size:48px 48px;-webkit-mask-image:radial-gradient(ellipse at center,#000 30%,transparent 80%);mask-image:radial-gradient(ellipse at center,#000 30%,transparent 80%);}'
                ].join('');
                d.head.appendChild(style);

                var wrap = d.createElement('div'); wrap.id='cap-aurora';
                ['a1','a2','a3'].forEach(function(c){var b=d.createElement('div');b.className=c;wrap.appendChild(b);});
                d.body.insertBefore(wrap, d.body.firstChild);

                var grid = d.createElement('div'); grid.id='cap-grid';
                d.body.insertBefore(grid, d.body.firstChild);

                /* ── Particle canvas ─────────────────────── */
                var canvas = d.createElement('canvas');
                canvas.id = 'cap-particles';
                canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;';
                d.body.insertBefore(canvas, d.body.firstChild);

                var ctx = canvas.getContext('2d');
                var W, H, pts = [];
                function resize() {
                    W = canvas.width  = p.innerWidth;
                    H = canvas.height = p.innerHeight;
                }
                resize();
                p.addEventListener('resize', resize);

                for (var i = 0; i < 55; i++) {
                    pts.push({
                        x: Math.random()*W, y: Math.random()*H,
                        vx:(Math.random()-.5)*.35, vy:(Math.random()-.5)*.35,
                        r: .8+Math.random()*1.6,
                        a: .12+Math.random()*.3,
                        c: Math.random()>.5 ? '124,58,237' : '6,182,212'
                    });
                }
                function tick() {
                    ctx.clearRect(0,0,W,H);
                    pts.forEach(function(p_) {
                        p_.x += p_.vx; p_.y += p_.vy;
                        if(p_.x<0) p_.x=W; if(p_.x>W) p_.x=0;
                        if(p_.y<0) p_.y=H; if(p_.y>H) p_.y=0;
                        ctx.beginPath();
                        ctx.arc(p_.x, p_.y, p_.r, 0, 6.2832);
                        ctx.fillStyle = 'rgba('+p_.c+','+p_.a+')';
                        ctx.fill();
                    });
                    requestAnimationFrame(tick);
                }
                tick();

            } catch(e) {}
        })();
        </script>
        """,
        height=0,
    )


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@600;700&family=Inter:wght@400;500;600;700&display=swap');

        /* ── Reset & base ─────────────────────────────────── */
        html, body {
            background: #0F172A !important;
            font-family: 'Inter', sans-serif !important;
        }
        [class*="css"] { font-family: 'Inter', sans-serif !important; }
        h1, h2, h3, .hero-title, .gate-title, .pricing-title,
        .content-h2, .referral-title, .success-title, .logo-text {
            font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
        }
        /* stApp must be transparent so the fixed background effects show through */
        .stApp {
            background: transparent !important;
        }
        /* Content sits above the z-index:0 effect layers */
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"],
        [data-testid="stMainBlockContainer"] {
            position: relative !important;
            z-index: 1 !important;
            background: transparent !important;
        }

        /* ── Hide sidebar, toolbar, header ───────────────── */
        [data-testid="stSidebar"],
        [data-testid="collapsedControl"],
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        #MainMenu,
        .stAppDeployButton { display: none !important; }

        /* ── Page wrapper: center & constrain width ──────── */
        .block-container {
            max-width: 1240px !important;
            padding: 40px 48px 80px !important;
            margin: 0 auto !important;
            position: relative;
            z-index: 1;
        }
        /* Hero content stays centered at 720px, full-bleed bg */
        .hero-wrap {
            max-width: 720px;
            margin: 0 auto;
            text-align: left;
        }
        /* Converter card centered at 720px */
        .converter-wrap {
            max-width: 720px;
            margin: 0 auto;
        }
        [data-testid="stForm"] {
            max-width: 720px !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        /* Center narrow elements within wide layout */
        .trust-bar, .credit-pill, .success-box, .warn-box,
        .error-box, .success-card, .gate-card, .preview-card {
            max-width: 720px;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        /* Download button stays within centered column */
        [data-testid="stDownloadButton"] {
            max-width: 720px;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        /* Progress bar centered */
        [data-testid="stProgressBarContainer"],
        .stProgress {
            max-width: 720px !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        @media (max-width: 900px) {
            .block-container { padding: 32px 24px 48px !important; }
            .steps-grid { grid-template-columns: 1fr !important; }
            .use-cases  { grid-template-columns: 1fr !important; }
            .feature-list { grid-template-columns: 1fr !important; }
        }

        /* ── Logo — centered ─────────────────────────────── */
        .logo {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 9px;
            margin-bottom: 24px;
        }
        .logo-text {
            font-size: 18.7px !important;
            font-weight: 700 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: initial !important;
            letter-spacing: -.02em !important;
            line-height: 1 !important;
        }
        .logo-mp3 {
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        /* ── Hero ─────────────────────────────────────────── */
        .hero-title {
            font-size: clamp(28px, 4.2vw, 48px) !important;
            font-weight: 700 !important;
            line-height: 1.05 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 0 12px !important;
            letter-spacing: -.03em !important;
        }
        .grad {
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .hero-sub {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            margin: 0 0 24px !important;
            line-height: 1.55 !important;
        }

        /* ── Converter card ───────────────────────────────── */
        .converter-card {
            background: rgba(15,23,42,.60);
            border: 1px solid rgba(148,163,184,.30);
            border-radius: 22px;
            padding: 24px;
            margin-bottom: 16px;
            backdrop-filter: blur(26px);
            -webkit-backdrop-filter: blur(26px);
            box-shadow: 0 20px 60px -20px rgba(124,58,237,.35),
                        inset 0 0 0 1px rgba(255,255,255,.04);
        }

        /* ── Input ────────────────────────────────────────── */
        .stTextInput > div > div > input {
            background: rgba(2,6,23,.35) !important;
            border: 1.5px solid rgba(148,163,184,.10) !important;
            border-radius: 16px !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            font-size: 16px !important;
            padding: 13px 16px !important;
            transition: border-color .25s, box-shadow .25s, background .25s;
            font-family: 'Inter', sans-serif !important;
        }
        .stTextInput > div > div > input:focus {
            background: rgba(2,6,23,.65) !important;
            border-color: rgba(6,182,212,.40) !important;
            box-shadow: 0 0 0 3px rgba(6,182,212,.13),
                        inset 0 0 28px rgba(6,182,212,.10) !important;
            outline: none !important;
        }
        .stTextInput > div > div > input::placeholder {
            color: rgba(148,163,184,.55) !important;
            -webkit-text-fill-color: rgba(148,163,184,.55) !important;
        }
        .stTextInput label { display: none !important; }

        /* ── Primary button ───────────────────────────────── */
        .stButton > button[kind="primary"],
        .stFormSubmitButton > button[kind="primary"],
        [data-testid="stFormSubmitButton"] > button,
        [data-testid="stBaseButton-primary"] {
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%) !important;
            border: none !important;
            border-radius: 16px !important;
            color: #fff !important;
            -webkit-text-fill-color: #fff !important;
            font-weight: 600 !important;
            font-size: 15px !important;
            letter-spacing: .005em !important;
            transition: transform .18s ease, box-shadow .18s ease !important;
            box-shadow: 0 8px 24px rgba(124,58,237,.27),
                        inset 0 0 0 1px rgba(255,255,255,.12) !important;
        }
        .stButton > button[kind="primary"]:hover,
        .stFormSubmitButton > button[kind="primary"]:hover,
        [data-testid="stFormSubmitButton"] > button:hover,
        [data-testid="stBaseButton-primary"]:hover {
            transform: translateY(-1px) !important;
            filter: brightness(1.12) !important;
            box-shadow: 0 16px 40px rgba(124,58,237,.50),
                        inset 0 0 0 1px rgba(255,255,255,.20) !important;
        }

        /* ── Secondary / link buttons ─────────────────────── */
        .stButton > button[kind="secondary"],
        .stFormSubmitButton > button[kind="secondary"],
        .stLinkButton > a {
            background: rgba(148,163,184,.10) !important;
            border: 1.5px solid rgba(148,163,184,.18) !important;
            border-radius: 12px !important;
            color: #CBD5E1 !important;
            -webkit-text-fill-color: #CBD5E1 !important;
            font-weight: 500 !important;
            font-size: 13.5px !important;
            transition: border-color .15s, color .15s !important;
        }
        .stButton > button[kind="secondary"]:hover,
        .stLinkButton > a:hover {
            border-color: rgba(6,182,212,.45) !important;
            color: #67E8F9 !important;
            -webkit-text-fill-color: #67E8F9 !important;
        }

        /* ── Trust bar ────────────────────────────────────── */
        .trust-bar {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 36px;
            flex-wrap: wrap;
            margin: 28px auto 0;
            max-width: 720px;
        }
        .trust-tile-item {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .trust-tile {
            width: 44px; height: 44px;
            border-radius: 12px;
            background: rgba(15,23,42,.60);
            border: 1px solid rgba(148,163,184,.14);
            display: flex; align-items: center; justify-content: center;
            flex-shrink: 0;
            backdrop-filter: blur(8px);
        }
        .trust-text {
            display: flex;
            flex-direction: column;
            gap: 1px;
        }
        .trust-label {
            font-size: 13.5px !important;
            font-weight: 600 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            line-height: 1.2 !important;
        }
        .trust-sub {
            font-size: 11.5px !important;
            color: rgba(148,163,184,.70) !important;
            -webkit-text-fill-color: rgba(148,163,184,.70) !important;
            line-height: 1.2 !important;
        }

        /* ── Email gate ───────────────────────────────────── */
        .gate-card {
            background: rgba(15,23,42,.60);
            border: 1px solid rgba(148,163,184,.20);
            border-radius: 22px;
            padding: 28px 24px 8px;
            margin-bottom: 4px;
            backdrop-filter: blur(26px);
            -webkit-backdrop-filter: blur(26px);
            box-shadow: 0 20px 60px -20px rgba(124,58,237,.35),
                        inset 0 0 0 1px rgba(255,255,255,.04);
        }
        .gate-title {
            font-size: 20px !important;
            font-weight: 700 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 0 6px !important;
            letter-spacing: -.02em !important;
        }
        .gate-sub {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            margin: 0 0 20px !important;
            line-height: 1.6 !important;
        }
        .privacy-note {
            text-align: center !important;
            font-size: 16px !important;
            color: rgba(148,163,184,.70) !important;
            -webkit-text-fill-color: rgba(148,163,184,.70) !important;
            margin: 8px 0 0 !important;
        }
        .gate-strong {
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            font-weight: 700 !important;
        }

        /* ── Credit pill ──────────────────────────────────── */
        .credit-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: rgba(124,58,237,.12);
            border: 1px solid rgba(124,58,237,.25);
            border-radius: 999px;
            padding: 5px 12px;
            font-size: 16px;
            color: #C4B5FD;
            font-weight: 500;
            margin-bottom: 16px;
        }
        .credit-pill.empty {
            background: rgba(239,68,68,.10);
            border-color: rgba(239,68,68,.25);
            color: #FCA5A5;
        }

        /* ── Pricing ──────────────────────────────────────── */
        .pricing-header {
            margin: 40px auto 24px;
            max-width: 720px;
            text-align: center;
        }
        /* pricing-label shares pill style with content-label (defined above) */
        .pricing-title {
            font-size: 40px !important;
            font-weight: 800 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 0 8px !important;
            letter-spacing: -.035em !important;
            line-height: 1.1 !important;
        }
        .pricing-sub {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            margin: 0 !important;
        }
        .pricing-card {
            background: rgba(15,23,42,.50);
            border: 1px solid rgba(148,163,184,.12);
            border-radius: 18px;
            padding: 24px 20px 20px;
            position: relative;
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            box-shadow: 0 20px 60px -20px rgba(124,58,237,.20),
                        inset 0 0 0 1px rgba(255,255,255,.04);
            transition: border-color .2s;
        }
        .pricing-card--featured {
            border-color: rgba(124,58,237,.45);
            box-shadow: 0 20px 60px -20px rgba(124,58,237,.35),
                        inset 0 0 0 1px rgba(255,255,255,.06);
        }
        .plan-badge {
            position: absolute;
            top: -11px; left: 50%;
            transform: translateX(-50%);
            background: linear-gradient(135deg, #7C3AED, #06B6D4);
            color: #fff;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: .08em;
            padding: 4px 12px;
            border-radius: 999px;
            white-space: nowrap;
        }
        .plan-name  { font-size: 16px !important; font-weight: 600 !important; color: rgba(203,213,225,.78) !important; -webkit-text-fill-color: rgba(203,213,225,.78) !important; margin: 0 0 8px !important; }
        .plan-price { font-size: 32px !important; font-weight: 700 !important; color: #F1F5F9 !important; -webkit-text-fill-color: #F1F5F9 !important; margin: 0 !important; line-height: 1 !important; letter-spacing: -.03em !important; font-family: 'Plus Jakarta Sans', sans-serif !important; }
        .plan-credits { font-size: 16px !important; color: #67E8F9 !important; -webkit-text-fill-color: #67E8F9 !important; font-weight: 600 !important; margin: 5px 0 2px !important; }
        .plan-unit  { font-size: 16px !important; color: rgba(148,163,184,.70) !important; -webkit-text-fill-color: rgba(148,163,184,.70) !important; margin: 0 0 16px !important; }
        .plan-features { list-style: none !important; padding: 0 !important; margin: 0 0 20px !important; }
        .plan-features li { font-size: 16px !important; color: rgba(203,213,225,.78) !important; -webkit-text-fill-color: rgba(203,213,225,.78) !important; padding: 3px 0 !important; }

        /* ── Hide Streamlit heading anchor icons ─────────── */
        [data-testid="stMarkdownContainer"] h1 a,
        [data-testid="stMarkdownContainer"] h2 a,
        [data-testid="stMarkdownContainer"] h3 a,
        .stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a {
            display: none !important;
        }

        /* ── Converter form card ──────────────────────────── */
        .url-label {
            font-size: 16px !important;
            font-weight: 500 !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            margin: 0 0 8px !important;
            letter-spacing: -.01em !important;
            display: block !important;
        }
        /* ══════════════════════════════════════════════════
           FORM — pixel-matched to landing.jsx design spec
           cardBg  = rgba(15,23,42,.60)
           cardBorder = rgba(148,163,184,.30)
           cardShadow = 0 20px 60px -20px rgba(124,58,237,.35), inset 0 0 0 1px rgba(255,255,255,.04)
           blur    = 26px
           innerRow: padding 6px, gap 10px
           inputBg = rgba(2,6,23,.35), focus rgba(2,6,23,.65)
           inputBorder = rgba(148,163,184,.10), focus rgba(6,182,212,.40)
           inputFocusRing = 0 0 0 3px rgba(6,182,212,.13), inset 0 0 28px rgba(6,182,212,.27)
           height  = 60px, borderRadius = 16px
        ═══════════════════════════════════════════════════ */

        /* 1 — Outer glass card */
        [data-testid="stForm"] {
            background: rgba(15,23,42,.60) !important;
            border: 1px solid rgba(148,163,184,.30) !important;
            border-radius: 22px !important;
            padding: 8px !important;
            backdrop-filter: blur(26px) !important;
            -webkit-backdrop-filter: blur(26px) !important;
            box-shadow: 0 20px 60px -20px rgba(124,58,237,.35),
                        inset 0 0 0 1px rgba(255,255,255,.04) !important;
            max-width: 720px !important;
            margin-left: auto !important;
            margin-right: auto !important;
            overflow: hidden !important;
        }

        /* 2 — Inner flex row */
        [data-testid="stForm"] [data-testid="stVerticalBlock"] {
            display: flex !important;
            flex-direction: row !important;
            align-items: center !important;
            gap: 8px !important;
            padding: 5px !important;
            margin: 0 !important;
        }

        /* 3 — TextInput flex item */
        [data-testid="stForm"] [data-testid="stTextInput"] {
            flex: 1 !important;
            min-width: 0 !important;
            margin-bottom: 0 !important;
        }
        [data-testid="stForm"] [data-testid="stTextInput"] > div {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0 !important;
            margin: 0 !important;
        }

        /* 4 — Inner input label (the styled dark box) */
        [data-testid="stForm"] [data-testid="stTextInput"] > div > div,
        [data-testid="stForm"] [data-baseweb="input"] {
            display: flex !important;
            align-items: center !important;
            height: 48px !important;
            border-radius: 13px !important;
            background: rgba(2,6,23,.35) !important;
            border: 1px solid rgba(148,163,184,.10) !important;
            box-shadow: none !important;
            padding: 0 18px 0 14px !important;
            gap: 10px !important;
            transition: background .25s ease, border-color .25s ease, box-shadow .25s ease !important;
            box-sizing: border-box !important;
        }
        /* Focus state on inner input label */
        [data-testid="stForm"] [data-testid="stTextInput"] > div > div:focus-within,
        [data-testid="stForm"] [data-baseweb="input"]:focus-within {
            background: rgba(2,6,23,.65) !important;
            border-color: rgba(6,182,212,.40) !important;
            box-shadow: 0 0 0 3px rgba(6,182,212,.13),
                        inset 0 0 28px rgba(6,182,212,.27) !important;
        }

        /* 5 — Link icon (inline SVG via ::before) */
        [data-testid="stForm"] [data-testid="stTextInput"] > div > div::before {
            content: '' !important;
            width: 20px !important; height: 20px !important;
            flex-shrink: 0 !important;
            background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='rgba(148,163,184,0.55)' stroke-width='1.75' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1'/%3E%3Cpath d='M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1'/%3E%3C/svg%3E") no-repeat center !important;
            pointer-events: none !important;
        }

        /* 6 — Actual <input> element */
        [data-testid="stForm"] [data-testid="stTextInput"] > div > div > input {
            flex: 1 !important;
            background: transparent !important;
            border: none !important;
            outline: none !important;
            box-shadow: none !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            font-size: 16px !important;
            font-family: inherit !important;
            letter-spacing: -.005em !important;
            height: 100% !important;
            padding: 0 !important;
            min-width: 0 !important;
        }
        [data-testid="stForm"] [data-testid="stTextInput"] > div > div > input:focus,
        [data-testid="stForm"] [data-testid="stTextInput"] > div > div > input:focus-visible {
            border: none !important;
            outline: none !important;
            box-shadow: none !important;
        }

        /* 7 — Submit button */
        [data-testid="stForm"] [data-testid="stFormSubmitButton"] {
            flex-shrink: 0 !important;
            margin: 0 !important;
        }
        [data-testid="stForm"] [data-testid="stFormSubmitButton"] > button {
            height: 48px !important;
            padding: 0 24px !important;
            border-radius: 13px !important;
            border: none !important;
            white-space: nowrap !important;
            font-size: 15px !important;
            font-weight: 600 !important;
            letter-spacing: .005em !important;
            margin: 0 !important;
        }

        /* Hide "Press Enter to submit form" Streamlit hint */
        [data-testid="InputInstructions"],
        small[data-testid="InputInstructions"],
        .stTextInput small {
            display: none !important;
        }

        /* ── Progress bar ─────────────────────────────────── */
        @keyframes shimmer {
            0%   { transform: translateX(-150%); }
            100% { transform: translateX(550%); }
        }
        .stProgress { margin: 16px 0 4px !important; }
        .stProgress > div > div > div {
            height: 14px !important;
            border-radius: 999px !important;
            background: rgba(148,163,184,.12) !important;
            overflow: hidden !important;
            position: relative !important;
        }
        .stProgress > div > div > div > div {
            height: 14px !important;
            border-radius: 999px !important;
            background: linear-gradient(90deg, #7C3AED 0%, #06B6D4 100%) !important;
            transition: width .3s ease !important;
        }
        .stProgress > div > div > div::after {
            content: "" !important;
            position: absolute !important;
            top: 0 !important; left: 0 !important;
            width: 30% !important; height: 100% !important;
            background: linear-gradient(90deg,transparent 0%,rgba(255,255,255,.45) 50%,transparent 100%) !important;
            animation: shimmer 1.4s ease-in-out infinite !important;
        }
        .stProgress > div > div > p,
        [data-testid="stProgressBarMessage"] {
            font-size: 16px !important;
            color: rgba(148,163,184,.70) !important;
            -webkit-text-fill-color: rgba(148,163,184,.70) !important;
            font-weight: 500 !important;
            margin: 6px 0 0 !important;
        }

        /* ── Referral banner ─────────────────────────────── */
        .referral-banner {
            background: linear-gradient(135deg, rgba(124,58,237,.12) 0%, rgba(6,182,212,.08) 100%);
            border: 1.5px solid rgba(124,58,237,.25);
            border-radius: 22px;
            padding: 28px 32px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 24px;
            margin: 56px auto 0;
            max-width: 720px;
            flex-wrap: wrap;
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
        }
        .referral-left { flex: 1; min-width: 200px; }
        .referral-badge {
            display: inline-block;
            background: linear-gradient(135deg, #7C3AED, #06B6D4);
            color: #fff;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: .08em;
            padding: 4px 12px;
            border-radius: 999px;
            margin-bottom: 10px;
            text-transform: uppercase;
        }
        .referral-title {
            font-size: 22px !important;
            font-weight: 700 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 0 6px !important;
            letter-spacing: -.02em !important;
        }
        .referral-sub {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            margin: 0 !important;
            line-height: 1.6 !important;
        }
        .referral-cta {
            display: inline-block;
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%);
            color: #fff !important;
            -webkit-text-fill-color: #fff !important;
            font-size: 15px;
            font-weight: 600;
            padding: 12px 24px;
            border-radius: 16px;
            text-decoration: none !important;
            white-space: nowrap;
            box-shadow: 0 8px 24px rgba(124,58,237,.27);
            transition: transform .18s ease, box-shadow .18s ease;
        }
        .referral-cta:hover {
            transform: scale(1.03);
            box-shadow: 0 12px 32px rgba(124,58,237,.40);
        }

        /* ── Alerts ───────────────────────────────────────── */
        .stAlert { border-radius: 12px !important; font-size: 16px !important; }
        [data-testid="stAlert"] p {
            font-size: 16px !important;
            color: inherit !important;
            -webkit-text-fill-color: inherit !important;
            line-height: 1.6 !important;
            margin: 0 !important;
        }

        /* ── Custom error / warn boxes ────────────────────── */
        .error-box {
            background: rgba(239,68,68,.10) !important;
            border: 1.5px solid rgba(239,68,68,.25) !important;
            border-radius: 12px !important;
            padding: 16px 20px !important;
            font-size: 16px !important;
            color: #FCA5A5 !important;
            -webkit-text-fill-color: #FCA5A5 !important;
            line-height: 1.6 !important;
            margin: 8px 0 !important;
        }
        .error-box strong {
            color: #FECACA !important;
            -webkit-text-fill-color: #FECACA !important;
        }
        .warn-box {
            background: rgba(245,158,11,.10) !important;
            border: 1.5px solid rgba(245,158,11,.25) !important;
            border-radius: 12px !important;
            padding: 16px 20px !important;
            font-size: 16px !important;
            color: #FDE68A !important;
            -webkit-text-fill-color: #FDE68A !important;
            line-height: 1.6 !important;
            margin: 8px 0 !important;
        }

        /* ── Success box ──────────────────────────────────── */
        .success-box {
            background: rgba(16,185,129,.10) !important;
            border: 1.5px solid rgba(16,185,129,.25) !important;
            border-radius: 12px !important;
            padding: 16px 20px !important;
            font-size: 16px !important;
            color: #6EE7B7 !important;
            -webkit-text-fill-color: #6EE7B7 !important;
            line-height: 1.6 !important;
            margin: 0 0 16px !important;
        }
        .success-box strong {
            color: #A7F3D0 !important;
            -webkit-text-fill-color: #A7F3D0 !important;
        }

        /* ── Status box ───────────────────────────────────── */
        [data-testid="stStatusWidget"] { border-radius: 10px !important; }

        /* ── Download button ──────────────────────────────── */
        [data-testid="stDownloadButton"] > button {
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%) !important;
            border: none !important;
            border-radius: 12px !important;
            color: #fff !important;
            -webkit-text-fill-color: #fff !important;
            font-weight: 600 !important;
            font-size: 13.5px !important;
            padding: 14px 18px !important;
            box-shadow: 0 8px 20px rgba(124,58,237,.33) !important;
            width: 100% !important;
            transition: transform .18s ease, box-shadow .18s ease !important;
        }
        [data-testid="stDownloadButton"] > button:hover {
            transform: scale(1.02) !important;
            box-shadow: 0 12px 28px rgba(124,58,237,.45) !important;
        }

        /* ── Content sections — centered at 720px ───────── */
        .content-section {
            margin: 56px auto 0 !important;
            max-width: 720px !important;
            text-align: center !important;
        }
        /* ── Section pill badge ───────────────────────────── */
        .content-label, .pricing-label {
            display: inline-block !important;
            padding: 5px 16px !important;
            border-radius: 999px !important;
            border: 1px solid rgba(6,182,212,.30) !important;
            background: rgba(6,182,212,.07) !important;
            font-size: 11px !important;
            font-weight: 600 !important;
            letter-spacing: .10em !important;
            color: #67E8F9 !important;
            -webkit-text-fill-color: #67E8F9 !important;
            text-transform: uppercase !important;
            margin: 0 0 18px !important;
        }
        .content-h2 {
            font-size: 40px !important;
            font-weight: 800 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 0 20px !important;
            letter-spacing: -.035em !important;
            line-height: 1.1 !important;
            display: block !important;
            text-align: center !important;
        }
        .content-p {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            line-height: 1.75 !important;
            margin: 0 0 14px !important;
        }
        .feature-list {
            list-style: none !important;
            padding: 0 !important;
            margin: 0 auto !important;
            display: grid !important;
            grid-template-columns: 1fr 1fr !important;
            gap: 10px !important;
            max-width: 720px !important;
        }
        .feature-list li {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            display: flex !important;
            align-items: flex-start !important;
            gap: 8px !important;
            line-height: 1.6 !important;
        }
        .feature-list li::before {
            content: "✓" !important;
            color: #06B6D4 !important;
            -webkit-text-fill-color: #06B6D4 !important;
            font-weight: 700 !important;
            flex-shrink: 0 !important;
            margin-top: 1px !important;
        }
        .steps-grid {
            display: grid !important;
            grid-template-columns: 1fr 1fr 1fr !important;
            gap: 16px !important;
            margin: 20px auto 0 !important;
            max-width: 720px !important;
        }
        .step-card {
            background: rgba(15,23,42,.50) !important;
            border: 1px solid rgba(148,163,184,.12) !important;
            border-radius: 18px !important;
            padding: 20px 16px !important;
            backdrop-filter: blur(14px) !important;
            -webkit-backdrop-filter: blur(14px) !important;
            transition: border-color .22s, box-shadow .22s !important;
        }
        .step-card:hover {
            border-color: rgba(6,182,212,.35) !important;
            box-shadow: 0 8px 32px rgba(6,182,212,.10) !important;
        }
        .step-num {
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            width: 40px !important; height: 40px !important;
            background: linear-gradient(135deg, #7C3AED, #06B6D4) !important;
            border-radius: 50% !important;
            color: #fff !important;
            -webkit-text-fill-color: #fff !important;
            font-size: 17px !important;
            font-weight: 800 !important;
            margin-bottom: 14px !important;
            box-shadow: 0 4px 16px rgba(124,58,237,.35) !important;
        }
        .step-title {
            font-size: 16px !important;
            font-weight: 700 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 0 6px !important;
        }
        .step-desc {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            line-height: 1.6 !important;
            margin: 0 !important;
        }
        .use-cases {
            display: grid !important;
            grid-template-columns: 1fr 1fr !important;
            gap: 12px !important;
            margin: 16px auto 0 !important;
            max-width: 720px !important;
        }
        .use-case {
            background: rgba(15,23,42,.50) !important;
            border: 1px solid rgba(148,163,184,.12) !important;
            border-radius: 18px !important;
            padding: 20px !important;
            backdrop-filter: blur(14px) !important;
            -webkit-backdrop-filter: blur(14px) !important;
            transition: border-color .22s, box-shadow .22s !important;
            text-align: left !important;
        }
        .use-case:hover {
            border-color: rgba(6,182,212,.35) !important;
            box-shadow: 0 8px 32px rgba(6,182,212,.10) !important;
        }
        /* ── Icon tile (use-cases + trust-bar style) ─────── */
        .icon-tile {
            width: 44px; height: 44px;
            border-radius: 12px;
            background: linear-gradient(135deg, rgba(124,58,237,.22), rgba(6,182,212,.18));
            border: 1px solid rgba(148,163,184,.15);
            display: inline-flex; align-items: center; justify-content: center;
            margin-bottom: 14px;
            flex-shrink: 0;
        }
        .use-case-icon { margin-bottom: 0 !important; }
        .use-case-title {
            font-size: 16px !important;
            font-weight: 700 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 0 5px !important;
        }
        .use-case-desc {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            line-height: 1.6 !important;
            margin: 0 !important;
        }
        .divider-line {
            border: none;
            border-top: 1px solid rgba(148,163,184,.10);
            margin: 48px auto;
            max-width: 720px;
        }

        /* ── Video preview card ──────────────────────────── */
        .preview-card {
            background: rgba(15,23,42,.60) !important;
            border: 1px solid rgba(6,182,212,.25) !important;
            border-radius: 18px !important;
            padding: 16px !important;
            display: grid !important;
            grid-template-columns: 100px 1fr !important;
            grid-template-rows: auto auto auto !important;
            column-gap: 16px !important;
            row-gap: 3px !important;
            align-items: center !important;
            margin: 16px 0 !important;
            backdrop-filter: blur(14px) !important;
            -webkit-backdrop-filter: blur(14px) !important;
            box-shadow: 0 0 28px rgba(6,182,212,.15) !important;
        }
        .preview-thumb {
            grid-column: 1 !important; grid-row: 1 / 4 !important;
            align-self: center !important;
            width: 100px !important; height: 64px !important;
            object-fit: cover !important;
            border-radius: 10px !important;
            background: rgba(124,58,237,.20) !important;
            display: block !important;
        }
        .preview-thumb-placeholder {
            grid-column: 1 !important; grid-row: 1 / 4 !important;
            align-self: center !important;
            width: 100px !important; height: 64px !important;
            border-radius: 10px !important;
            background: linear-gradient(135deg, rgba(124,58,237,.25), rgba(6,182,212,.20)) !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            font-size: 24px !important;
        }
        .preview-status {
            grid-column: 2 !important; grid-row: 1 !important;
            font-size: 12px !important; font-weight: 700 !important;
            letter-spacing: .07em !important;
            color: #6EE7B7 !important; -webkit-text-fill-color: #6EE7B7 !important;
            margin: 0 !important; text-transform: uppercase !important;
        }
        .preview-title {
            grid-column: 2 !important; grid-row: 2 !important;
            font-size: 16px !important; font-weight: 600 !important;
            color: #F1F5F9 !important; -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 !important;
            white-space: nowrap !important; overflow: hidden !important;
            text-overflow: ellipsis !important;
        }
        .preview-hint {
            grid-column: 2 !important; grid-row: 3 !important;
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            margin: 0 !important;
        }

        /* ── Success card ─────────────────────────────────── */
        .success-card {
            background: linear-gradient(135deg, rgba(6,182,212,.10) 0%, rgba(124,58,237,.10) 100%) !important;
            border: 1px solid rgba(6,182,212,.25) !important;
            border-radius: 22px !important;
            padding: 32px 24px !important;
            text-align: center !important;
            margin: 8px 0 4px !important;
            box-shadow: 0 0 28px rgba(6,182,212,.20) !important;
            backdrop-filter: blur(14px) !important;
            -webkit-backdrop-filter: blur(14px) !important;
        }
        .success-icon {
            font-size: 44px !important;
            display: block !important;
            margin-bottom: 12px !important;
            line-height: 1 !important;
        }
        .success-title {
            font-size: 24px !important;
            font-weight: 700 !important;
            color: #F1F5F9 !important;
            -webkit-text-fill-color: #F1F5F9 !important;
            margin: 0 0 6px !important;
            letter-spacing: -.02em !important;
        }
        .success-sub {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            -webkit-text-fill-color: rgba(203,213,225,.78) !important;
            margin: 0 !important;
            line-height: 1.6 !important;
        }

        /* ── Plan buttons inside pricing HTML ─────────────── */
        .plan-btn {
            display: block !important;
            background: rgba(148,163,184,.10) !important;
            border: 1.5px solid rgba(148,163,184,.18) !important;
            border-radius: 12px !important;
            color: #CBD5E1 !important;
            -webkit-text-fill-color: #CBD5E1 !important;
            font-size: 13.5px !important;
            font-weight: 600 !important;
            padding: 13px 20px !important;
            text-align: center !important;
            text-decoration: none !important;
            margin-top: 16px !important;
            transition: border-color .15s, background .15s !important;
        }
        .plan-btn:hover {
            border-color: rgba(6,182,212,.45) !important;
            color: #67E8F9 !important;
            -webkit-text-fill-color: #67E8F9 !important;
            background: rgba(6,182,212,.08) !important;
            text-decoration: none !important;
        }
        .plan-btn-primary {
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%) !important;
            border-color: transparent !important;
            color: #fff !important;
            -webkit-text-fill-color: #fff !important;
            box-shadow: 0 8px 20px rgba(124,58,237,.33) !important;
        }
        .plan-btn-primary:hover {
            transform: scale(1.02) !important;
            color: #fff !important;
            -webkit-text-fill-color: #fff !important;
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%) !important;
        }
        .pricing-card { display: flex !important; flex-direction: column !important; }
        .plan-features { flex: 1 !important; }

        /* Hide st.status widget entirely */
        [data-testid="stStatusWidget"],
        [data-testid="stStatus"] { display: none !important; }

        /* ── FAQ ──────────────────────────────────────────── */
        [data-testid="stExpander"] {
            background: rgba(15,23,42,.50) !important;
            border: 1px solid rgba(148,163,184,.12) !important;
            border-radius: 12px !important;
            margin-bottom: 8px !important;
            box-shadow: none !important;
            backdrop-filter: blur(14px) !important;
            -webkit-backdrop-filter: blur(14px) !important;
        }
        [data-testid="stExpander"] summary {
            font-size: 16px !important;
            font-weight: 600 !important;
            color: #F1F5F9 !important;
        }
        [data-testid="stExpanderDetails"] p {
            font-size: 16px !important;
            color: rgba(203,213,225,.78) !important;
            line-height: 1.7 !important;
        }

        /* ── Footer ───────────────────────────────────────── */
        .footer {
            text-align: center;
            padding: 48px 0 0;
            font-size: 13.5px;
            color: rgba(148,163,184,.70);
        }
        .footer a { color: rgba(148,163,184,.70); text-decoration: none; }
        .footer a:hover { color: #67E8F9; }

        /* ── Divider ──────────────────────────────────────── */
        hr {
            border-color: rgba(148,163,184,.10) !important;
            margin: 32px auto !important;
            max-width: 720px !important;
        }

        /* ── Pricing cards — constrain to 720px ──────────── */
        [data-testid="stHorizontalBlock"] {
            max-width: 720px !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }

        /* ── FAQ expanders — constrain to 720px ──────────── */
        [data-testid="stExpander"] {
            max-width: 720px !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Pricing section
# ---------------------------------------------------------------------------

def _render_pricing() -> None:
    # Auto-scroll here when triggered after a successful download
    if st.session_state.get("_scroll_pricing"):
        st.session_state["_scroll_pricing"] = False
        _components.html(
            "<script>window.parent.scrollTo({top: window.parent.document.body.scrollHeight, behavior: 'smooth'});</script>",
            height=0,
        )

    st.markdown(
        """
        <div class="pricing-header" id="pricing">
            <p class="pricing-label">PRICING</p>
            <h2 class="pricing-title">Top up your credits</h2>
            <p class="pricing-sub">One-time · Credits never expire · Stripe checkout</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Build Stripe URLs — pre-fill email + pass it as client_reference_id so the
    # webhook can credit the right account without the user typing it twice.
    email = st.session_state.get("email", "")
    if email and not STRIPE_BASIC_URL.endswith("REPLACE_BASIC") and not STRIPE_PRO_URL.endswith("REPLACE_PRO"):
        _params = urlencode({"client_reference_id": email, "prefilled_email": email})
        _basic_url = f"{STRIPE_BASIC_URL}?{_params}"
        _pro_url   = f"{STRIPE_PRO_URL}?{_params}"
    else:
        _basic_url = STRIPE_BASIC_URL
        _pro_url   = STRIPE_PRO_URL

    col1, col2, col3 = st.columns(3, gap="small")

    with col1:
        st.markdown(
            f"""
            <div class="pricing-card">
                <p class="plan-name">Starter</p>
                <p class="plan-price">$4.99</p>
                <p class="plan-credits">10 credits</p>
                <p class="plan-unit">$0.50 / download</p>
                <ul class="plan-features">
                    <li>10 MP3 downloads</li>
                    <li>190 kbps quality</li>
                    <li>cap.so &amp; cap.link</li>
                </ul>
                <a href="{_basic_url}" target="_blank" rel="noopener" class="plan-btn">Get Starter →</a>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col2:
        st.markdown(
            f"""
            <div class="pricing-card pricing-card--featured">
                <span class="plan-badge">BEST VALUE</span>
                <p class="plan-name">Pro</p>
                <p class="plan-price">$9.99</p>
                <p class="plan-credits">30 credits</p>
                <p class="plan-unit">$0.33 / download</p>
                <ul class="plan-features">
                    <li>30 MP3 downloads</li>
                    <li>190 kbps quality</li>
                    <li>cap.so &amp; cap.link</li>
                    <li>Priority processing</li>
                </ul>
                <a href="{_pro_url}" target="_blank" rel="noopener" class="plan-btn plan-btn-primary">Get Pro →</a>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col3:
        st.markdown(
            f"""
            <div class="pricing-card">
                <p class="plan-name">Teams</p>
                <p class="plan-price" style="font-size:22px;padding-top:6px;line-height:1.2;">Custom</p>
                <p class="plan-credits">&nbsp;</p>
                <p class="plan-unit">For teams &amp; power users</p>
                <ul class="plan-features">
                    <li>Unlimited downloads</li>
                    <li>API access</li>
                    <li>Dedicated support</li>
                    <li>SLA</li>
                </ul>
                <a href="mailto:{CONTACT_EMAIL}?subject=CapMP3%20Teams" class="plan-btn">Contact us →</a>
            </div>
            """,
            unsafe_allow_html=True,
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
                    <div class="use-case-icon">
                        <div class="icon-tile">
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#67E8F9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
                        </div>
                    </div>
                    <p class="use-case-title">Transcription</p>
                    <p class="use-case-desc">Feed the MP3 into Whisper, Otter.ai, Descript, or Notion AI for automatic transcription of meetings, demos, and walkthroughs.</p>
                </div>
                <div class="use-case">
                    <div class="use-case-icon">
                        <div class="icon-tile">
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#67E8F9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/><path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>
                        </div>
                    </div>
                    <p class="use-case-title">Offline listening</p>
                    <p class="use-case-desc">Play back meeting recaps and briefings without an internet connection or the cap.so app — on any device that supports MP3.</p>
                </div>
                <div class="use-case">
                    <div class="use-case-icon">
                        <div class="icon-tile">
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#67E8F9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="2" y1="7" x2="7" y2="7"/><line x1="2" y1="17" x2="7" y2="17"/><line x1="17" y1="17" x2="22" y2="17"/><line x1="17" y1="7" x2="22" y2="7"/></svg>
                        </div>
                    </div>
                    <p class="use-case-title">Podcast &amp; video production</p>
                    <p class="use-case-desc">Use interview or presentation audio as raw material in your podcast editor, Premiere Pro, or DaVinci Resolve.</p>
                </div>
                <div class="use-case">
                    <div class="use-case-icon">
                        <div class="icon-tile">
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#67E8F9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        </div>
                    </div>
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

class _ScaledBar:
    """Maps bar.progress(0–1) into a sub-range of the real Streamlit progress bar."""

    def __init__(self, bar, lo: float, hi: float) -> None:
        self._bar = bar
        self._lo  = lo
        self._hi  = hi

    def progress(self, value: float, text: str = "") -> None:
        scaled  = self._lo + min(max(value, 0.0), 1.0) * (self._hi - self._lo)
        overall = int(scaled * 100)
        label   = f"{text}  ·  {overall}%" if text else f"{overall}%"
        self._bar.progress(min(scaled, self._hi), label)


def _run_conversion(url: str) -> None:
    """Execute the full download + convert pipeline and render result."""

    if _is_rate_limited():
        st.markdown(
            f'<div class="warn-box">Too many requests — please wait a moment and try again.</div>',
            unsafe_allow_html=True,
        )
        return

    # Re-verify credit balance from DB before every conversion.
    # Prevents session-state manipulation via browser dev tools.
    email = st.session_state.get("email", "")
    if email:
        db_credits = _load_credits_from_supabase(email)
        if db_credits is not None:
            st.session_state.credits = db_credits

    if _credits() <= 0:
        st.markdown(
            '<div class="warn-box">No credits remaining. Purchase a plan below to continue.</div>',
            unsafe_allow_html=True,
        )
        return

    # Deduct credit BEFORE conversion starts so the server can't be abused
    # by cancelling mid-flight. On any failure we call _refund_credit().
    _deduct_credit()

    with tempfile.TemporaryDirectory() as tmpdir:
        src_path    = os.path.join(tmpdir, "source")
        audio_path  = os.path.join(tmpdir, "audio.mp3")
        audio_bytes = None

        try:
            bar = st.progress(0, text="Finding audio source…  0%")

            video_url, _method, audio_only = find_video_url(url)
            bar.progress(0.04, text="Downloading…  4%")

            download_to_file(
                video_url, src_path,
                _ScaledBar(bar, 0.04, 0.72),
                "Downloading" if audio_only else "Downloading",
            )

            convert_to_mp3(
                src_path, audio_path,
                _ScaledBar(bar, 0.72, 1.00),
                "Converting to MP3",
            )

            bar.progress(1.0, text="✓ Complete  100%")

            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

        except requests.HTTPError as e:
            # Log full detail server-side; show generic message to user
            print(f"[CAPMP3 ERROR] HTTPError for {url!r}: {e}", flush=True)
            _refund_credit()
            st.markdown(
                '<div class="error-box"><strong>Connection error.</strong> '
                "Could not retrieve the recording. Check that the URL is correct "
                "and the recording is publicly accessible.</div>",
                unsafe_allow_html=True,
            )
            return
        except ValueError as e:
            # ValueError messages are written by us — safe to surface
            _refund_credit()
            st.markdown(
                f'<div class="error-box">{html_lib.escape(str(e))}</div>',
                unsafe_allow_html=True,
            )
            return
        except RuntimeError as e:
            # May contain ffmpeg paths / internal details — log only
            print(f"[CAPMP3 ERROR] RuntimeError for {url!r}: {e}", flush=True)
            _refund_credit()
            st.markdown(
                '<div class="error-box"><strong>Conversion failed.</strong> '
                "An error occurred during processing. Please try again or contact support.</div>",
                unsafe_allow_html=True,
            )
            return
        except Exception as e:
            print(f"[CAPMP3 ERROR] Unexpected error for {url!r}: {type(e).__name__}: {e}", flush=True)
            _refund_credit()
            st.markdown(
                '<div class="error-box"><strong>Unexpected error.</strong> '
                "Something went wrong. Please try again or contact support.</div>",
                unsafe_allow_html=True,
            )
            return

    if audio_bytes:
        st.markdown(
            """
            <div class="success-card">
                <span class="success-icon">🎉</span>
                <p class="success-title">Your MP3 is ready!</p>
                <p class="success-sub">Click the button below to save. File is deleted from our servers immediately after download.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.download_button(
            label="⬇ Save MP3",
            data=audio_bytes,
            file_name="cap_audio.mp3",
            mime="audio/mpeg",
            use_container_width=True,
        )
        # Signal to scroll to pricing on next rerun (when credits hit 0)
        if _credits() <= 0:
            st.session_state["_scroll_pricing"] = True
        del audio_bytes
        gc.collect()


# ---------------------------------------------------------------------------
# Terms of Service page
# ---------------------------------------------------------------------------

def _render_terms() -> None:
    st.markdown(
        """
        <style>
        .tos-h1  {
            font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
            font-size:30px; font-weight:700; color:#F1F5F9;
            -webkit-text-fill-color:#F1F5F9;
            margin:0 0 4px; letter-spacing:-.025em;
        }
        .tos-updated { font-size:16px; color:rgba(148,163,184,.70); -webkit-text-fill-color:rgba(148,163,184,.70); margin:0 0 20px; }
        .tos-disclaimer {
            background:rgba(245,158,11,.08); border:1.5px solid rgba(245,158,11,.25);
            border-radius:12px; padding:16px 20px; margin-bottom:16px;
        }
        .tos-disclaimer strong { color:#FDE68A; -webkit-text-fill-color:#FDE68A; }
        .tos-disclaimer p { font-size:16px; color:rgba(253,230,138,.85); -webkit-text-fill-color:rgba(253,230,138,.85); margin:4px 0 0; line-height:1.6; }
        .tos-h2  {
            font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
            font-size:18px; font-weight:700; color:#F1F5F9;
            -webkit-text-fill-color:#F1F5F9;
            margin:24px 0 6px; letter-spacing:-.01em;
        }
        .tos-p   { font-size:16px; color:rgba(203,213,225,.78); -webkit-text-fill-color:rgba(203,213,225,.78); line-height:1.7; margin:0 0 8px; }
        .tos-p a { color:#67E8F9 !important; -webkit-text-fill-color:#67E8F9 !important; }
        .tos-p strong { color:#F1F5F9; -webkit-text-fill-color:#F1F5F9; }
        .tos-ul  { padding-left:20px; margin:0 0 8px; }
        .tos-ul li { font-size:16px; color:rgba(203,213,225,.78); -webkit-text-fill-color:rgba(203,213,225,.78); line-height:1.7; margin-bottom:3px; }
        .tos-ul li strong { color:#F1F5F9; -webkit-text-fill-color:#F1F5F9; }
        .tos-ul a { color:#67E8F9 !important; -webkit-text-fill-color:#67E8F9 !important; }
        .tos-risk {
            background:rgba(239,68,68,.08); border:1px solid rgba(239,68,68,.22);
            border-radius:10px; padding:14px 18px; margin:10px 0;
            font-size:16px; color:#FCA5A5; -webkit-text-fill-color:#FCA5A5;
            line-height:1.65;
        }
        .tos-risk strong { color:#FECACA; -webkit-text-fill-color:#FECACA; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="logo" style="margin-bottom:32px;">
            <svg width="34" height="34" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" style="flex-shrink:0">
                <defs>
                    <linearGradient id="cap-grad-tos" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
                        <stop offset="0" stop-color="#7C3AED"/>
                        <stop offset="1" stop-color="#06B6D4"/>
                    </linearGradient>
                </defs>
                <path d="M44 14a22 22 0 1 0 0 36" stroke="url(#cap-grad-tos)" stroke-width="6" stroke-linecap="round" fill="none"/>
                <rect x="42" y="26" width="5" height="12" rx="2.5" fill="url(#cap-grad-tos)"/>
                <rect x="50" y="20" width="5" height="24" rx="2.5" fill="url(#cap-grad-tos)"/>
                <rect x="58" y="28" width="5" height="8"  rx="2.5" fill="url(#cap-grad-tos)"/>
            </svg>
            <span class="logo-text">cap<span class="logo-mp3">mp3</span></span>
        </div>
        <h1 class="tos-h1">Terms of Service</h1>
        <p class="tos-updated">Last updated: April 24, 2026</p>

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
        screen recordings to MP3 audio files. The Service is operated by Tozame s.r.o. (IČO: 11726938),
        hereinafter referred to as "CapMP3", "we", "us", or "our". By accessing or using the Service,
        you agree to be bound by these Terms of Service ("Terms"). If you do not agree, do not use the Service.</p>

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
        <p class="tos-p">You must be at least <strong>18 years of age</strong> to use the Service,
        particularly to make any purchase. By using the Service, you represent and warrant that you
        meet this requirement and are legally capable of entering into a binding contract.</p>

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

        <h2 class="tos-h2">6. Credits, Payments & Refund Policy</h2>
        <p class="tos-p"><strong>Free credit:</strong> One (1) free conversion credit is provided upon email
        registration. This credit carries no monetary value and cannot be transferred or exchanged for cash.</p>

        <p class="tos-p"><strong>Paid credits:</strong> Additional credits are available for purchase via Stripe.
        All prices are listed in USD and are inclusive of any applicable taxes unless stated otherwise.
        By completing a purchase, you also agree to
        <a href="https://stripe.com/legal/ssa" target="_blank" rel="noopener">Stripe's Terms of Service</a>.</p>

        <p class="tos-p"><strong>Delivery of digital goods:</strong> Credits are a digital product.
        Upon successful payment confirmation by Stripe, credits are credited to your account
        <strong>immediately and automatically</strong>. By initiating a purchase, you explicitly request
        immediate access to the digital content and acknowledge that the service begins at the moment
        of payment confirmation.</p>

        <div class="tos-risk">
            <strong>No Refund Policy</strong><br>
            All credit purchases are <strong>final and non-refundable</strong>. Because credits represent
            access to a digital service that is made available immediately upon payment and is consumed
            on demand, no right of withdrawal or refund applies after the payment is confirmed.<br><br>
            The sole exception: if a technical error verifiably attributable to CapMP3 causes a credit
            to be deducted without a successful conversion, that credit will be restored to your account.
            To report such an issue, contact info@capmp3.com within 14 days of the failed transaction,
            including your registered email address and the approximate date and time of the attempt.
        </div>

        <p class="tos-p" style="margin-top:12px;"><strong>Credit expiry:</strong> Purchased credits do not expire.</p>

        <p class="tos-p"><strong>Payment disputes (chargebacks):</strong> We take payment disputes seriously.
        If you believe an unauthorised charge has occurred, contact us at info@capmp3.com before
        initiating a chargeback with your card issuer. Fraudulent or unjustified chargebacks may result
        in permanent suspension of your account.</p>

        <h2 class="tos-h2">7. Privacy & Data Processing</h2>
        <p class="tos-p">We collect and process the following personal data:</p>
        <ul class="tos-ul">
            <li><strong>Email address</strong> — collected voluntarily at registration, used solely
            to manage your credit balance and send occasional product updates. You may opt out at
            any time by contacting us.</li>
            <li><strong>IP address</strong> — processed ephemerally for rate limiting and abuse
            prevention. Not stored beyond the current session window.</li>
            <li><strong>Payment data</strong> — processed exclusively by Stripe. CapMP3 does not
            store or have access to your card number or other payment credentials.</li>
        </ul>
        <p class="tos-p">We do not collect, store, or process the content of any recording submitted
        to the Service. We do not sell, rent, or share personal data with third parties, except
        as necessary to process payments (Stripe) or as required by law.</p>
        <p class="tos-p"><strong>EU/EEA users (GDPR):</strong> You have the right to access, rectify,
        erase, and port your personal data, and to withdraw consent at any time. To exercise these
        rights, contact us at info@capmp3.com. You also have the right to lodge a complaint
        with your local supervisory authority (in the Czech Republic: Úřad pro ochranu osobních
        údajů, uoou.cz).</p>

        <h2 class="tos-h2">8. Disclaimer of Warranties</h2>
        <p class="tos-p">TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, THE SERVICE IS PROVIDED
        "AS IS" AND "AS AVAILABLE" WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
        BUT NOT LIMITED TO WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE,
        OR NON-INFRINGEMENT. WE DO NOT WARRANT THAT THE SERVICE WILL BE UNINTERRUPTED, ERROR-FREE,
        OR THAT ANY SPECIFIC RECORDING WILL BE PROCESSABLE.</p>

        <h2 class="tos-h2">9. Limitation of Liability</h2>
        <p class="tos-p">TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, CAPMP3 AND ITS OPERATOR
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
            <li><strong>Stripe, Inc.</strong> — for payment processing. CapMP3 does not store
            payment card data. Stripe processes payments in accordance with PCI-DSS standards.
            See <a href="https://stripe.com/privacy" target="_blank" rel="noopener">Stripe's Privacy Policy</a>
            and <a href="https://stripe.com/legal/ssa" target="_blank" rel="noopener">Terms of Service</a>.</li>
            <li><strong>Supabase</strong> — for secure storage of email addresses and credit balances.</li>
        </ul>

        <h2 class="tos-h2">12. Anti-Spam Policy</h2>
        <p class="tos-p">We commit to the following:</p>
        <ul class="tos-ul">
            <li>CapMP3 will never be promoted through unsolicited messages of any kind</li>
            <li>We will not send bulk emails without prior, explicit user consent</li>
            <li>All user email communications are strictly opt-in and permission-based</li>
            <li>We will not share or sell email addresses for any marketing purpose</li>
        </ul>
        <p class="tos-p">If you receive an unsolicited message appearing to promote CapMP3, please
        contact info@capmp3.com immediately.</p>

        <h2 class="tos-h2">13. Changes to Terms</h2>
        <p class="tos-p">We reserve the right to modify these Terms at any time. Material changes
        will be communicated via the Service. Continued use of the Service after changes are posted
        constitutes acceptance of the revised Terms.</p>

        <h2 class="tos-h2">14. Governing Law & Dispute Resolution</h2>
        <p class="tos-p">These Terms shall be governed by and construed in accordance with the laws
        of the Czech Republic, without regard to its conflict of law provisions. Any disputes arising
        under these Terms shall be subject to the exclusive jurisdiction of the competent courts of
        the Czech Republic.</p>
        <p class="tos-p">For users in the European Union, mandatory consumer protection provisions
        of your country of residence apply in addition to the above, and nothing in these Terms
        limits rights you have under EU consumer law. EU users may also use the European Commission's
        Online Dispute Resolution platform:
        <a href="https://ec.europa.eu/consumers/odr" target="_blank" rel="noopener">ec.europa.eu/consumers/odr</a>.</p>

        <h2 class="tos-h2">15. Contact & Operator Information</h2>
        <p class="tos-p">For questions, complaints, refund requests, or data-related inquiries:</p>
        <p class="tos-p">
            <strong>Tozame s.r.o.</strong><br>
            Nové sady 988/2, 602 00 Brno, Czech Republic<br>
            IČO: 11726938<br>
            <strong>Email:</strong> info@capmp3.com
        </p>
        <p class="tos-p" style="color:rgba(148,163,184,.60); -webkit-text-fill-color:rgba(148,163,184,.60);">
            We aim to respond to all enquiries within 3 business days.
        </p>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div style="margin-top:48px; padding-top:24px;
                    border-top:1px solid rgba(148,163,184,.10);
                    text-align:center; font-size:13px;
                    color:rgba(148,163,184,.60);">
            capmp3.com &middot; <a href="/" style="color:rgba(148,163,184,.60); text-decoration:none;">← Back to converter</a>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="CapMP3 – Download cap.so & cap.link Recordings as MP3",
    page_icon="🎵",
    layout="wide",
)

_init_session()
_inject_css()
_inject_analytics()
_inject_seo_meta()
_inject_effects()

# ── Device ID cookie — injected on every page load via component iframe ───────
# The iframe runs on the same domain (capmp3.com), so document.cookie applies
# to capmp3.com and will be sent in the Cookie header on subsequent WS upgrades.
# Python reads it via st.context.headers → _get_cookie_device_id().
_components.html(
    """
    <script>
    (function () {
        try {
            var K = 'capmp3_did';
            var did = localStorage.getItem(K);
            if (!did || !/^[0-9a-f]{32}$/.test(did)) {
                var a = new Uint8Array(16);
                crypto.getRandomValues(a);
                did = Array.from(a, function(b){ return b.toString(16).padStart(2,'0'); }).join('');
                localStorage.setItem(K, did);
            }
            // 2-year cookie on root path — sent with every request to this domain
            var exp = new Date(Date.now() + 730 * 86400000).toUTCString();
            document.cookie = K + '=' + did + '; expires=' + exp + '; path=/; SameSite=Lax';
        } catch (e) {}
    })();
    </script>
    """,
    height=0,
)

# ── Persist email in cookie so it survives the Stripe redirect ────────────────
_em = st.session_state.get("email", "")
if _em:
    _components.html(
        f"""
        <script>
        (function() {{
            try {{
                var exp = new Date(Date.now() + 30 * 86400000).toUTCString();
                document.cookie = 'capmp3_em={html_lib.escape(_em)}; expires=' + exp + '; path=/; SameSite=Lax';
            }} catch (e) {{}}
        }})();
        </script>
        """,
        height=0,
    )

# ── Page routing ──────────────────────────────────────────────────────────────
if st.query_params.get("page") == "terms":
    _render_terms()
    st.stop()

# ── Payment success handler ───────────────────────────────────────────────────
if st.query_params.get("payment") == "success":
    # Email may be missing — session is fresh after Stripe redirect.
    # Fall back to the capmp3_em cookie stored before the user left for Stripe.
    email = st.session_state.get("email", "")
    if not email:
        try:
            cookie_header = st.context.headers.get("Cookie", "")
            for _part in cookie_header.split(";"):
                _k, _, _v = _part.strip().partition("=")
                if _k.strip() == "capmp3_em":
                    _candidate = _v.strip()
                    if "@" in _candidate and len(_candidate) <= 254:
                        email = _candidate
                    break
        except Exception:
            pass
    if email:
        fresh = _load_credits_from_supabase(email)
        if fresh is not None:
            st.session_state.email      = email
            st.session_state.registered = True
            st.session_state.credits    = fresh
    st.session_state["_payment_success"] = True
    st.query_params.clear()

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
        <svg width="34" height="34" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" style="flex-shrink:0">
            <defs>
                <linearGradient id="cap-grad" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
                    <stop offset="0" stop-color="#7C3AED"/>
                    <stop offset="1" stop-color="#06B6D4"/>
                </linearGradient>
            </defs>
            <path d="M44 14a22 22 0 1 0 0 36" stroke="url(#cap-grad)" stroke-width="6" stroke-linecap="round" fill="none"/>
            <rect x="42" y="26" width="5" height="12" rx="2.5" fill="url(#cap-grad)"/>
            <rect x="50" y="20" width="5" height="24" rx="2.5" fill="url(#cap-grad)"/>
            <rect x="58" y="28" width="5" height="8"  rx="2.5" fill="url(#cap-grad)"/>
        </svg>
        <span class="logo-text">cap<span class="logo-mp3">mp3</span></span>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Hero ──────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <div style="max-width:720px; margin:0 auto; text-align:center; padding:40px 0 8px;">
        <h1 class="hero-title">
            Convert cap.so &amp; cap.link<br>recordings to <span class="grad">MP3</span>
        </h1>
        <p class="hero-sub">
            Paste any cap.so or cap.link URL and download a clean MP3 in under 30 seconds.<br>Free to try — no install needed.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Payment success banner ────────────────────────────────────────────────────
if st.session_state.pop("_payment_success", False):
    credits = _credits()
    label   = "credit" if credits == 1 else "credits"
    st.markdown(
        f'<div class="success-box">'
        f'🎉 <strong>Payment successful!</strong> '
        f'Your account has been topped up. You now have <strong>{credits} {label}</strong> available.'
        f'</div>',
        unsafe_allow_html=True,
    )

# ── Converter ─────────────────────────────────────────────────────────────────
with st.form("converter_form", clear_on_submit=False):
    url_input = st.text_input(
        "url",
        placeholder="Paste a cap.so video URL",
        label_visibility="collapsed",
    )
    clicked = st.form_submit_button("Convert →", type="primary", use_container_width=False)

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
        <div class="trust-tile-item">
            <div class="trust-tile">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#06B6D4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M9.663 17h4.673M12 3v1m6.364 1.636-.707.707M21 12h-1M4 12H3m3.343-5.657-.707-.707m2.828 9.9a5 5 0 1 1 7.072 0l-.548.547A3.374 3.374 0 0 0 14 18.469V19a2 2 0 1 1-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>
                </svg>
            </div>
            <div class="trust-text">
                <span class="trust-label">No account needed</span>
                <span class="trust-sub">Free · no signup</span>
            </div>
        </div>
        <div class="trust-tile-item">
            <div class="trust-tile">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#06B6D4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
                </svg>
            </div>
            <div class="trust-text">
                <span class="trust-label">~30 second conversion</span>
                <span class="trust-sub">Fast · server-side</span>
            </div>
        </div>
        <div class="trust-tile-item">
            <div class="trust-tile">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#06B6D4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                    <polyline points="9 12 11 14 15 10"/>
                </svg>
            </div>
            <div class="trust-text">
                <span class="trust-label">Files auto-deleted</span>
                <span class="trust-sub">Privacy · no storage</span>
            </div>
        </div>
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
        # SSRF guard — validate domain before any server-side fetch
        try:
            _validate_user_url(url)
        except ValueError as _ve:
            st.markdown(
                f'<div class="error-box">{html_lib.escape(str(_ve))}</div>',
                unsafe_allow_html=True,
            )
            url = ""  # prevent further processing

    if url and url.startswith(("http://", "https://")):
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

    safe_title = html_lib.escape(title)

    if thumb:
        thumb_html = f'<img src="{html_lib.escape(thumb)}" class="preview-thumb" loading="lazy">'
    else:
        thumb_html = '<div class="preview-thumb-placeholder">🎵</div>'

    gate_hint = (
        ""
        if st.session_state.registered
        else '<p class="preview-hint">Enter your email below to start the free download ↓</p>'
    )

    # Flat HTML — no nested divs (Markdown parser strips/corrupts them)
    st.markdown(
        f'<div class="preview-card">'
        f'{thumb_html}'
        f'<div class="preview-status">✓ Recording found</div>'
        f'<p class="preview-title">{safe_title}</p>'
        f'{gate_hint}'
        f'</div>',
        unsafe_allow_html=True,
    )

# ── Inline email gate ─────────────────────────────────────────────────────────
if st.session_state.show_gate and not st.session_state.registered:
    with st.form("email_gate_form", clear_on_submit=True):
        st.markdown("""
            <p class="gate-title">Get your first MP3 free</p>
            <p class="gate-sub">
                Enter your email to unlock <strong class="gate-strong">1 free download</strong>.
                No password, no subscription.
            </p>
            """,
            unsafe_allow_html=True,
        )
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
            st.markdown(
                '<div class="error-box">Please enter a valid email address.</div>',
                unsafe_allow_html=True,
            )
            st.stop()

        # DNS check — reject domains that don't exist (e.g. khjbr@khtr.khe)
        if not _email_domain_is_real(clean_email):
            st.markdown(
                '<div class="error-box"><strong>Invalid email domain.</strong> '
                "The domain in your email address doesn't exist. "
                "Please use a real email address to claim your free download.</div>",
                unsafe_allow_html=True,
            )
            st.stop()

        db_credits = _load_credits_from_supabase(clean_email)

        if db_credits is not None:
            # Returning user — use persisted balance from DB
            assigned = db_credits
        elif _has_used_free_credit(clean_email):
            # Same email or same browser fingerprint already used free credit
            assigned = 0
        else:
            assigned = FREE_CREDITS

        st.session_state.email              = clean_email
        st.session_state.registered         = True
        st.session_state.credits            = assigned
        st.session_state.show_gate          = False
        # Only queue conversion if the user actually has credits.
        # Critical: prevents 0-credit users from slipping through the do_convert path.
        st.session_state.do_convert         = (assigned > 0)
        # Persist: upsert user row (preserves existing balance on conflict)
        _save_email_to_supabase(clean_email)
        # KEY: lock the fingerprint + cookie device ID immediately when the free
        # credit is reserved — even if the conversion later fails, the same device
        # cannot claim another free credit with a different email address.
        if assigned == FREE_CREDITS:
            _mark_free_credit_used(clean_email)
        st.rerun()

# ── Post-registration: auto-trigger conversion ────────────────────────────────
if st.session_state.do_convert and st.session_state.registered and st.session_state.pending_url:
    st.session_state.do_convert = False
    _run_conversion(st.session_state.pending_url)

# ── Out of credits warning ────────────────────────────────────────────────────
if st.session_state.registered and _credits() <= 0 and not st.session_state.do_convert:
    st.markdown(
        '<div class="warn-box" style="margin-top:12px;">'
        '⚠ <strong>Free credit already used.</strong> '
        'Each email address and device is limited to 1 free download. '
        'Top up below to continue.'
        '</div>',
        unsafe_allow_html=True,
    )

# ── Content & FAQ ─────────────────────────────────────────────────────────────
_render_content()

# ── Pricing ───────────────────────────────────────────────────────────────────
_render_pricing()

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
