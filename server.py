#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import threading
import smtplib
from email.message import EmailMessage
from urllib.parse import parse_qs
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server
from capjs_server import CapServer

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT.parent / "data"
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "neuorise.sqlite3"
SESSION_COOKIE = "neuorise_session"
SESSION_DAYS = 14
HTTP_TIMEOUT_SECONDS = 45
MIN_POLL_INTERVAL_SECONDS = 5
SUNO_BASE_URL = os.environ.get("SUNO_BASE_URL", "https://api.sunoapi.org/api/v1")
GEMINI_BASE_URL = "https://api.openai-proxy.org/google"
AUDIO_URL_PATH = "/audio"
DUMMY_CALLBACK_URL = "https://example.com/suno-callback"

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


class ProviderError(Exception):
    pass


def provider_ssl_context():
    cert_file = os.environ.get("SSL_CERT_FILE")
    if cert_file:
        return ssl.create_default_context(cafile=cert_file)
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()
SUNO_BASE_URL = os.environ.get("SUNO_BASE_URL", SUNO_BASE_URL)

CAPJS_SECRET_KEY = os.environ.get("CAPJS_SECRET_KEY") or secrets.token_urlsafe(32)
CAPJS_CHALLENGE_COUNT = int(os.environ.get("CAPJS_CHALLENGE_COUNT", "24"))
CAPJS_CHALLENGE_SIZE = int(os.environ.get("CAPJS_CHALLENGE_SIZE", "32"))
CAPJS_CHALLENGE_DIFFICULTY = int(os.environ.get("CAPJS_CHALLENGE_DIFFICULTY", "5"))
CAPJS_CHALLENGE_EXPIRY_MS = int(os.environ.get("CAPJS_CHALLENGE_EXPIRY_MS", "600000"))
CAPJS_TOKEN_EXPIRY_MS = int(os.environ.get("CAPJS_TOKEN_EXPIRY_MS", "300000"))

cap_server = CapServer(
    secret_key=CAPJS_SECRET_KEY,
    challenge_count=CAPJS_CHALLENGE_COUNT,
    challenge_size=CAPJS_CHALLENGE_SIZE,
    challenge_difficulty=CAPJS_CHALLENGE_DIFFICULTY,
    challenge_expiry_ms=CAPJS_CHALLENGE_EXPIRY_MS,
    token_expiry_ms=CAPJS_TOKEN_EXPIRY_MS,
)

MOOD_PROFILES = {
    "Anxious": {"tempo": 58, "key": "D major", "arc": "down-regulate from alertness to safety"},
    "Sad": {"tempo": 62, "key": "F major", "arc": "validate heaviness before opening toward warmth"},
    "Overstimulated": {"tempo": 52, "key": "C major", "arc": "reduce sensory density and restore spaciousness"},
    "Restless": {"tempo": 66, "key": "G major", "arc": "settle movement into steady focus"},
    "Numb": {"tempo": 60, "key": "A minor to C major", "arc": "reintroduce gentle feeling without intensity"},
    "Hopeful": {"tempo": 72, "key": "E major", "arc": "support optimism with grounded momentum"},
    "Calm": {"tempo": 64, "key": "B-flat major", "arc": "maintain ease with soft attentional anchors"},
}


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def generate_session_id():
    """Generate a random 8-character session ID using lowercase letters and numbers."""
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(chars) for _ in range(8))


def db():
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    with db() as connection:
        connection.executescript(
            """
                        CREATE TABLE IF NOT EXISTS users (
                            id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            email TEXT NOT NULL UNIQUE,
                            password_hash TEXT NOT NULL,
                            verified INTEGER NOT NULL DEFAULT 0,
                            verification_token TEXT,
                            verification_sent_at TEXT,
                            verified_at TEXT,
                            created_at TEXT NOT NULL
                        );

            CREATE TABLE IF NOT EXISTS auth_sessions (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS healing_sessions (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              intake_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tracks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL REFERENCES healing_sessions(id) ON DELETE CASCADE,
              version INTEGER NOT NULL,
              title TEXT NOT NULL,
              gemini_prompt TEXT NOT NULL,
              suno_prompt TEXT NOT NULL,
              suno_request_json TEXT NOT NULL,
              provider_task_id TEXT,
              provider_response_json TEXT,
              audio_url TEXT,
              audio_config_json TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL REFERENCES healing_sessions(id) ON DELETE CASCADE,
              track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
              rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
              feedback_text TEXT,
              skipped INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

                        CREATE TABLE IF NOT EXISTS folders (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            name TEXT NOT NULL,
                            color TEXT NOT NULL DEFAULT '#92d8c4',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

            CREATE INDEX IF NOT EXISTS idx_sessions_user ON healing_sessions(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tracks_session ON tracks(session_id, version);
            CREATE INDEX IF NOT EXISTS idx_feedback_session ON feedback(session_id, created_at DESC);
            """
        )
        ensure_column(connection, "tracks", "provider_task_id", "TEXT")
        ensure_column(connection, "tracks", "provider_response_json", "TEXT")
        ensure_column(connection, "healing_sessions", "favorite", "INTEGER DEFAULT 0")
        ensure_column(connection, "healing_sessions", "folder_id", "INTEGER REFERENCES folders(id)")
        ensure_column(connection, "users", "verified", "INTEGER DEFAULT 0")
        ensure_column(connection, "users", "verification_token", "TEXT")
        ensure_column(connection, "users", "verification_sent_at", "TEXT")
        ensure_column(connection, "users", "verified_at", "TEXT")
        ensure_column(connection, "users", "reset_token", "TEXT")
        ensure_column(connection, "users", "reset_token_expires_at", "TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tracks_provider_task ON tracks(provider_task_id)")


def ensure_column(connection, table, column, definition):
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password, stored):
    try:
        algorithm, salt, expected = stored.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    actual = hash_password(password, salt).split("$", 2)[2]
    return hmac.compare_digest(actual, expected)


def json_response(start_response, status, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    response_headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    if headers:
        response_headers.extend(headers)
    start_response(f"{status.value} {status.phrase}", response_headers)
    return [body]


def html_response(start_response, status, html, headers=None):
    body = html.encode("utf-8")
    response_headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    if headers:
        response_headers.extend(headers)
    start_response(f"{status.value} {status.phrase}", response_headers)
    return [body]


def redirect_response(start_response, location, headers=None):
    response_headers = [("Location", location)]
    if headers:
        response_headers.extend(headers)
    start_response(f"{HTTPStatus.FOUND.value} {HTTPStatus.FOUND.phrase}", response_headers)
    return [b""]


def read_json(environ):
    length = int(environ.get("CONTENT_LENGTH") or 0)
    if length == 0:
        return {}
    raw = environ["wsgi.input"].read(length)
    return json.loads(raw.decode("utf-8"))


def get_captcha_token(environ, payload):
    token = None
    if isinstance(payload, dict):
        token = (
            payload.get("captchaVerificationToken")
            or payload.get("captcha_token")
            or payload.get("captchaToken")
            or payload.get("cap-token")
        )
    if not token:
        token = environ.get("HTTP_X_CAPTCHA_VERIFICATION_TOKEN")
    return token


def validate_captcha(environ, payload):
    token = get_captcha_token(environ, payload)
    if not token:
        raise ValueError("Captcha verification token is required.")
    if not cap_server.validate(token):
        raise ValueError("Captcha verification failed.")


def get_cookie(environ, name):
    cookie = SimpleCookie(environ.get("HTTP_COOKIE", ""))
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def serialize_user(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "verified": bool(row["verified"] if "verified" in row.keys() else False),
    }


def current_user(environ):
    token = get_cookie(environ, SESSION_COOKIE)
    if not token:
        return None
    with db() as connection:
        row = connection.execute(
            """
            SELECT users.* FROM auth_sessions
            JOIN users ON users.id = auth_sessions.user_id
            WHERE auth_sessions.id = ? AND auth_sessions.expires_at > ?
            """,
            (token, iso_now()),
        ).fetchone()
    return row


def require_user(environ):
    user = current_user(environ)
    if not user:
        raise PermissionError("Please sign in to continue.")
    return user


def create_session_cookie(user_id):
    token = secrets.token_urlsafe(32)
    expires_at = utc_now() + timedelta(days=SESSION_DAYS)
    with db() as connection:
        connection.execute(
            "INSERT INTO auth_sessions (id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token, user_id, expires_at.isoformat(), iso_now()),
        )
    return (
        "Set-Cookie",
        f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 24 * 60 * 60}",
    )


def send_reset_password_email(to_email, reset_url, name=None):
    """Send password reset email with clickable link."""
    try:
        smtp_server = os.environ.get("SMTP_SERVER")
        smtp_port = int(os.environ.get("SMTP_PORT", "465"))
        smtp_account = os.environ.get("SMTP_ACCOUNT")
        smtp_password = os.environ.get("SMTP_PASSWORD")
        if not all([smtp_server, smtp_account, smtp_password]):
            return

        display_name = name or to_email.split("@")[0]
        msg = EmailMessage()
        msg["From"] = smtp_account
        msg["To"] = to_email
        msg["Subject"] = "Reset your Neuorise password"
        msg.set_content(
            f"Hello {display_name},\n\n"
            f"Click the link below to reset your password:\n\n{reset_url}\n\n"
            f"This link expires in 1 hour.\n\n"
            f"If you didn't request this, please ignore this email."
        )
        html = f"""<html><body style='font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #333;'>
          <p>Hello {display_name},</p>
          <p>Click the link below to reset your password:</p>
          <p><a href='{reset_url}' style='display: inline-block; padding: 10px 20px; background: #6366f1; color: white; text-decoration: none; border-radius: 4px;'>Reset Password</a></p>
          <p>Or copy this link: <code>{reset_url}</code></p>
          <p style='color: #999; font-size: 12px;'>This link expires in 1 hour.</p>
          <p style='color: #999; font-size: 12px;'>If you didn't request this, please ignore this email.</p>
        </body></html>"""
        msg.add_alternative(html, subtype="html")

        if '465' in str(smtp_port):
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10) as server:
                server.login(smtp_account, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(smtp_account, smtp_password)
                server.send_message(msg)
    except:
        pass

def send_verification_email(to_email, token, name=None):
    """Send an email with a verification link using SMTP settings from environment."""
    smtp_server = os.environ.get("SMTP_SERVER")
    smtp_port = int(os.environ.get("SMTP_PORT", "0") or 0)
    smtp_account = os.environ.get("SMTP_ACCOUNT")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if not smtp_server or not smtp_port or not smtp_account or not smtp_password:
        return

    site_url = os.environ.get("SITE_URL")
    if not site_url:
        port = os.environ.get("PORT", "5173")
        site_url = f"http://localhost:{port}"
    site_url = site_url.rstrip("/")
    verify_url = f"{site_url}/api/verify-email?token={token}"

    subject = "Verify your Neuorise email"
    friendly = name or "User"
    text = f"Hi {friendly},\n\nPlease verify your email address by clicking the link below:\n\n{verify_url}\n\nIf you did not sign up, you can ignore this email.\n\nThanks,\nNeuorise Team"
    html = f"""
    <html>
      <body>
        <p>Hi {friendly},</p>
        <p>Please verify your email address by clicking the link below:</p>
        <p><a href=\"{verify_url}\">Verify your email</a></p>
        <p>If you did not sign up, you can ignore this email.</p>
        <p>Thanks,<br />Neuorise Team</p>
      </body>
    </html>
    """

    msg = EmailMessage()
    msg["From"] = smtp_account
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10) as server:
                server.login(smtp_account, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
                server.ehlo()
                try:
                    server.starttls()
                except Exception:
                    pass
                server.login(smtp_account, smtp_password)
                server.send_message(msg)
    except:
        pass

def clear_session_cookie(environ):
    token = get_cookie(environ, SESSION_COOKIE)
    if token:
        with db() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE id = ?", (token,))
    return ("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")


def infer_physiology(intake):
    heart_rate = int(intake["heartRate"])
    breath_rate = int(intake["breathRate"])
    stress = int(intake["stress"])
    energy = int(intake["energy"])
    heart_state = "elevated heart rate" if heart_rate > 92 else "low heart rate" if heart_rate < 58 else "steady heart rate"
    breath_state = "quick breathing" if breath_rate > 18 else "slow breathing" if breath_rate < 10 else "balanced breathing"
    if stress >= 7 or heart_rate > 92 or breath_rate > 18:
        regulation_goal = "parasympathetic downshift"
    elif energy <= 3:
        regulation_goal = "gentle activation"
    else:
        regulation_goal = "stable emotional regulation"
    return {
        "heart_state": heart_state,
        "breath_state": breath_state,
        "regulation_goal": regulation_goal,
    }


def build_gemini_prompt(intake, feedback=None):
    profile = MOOD_PROFILES.get(intake["mood"], MOOD_PROFILES["Calm"])
    physiology = infer_physiology(intake)
    symptom_ratings = intake.get("symptomRatings") or {}
    rating_labels = [
        ("sadness", "Feelings of sadness"),
        ("noInterest", "Feeling no interest or pleasure in things"),
        ("deathThoughts", "Recurrent thoughts of death or suicide"),
        ("hopelessness", "Hopelessness about the future"),
        ("anxious", "Feeling anxious or fearful"),
        ("excessiveWorry", "Excessive worry about several different things"),
        ("panicAttacks", "Sudden attacks of panic with palpitations, shortness of breath, faintness or other frightening bodily sensations"),
        ("fatigue", "Fatigue or loss of energy"),
        ("concentration", "Diminished ability to think or concentrate"),
        ("difficultySleeping", "Difficulty falling asleep"),
        ("disturbedSleep", "Restless or disturbed sleep"),
        ("irritable", "Feeling easily irritated or annoyed"),
    ]
    symptom_lines = "\n".join(
        f"- {label}: {symptom_ratings.get(key, 'n/a')}/5"
        for key, label in rating_labels
    )
    feedback_line = (
        f'User feedback to adapt from previous track: rating={feedback["rating"]}; notes="{feedback.get("feedbackText") or "no written notes"}".'
        if feedback
        else "No previous feedback. Create the first therapeutic direction."
    )
    return f"""You are a music therapy prompt engineer for a wellness product.

Create a Suno-compatible music generation prompt using the user's current state.

User state:
- Mood: {intake["mood"]}
- Energy: {intake["energy"]}/10
- Stress: {intake["stress"]}/10
- Mental need: {intake["need"]}
- Preferred texture: {intake["texture"]}
- Sounds to avoid: {intake.get("avoid") or "none specified"}
- User instructions: {intake.get("instructions") or "none specified"}
- Mental symptom ratings:
{symptom_lines}
- Heart rate: {intake["heartRate"]} bpm ({physiology["heart_state"]})
- Respiratory rate: {intake["breathRate"]} breaths/min ({physiology["breath_state"]})
- Session target: {intake["duration"]} minutes
- Regulation goal: {physiology["regulation_goal"]}
- Emotional arc: {profile["arc"]}

{feedback_line}

Return:
1. A concise Suno prompt.
2. Tempo, key, arrangement, instrumentation, mix notes, and negative prompt.
3. A short therapeutic intention sentence.

Safety: keep language wellness-focused and avoid medical claims."""


def http_json(method, url, headers=None, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "Neuorise/1.0")
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS, context=provider_ssl_context()) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        detail = body[:1000] if body else error.reason
        raise ProviderError(f"Provider HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise ProviderError(f"Provider network error: {error.reason}") from error
    except Exception as error:
        raise ProviderError(f"Provider request failed: {error}") from error


def bearer_token(value):
    token = (value or "").strip()
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1].strip()
    return f"Bearer {token}"


def suno_callback_url():
    """Return callback URL for Suno API.
    
    Note: Even though we use polling instead of relying on callbacks,
    the Suno API requires this parameter in the request. We provide a
    dummy URL since we fetch task status via polling instead.
    """
    explicit = os.environ.get("SUNO_CALLBACK_URL")
    if explicit:
        return explicit.strip()
    return DUMMY_CALLBACK_URL


def extract_json_object(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def fallback_gemini_plan(intake, feedback=None):
    profile = MOOD_PROFILES.get(intake["mood"], MOOD_PROFILES["Calm"])
    physiology = infer_physiology(intake)
    stress = int(intake["stress"])
    breath_rate = int(intake["breathRate"])
    stress_offset = -6 if stress >= 8 else 4 if stress <= 3 else 0
    breath_offset = -5 if breath_rate > 18 else 3 if breath_rate < 10 else 0
    feedback_offset = -4 if feedback and feedback["rating"] == "down" else 2 if feedback and feedback["rating"] == "up" else 0
    tempo = max(46, min(78, profile["tempo"] + stress_offset + breath_offset + feedback_offset))
    density = "minimal, spacious, low-density" if stress >= 7 or intake["mood"] == "Overstimulated" else "softly layered"
    percussion = "muted heartbeat-like pulse below the mix" if intake["need"] == "Focus" else "no drums, only breath-like swells"
    adaptation = (
        f'Adaptation from feedback: {"reduce intensity and simplify the arrangement" if feedback["rating"] == "down" else "preserve the helpful mood and add subtle variation"}; user notes: {feedback.get("feedbackText") or "none"}.'
        if feedback
        else "Initial generation based on intake."
    )
    instructions = intake.get("instructions") or ""
    instruction_line = f'User instruction: {instructions}.' if instructions else ""
    symptom_ratings = intake.get("symptomRatings") or {}
    symptom_labels = [
        ("sadness", "Feelings of sadness"),
        ("noInterest", "Feeling no interest or pleasure in things"),
        ("deathThoughts", "Recurrent thoughts of death or suicide"),
        ("hopelessness", "Hopelessness about the future"),
        ("anxious", "Feeling anxious or fearful"),
        ("excessiveWorry", "Excessive worry about several different things"),
        ("panicAttacks", "Sudden attacks of panic with palpitations, shortness of breath, faintness or other frightening bodily sensations"),
        ("fatigue", "Fatigue or loss of energy"),
        ("concentration", "Diminished ability to think or concentrate"),
        ("difficultySleeping", "Difficulty falling asleep"),
        ("disturbedSleep", "Restless or disturbed sleep"),
        ("irritable", "Feeling easily irritated or annoyed"),
    ]
    symptom_lines = "\n".join(
        f"- {label}: {symptom_ratings.get(key, 'n/a')}/5"
        for key, label in symptom_labels
    )
    prompt = f"""Therapeutic instrumental ambient music for {intake["need"].lower()} while the listener feels {intake["mood"].lower()}.
Mood arc: {profile["arc"]}.
Tempo: {tempo} BPM. Key: {profile["key"]}. Length target: {intake["duration"]} minutes.
{instruction_line}
Mental symptom ratings:
{symptom_lines}
Arrangement: slow opening pad, gentle motif after 30 seconds, warm harmonic bed, gradual soft resolution.
Instrumentation: {intake["texture"]}, sub-bass warmth, distant organic room tone, {percussion}.
Mix: soft transients, no sudden drops, wide stereo, low high-frequency glare, volume-safe mastering.
Therapeutic intention: Support {physiology["regulation_goal"]}. {adaptation}"""
    style = f"therapeutic ambient, {density}, {intake['texture'].lower()}, {profile['key']}, {tempo} BPM"
    negative_tags = intake.get("avoid") or "harsh leads, aggressive drums, sudden risers, distorted vocals, alarm-like tones"
    return {
        "title": f'{intake["need"]} for {intake["mood"]}',
        "prompt": prompt,
        "style": style,
        "negativeTags": negative_tags,
        "instrumental": True,
        "model": os.environ.get("SUNO_MODEL", "V4_5ALL"),
        "therapeuticIntention": f'Support {physiology["regulation_goal"]} while helping the listener move through "{profile["arc"]}".',
        "raw": json.dumps(
            {
                "title": f'{intake["need"]} for {intake["mood"]}',
                "prompt": prompt,
                "style": style,
                "negativeTags": negative_tags,
                "therapeuticIntention": f'Support {physiology["regulation_goal"]} while helping the listener move through "{profile["arc"]}".',
            },
            indent=2,
        ),
    }


def call_gemini_api(intake, feedback=None):
    api_key = os.environ.get("GEMINI_APIKEY")
    if not api_key:
        raise ProviderError("GEMINI_APIKEY is missing from the server environment.")

    user_prompt = build_gemini_prompt(intake, feedback)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                    "You generate production-ready music generation parameters. "
                    "Return only valid JSON with keys: title, prompt, style, negativeTags, "
                    "instrumental, model, therapeuticIntention. The prompt must be suitable "
                            "for Suno instrumental therapeutic music and must avoid medical claims.\n\n"
                            f"{user_prompt}"
                        )
                    }
                ],
            },
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1400,
            "responseMimeType": "application/json",
        },
    }
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    result = http_json(
        "POST",
        f"{GEMINI_BASE_URL}/v1beta/models/{model}:generateContent?{urlencode({'key': api_key})}",
        {
            "Content-Type": "application/json",
        },
        payload,
    )
    try:
        parts = result["candidates"][0]["content"]["parts"]
        content = "".join(part.get("text", "") for part in parts)
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderError("Gemini response did not include generated content.") from error

    try:
        plan = extract_json_object(content)
    except json.JSONDecodeError:
        plan = fallback_gemini_plan(intake, feedback)
        plan["raw"] = content
        return plan

    fallback = fallback_gemini_plan(intake, feedback)
    return {
        "title": str(plan.get("title") or fallback["title"])[:100],
        "prompt": str(plan.get("prompt") or fallback["prompt"])[:5000],
        "style": str(plan.get("style") or fallback["style"])[:1000],
        "negativeTags": str(plan.get("negativeTags") or fallback["negativeTags"])[:1000],
        "instrumental": bool(plan.get("instrumental", True)),
        "model": str(plan.get("model") or os.environ.get("SUNO_MODEL", "V4_5ALL")),
        "therapeuticIntention": str(plan.get("therapeuticIntention") or fallback["therapeuticIntention"]),
        "raw": json.dumps(plan, indent=2, ensure_ascii=False),
    }


def suno_prompt_summary(plan):
    return f"""Title: {plan["title"]}
Style: {plan["style"]}
Instrumental: {"yes" if plan.get("instrumental", True) else "no"}
Model: {plan.get("model") or os.environ.get("SUNO_MODEL", "V4_5ALL")}
Prompt:
{plan["prompt"]}

Negative prompt:
{plan["negativeTags"]}

Therapeutic intention:
{plan["therapeuticIntention"]}"""


def audio_config_from_plan(intake, plan, feedback=None):
    profile = MOOD_PROFILES.get(intake["mood"], MOOD_PROFILES["Calm"])
    stress = int(intake["stress"])
    tempo = profile["tempo"]
    for token in (plan.get("style", "") + " " + plan.get("prompt", "")).replace(",", " ").split():
        if token.isdigit():
            number = int(token)
            if 40 <= number <= 100:
                tempo = number
                break
    return {
        "tempo": max(46, min(78, tempo)),
        "intensity": max(0.15, min(0.8, (11 - stress) / 12)),
        "warmth": 0.74 if feedback and feedback["rating"] == "down" else 0.9,
        "root": 220 if "minor" in profile["key"] else 246.94,
    }


def call_suno_api(plan):
    api_key = os.environ.get("SUNO_APIKEY")
    if not api_key:
        raise ProviderError("SUNO_APIKEY is missing from the server environment.")

    request_payload = {
        "customMode": True,
        "instrumental": bool(plan.get("instrumental", True)),
        "model": plan.get("model") or os.environ.get("SUNO_MODEL", "V4_5ALL"),
        "callBackUrl": suno_callback_url(),
        "prompt": plan["prompt"][:5000],
        "style": plan["style"][:1000],
        "title": plan["title"][:80],
        "negativeTags": plan["negativeTags"][:1000],
    }
    result = http_json(
        "POST",
        f"{SUNO_BASE_URL}/generate",
        {
            "Authorization": bearer_token(api_key),
            "Content-Type": "application/json",
        },
        request_payload,
    )
    if result.get("code") != 200:
        raise ProviderError(f"Suno generation failed: {result.get('msg') or result}")
    task_id = (result.get("data") or {}).get("taskId")
    if not task_id:
        raise ProviderError("Suno generation did not return a taskId.")
    return {
        "task_id": task_id,
        "request": request_payload,
        "response": result,
        "status": "PENDING",
    }


def extract_suno_audio(data):
    if data is None:
        return None
    if isinstance(data, list):
        candidates = data
    else:
        response = data.get("response") or {}
        candidates = response.get("sunoData") or response.get("data") or data.get("sunoData") or data.get("data") or []
    if not candidates:
        return None
    first = candidates[0]
    return {
        "audio_url": first.get("audioUrl") or first.get("audio_url") or first.get("streamAudioUrl") or first.get("stream_audio_url"),
        "stream_audio_url": first.get("streamAudioUrl") or first.get("stream_audio_url"),
        "title": first.get("title"),
        "duration": first.get("duration"),
        "raw": first,
    }


def is_suno_terminal_status(status):
    return str(status or "").strip().upper() in {
        "COMPLETED",
        "COMPLETE",
        "SUCCESS",
        "SUCCEEDED",
        "DONE",
        "FINISHED",
        "FINISH",
    }


def download_audio_file(audio_url, task_id, track_id):
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(audio_url)
    ext = Path(parsed.path).suffix.lower() or ".mp3"
    if ext not in {".mp3", ".wav", ".ogg", ".m4a", ".flac"}:
        ext = ".mp3"
    safe_task_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(task_id) or "audio")
    filename = f"track_{track_id}_{safe_task_id}{ext}"
    local_path = AUDIO_DIR / filename
    if local_path.exists():
        return local_path

    request = Request(audio_url, method="GET")
    request.add_header("User-Agent", "Neuorise/1.0")
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS, context=provider_ssl_context()) as response:
            local_path.write_bytes(response.read())
    except HTTPError as error:
        raise ProviderError(f"Audio download failed: {error.code} {error.reason}") from error
    except URLError as error:
        raise ProviderError(f"Audio download failed: {error.reason}") from error
    except Exception as error:
        raise ProviderError(f"Audio download failed: {error}") from error
    return local_path


def poll_suno_task(task_id):
    api_key = os.environ.get("SUNO_APIKEY")
    if not api_key:
        raise ProviderError("SUNO_APIKEY is missing from the server environment.")
    query = urlencode({"taskId": task_id})
    result = http_json(
        "GET",
        f"{SUNO_BASE_URL}/generate/record-info?{query}",
        {"Authorization": bearer_token(api_key)},
    )
    if result.get("code") != 200:
        raise ProviderError(f"Suno status check failed: {result.get('msg') or result}")
    data = result.get("data") or result
    audio = extract_suno_audio(data) or {}
    return {
        "status": (data.get("status") or data.get("state") or "PENDING").upper(),
        "audio_url": audio.get("audio_url"),
        "title": audio.get("title"),
        "provider_response": result,
    }


def suno_update_from_payload(payload):
    data = payload.get("data") or payload
    task_id = data.get("taskId") or data.get("task_id") or payload.get("taskId")
    status = data.get("status") or payload.get("status") or "PENDING"
    audio = extract_suno_audio(data) or extract_suno_audio({"response": data}) or {}
    return {
        "task_id": task_id,
        "status": status,
        "audio_url": audio.get("audio_url"),
        "title": audio.get("title"),
        "provider_response": payload,
    }


def persist_suno_update(connection, update, schedule_background=True):
    if not update.get("task_id"):
        return None
    track = connection.execute(
        """
        SELECT tracks.*, healing_sessions.user_id
        FROM tracks
        JOIN healing_sessions ON healing_sessions.id = tracks.session_id
        WHERE tracks.provider_task_id = ?
        """,
        (update["task_id"],),
    ).fetchone()
    if not track:
        return None

    audio_url = update.get("audio_url")
    local_audio_url = track["audio_url"]

    if local_audio_url and local_audio_url.startswith(AUDIO_URL_PATH):
        connection.execute(
            """
            UPDATE tracks
            SET status = ?, title = COALESCE(?, title), provider_response_json = ?
            WHERE id = ?
            """,
            (update["status"], update.get("title"), json.dumps(update["provider_response"]), track["id"]),
        )
    elif audio_url:
        connection.execute(
            """
            UPDATE tracks
            SET status = ?, audio_url = COALESCE(NULL, audio_url), title = COALESCE(?, title), provider_response_json = ?
            WHERE id = ?
            """,
            (update["status"], update.get("title"), json.dumps(update["provider_response"]), track["id"]),
        )

        if schedule_background and not str(audio_url).startswith(AUDIO_URL_PATH):
            # Start a daemon thread to download and save the audio file.
            threading.Thread(
                target=_background_download_and_persist,
                args=(track["id"], update["task_id"], audio_url),
                daemon=True,
            ).start()
    else:
        connection.execute(
            """
            UPDATE tracks
            SET status = ?, title = COALESCE(?, title), provider_response_json = ?
            WHERE id = ?
            """,
            (update["status"], update.get("title"), json.dumps(update["provider_response"]), track["id"]),
        )

    connection.execute("UPDATE healing_sessions SET updated_at = ? WHERE id = ?", (iso_now(), track["session_id"]))
    return track


def _background_download_and_persist(track_id, task_id, audio_url):
    """Download the audio in a background thread and update the DB.

    This function uses its own sqlite connection (via `db()`) so it does
    not block the main request-handling thread.
    """
    try:
        local_path = download_audio_file(audio_url, task_id, track_id)
        local_audio_url = f"{AUDIO_URL_PATH}/{local_path.name}"
    except ProviderError:
        local_audio_url = audio_url

    try:
        with db() as connection:
            new_status = "COMPLETED" if str(local_audio_url).startswith(AUDIO_URL_PATH) else "AVAILABLE"
            connection.execute(
                """
                UPDATE tracks
                SET audio_url = ?, status = ?, provider_response_json = COALESCE(provider_response_json, ?)
                WHERE id = ?
                """,
                (local_audio_url, new_status, json.dumps({}), track_id),
            )
            row = connection.execute("SELECT session_id FROM tracks WHERE id = ?", (track_id,)).fetchone()
            if row:
                connection.execute("UPDATE healing_sessions SET updated_at = ? WHERE id = ?", (iso_now(), row["session_id"]))
    except Exception:
        pass


def validate_intake(intake):
    required = ["mood", "energy", "stress", "need", "texture", "heartRate", "breathRate", "duration"]
    missing = [field for field in required if field not in intake or intake[field] in ("", None)]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")
    for field, low, high in [("energy", 1, 10), ("stress", 1, 10), ("heartRate", 40, 180), ("breathRate", 4, 40), ("duration", 2, 30)]:
        value = int(intake[field])
        if value < low or value > high:
            raise ValueError(f"{field} must be between {low} and {high}.")


def create_track(connection, session_id, version, intake, feedback=None):
    plan = call_gemini_api(intake, feedback)
    suno = call_suno_api(plan)
    suno_prompt = suno_prompt_summary(plan)
    audio_config = audio_config_from_plan(intake, plan, feedback)
    created_at = iso_now()
    cursor = connection.execute(
        """
        INSERT INTO tracks (
          session_id, version, title, gemini_prompt, suno_prompt,
          suno_request_json, provider_task_id, provider_response_json,
          audio_url, audio_config_json, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            version,
            plan["title"],
            plan["raw"],
            suno_prompt,
            json.dumps(suno["request"]),
            suno["task_id"],
            json.dumps(suno["response"]),
            None,
            json.dumps(audio_config),
            suno["status"],
            created_at,
        ),
    )
    return cursor.lastrowid, plan["title"]


def session_payload(connection, session_id, user_id):
    session = connection.execute(
        "SELECT * FROM healing_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    ).fetchone()
    if not session:
        return None
    tracks = connection.execute(
        "SELECT * FROM tracks WHERE session_id = ? ORDER BY version ASC",
        (session_id,),
    ).fetchall()
    feedback = connection.execute(
        "SELECT * FROM feedback WHERE session_id = ? ORDER BY created_at DESC",
        (session_id,),
    ).fetchall()
    return {
        "id": session["id"],
        "title": session["title"],
        "favorite": bool(session["favorite"] if "favorite" in session.keys() else False),
        "intake": json.loads(session["intake_json"]),
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "tracks": [
            {
                "id": track["id"],
                "version": track["version"],
                "title": track["title"],
                "gemini_prompt": track["gemini_prompt"],
                "suno_prompt": track["suno_prompt"],
                "suno_request": json.loads(track["suno_request_json"]),
                "provider_task_id": track["provider_task_id"],
                "provider_response": json.loads(track["provider_response_json"]) if track["provider_response_json"] else None,
                "audio_url": track["audio_url"],
                "audio_config": json.loads(track["audio_config_json"]),
                "status": track["status"],
                "created_at": track["created_at"],
            }
            for track in tracks
        ],
        "feedback": [
            {
                "id": item["id"],
                "track_id": item["track_id"],
                "rating": item["rating"],
                "feedback_text": item["feedback_text"],
                "skipped": bool(item["skipped"]),
                "created_at": item["created_at"],
            }
            for item in feedback
        ],
        "folder": (lambda fid: (None if not fid else (lambda r: {"id": r["id"], "name": r["name"], "color": r["color"]} if r else None)(connection.execute("SELECT * FROM folders WHERE id = ? AND user_id = ?", (fid, user_id)).fetchone())))(session["folder_id"] if "folder_id" in session.keys() else None),
    }


def handle_api(environ, start_response, path, method):
    try:
        if path == "/api/captcha/challenge" and method == "GET":
            challenge = cap_server.create_challenge()
            return json_response(start_response, HTTPStatus.OK, {"captcha": challenge})

        if path == "/api/verify-email":
            qs = parse_qs(environ.get("QUERY_STRING", "") or "")
            token = (qs.get("token") or [None])[0]
            if not token:
                url = "/verify-result.html?status=error&message=Verification+token+is+missing"
                return redirect_response(start_response, url)
            with db() as connection:
                row = connection.execute("SELECT * FROM users WHERE verification_token = ?", (token,)).fetchone()
                if not row:
                    url = "/verify-result.html?status=error&message=Invalid+or+already+used+verification+link"
                    return redirect_response(start_response, url)
                connection.execute(
                    "UPDATE users SET verified = 1, verified_at = ?, verification_token = NULL, verification_sent_at = NULL WHERE id = ?",
                    (iso_now(), row["id"]),
                )
                user = connection.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
            url = "/verify-result.html?status=success&message=Email+verified+successfully"
            return redirect_response(start_response, url, [create_session_cookie(user["id"])])

        if path == "/api/resend-verification" and method == "POST":
            payload = read_json(environ)
            email = (payload.get("email") or "").strip().lower()
            if not email:
                raise ValueError("Email is required.")
            with db() as connection:
                row = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if not row:
                    raise ValueError("No account found with that email.")
                if row["verified"] if "verified" in row.keys() else 0:
                    raise ValueError("Account already verified.")
                last_sent = row["verification_sent_at"]
                if last_sent:
                    try:
                        last_sent_dt = datetime.fromisoformat(last_sent)
                    except ValueError:
                        last_sent_dt = None
                    if last_sent_dt:
                        elapsed = utc_now() - last_sent_dt
                        if elapsed.total_seconds() < 60:
                            remaining = int(60 - elapsed.total_seconds())
                            raise ValueError(f"Please wait {remaining} seconds before resending verification email.")
                token = secrets.token_urlsafe(24)
                connection.execute(
                    "UPDATE users SET verification_token = ?, verification_sent_at = ? WHERE id = ?",
                    (token, iso_now(), row["id"]),
                )
            threading.Thread(target=send_verification_email, args=(email, token, row["name"]), daemon=True).start()
            return json_response(start_response, HTTPStatus.OK, {"message": "Verification email resent. Please check your inbox."})

        if path == "/api/me" and method == "DELETE":
            user = require_user(environ)
            with db() as connection:
                connection.execute("DELETE FROM users WHERE id = ?", (user["id"],))
            return json_response(start_response, HTTPStatus.OK, {"ok": True, "message": "Account deleted."})

        if path == "/api/forgot-password" and method == "POST":
            payload = read_json(environ)
            validate_captcha(environ, payload)
            email = (payload.get("email") or "").strip().lower()
            if not email:
                raise ValueError("Email is required.")
            with db() as connection:
                user = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if not user:
                    return json_response(start_response, HTTPStatus.OK, {"message": "If an account exists with that email, a password reset link has been sent."})
                reset_token = secrets.token_urlsafe(24)
                reset_expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                connection.execute(
                    "UPDATE users SET reset_token = ?, reset_token_expires_at = ? WHERE id = ?",
                    (reset_token, reset_expires, user["id"])
                )
            reset_url = f"{os.environ.get('SITE_URL', 'http://localhost:8000')}/?reset={reset_token}"
            threading.Thread(
                target=send_reset_password_email,
                args=(email, reset_url, user["name"]),
                daemon=True
            ).start()
            return json_response(start_response, HTTPStatus.OK, {"message": "If an account exists with that email, a password reset link has been sent."})

        if path == "/api/reset-password" and method == "POST":
            payload = read_json(environ)
            token = (payload.get("token") or "").strip()
            password = payload.get("password") or ""
            if not token or len(password) < 8:
                raise ValueError("Valid token and 8+ character password are required.")
            with db() as connection:
                user = connection.execute(
                    "SELECT * FROM users WHERE reset_token = ? AND reset_token_expires_at > ?",
                    (token, datetime.now(timezone.utc).isoformat())
                ).fetchone()
                if not user:
                    raise ValueError("Invalid or expired password reset link.")
                password_hash = hash_password(password)
                connection.execute(
                    "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expires_at = NULL WHERE id = ?",
                    (password_hash, user["id"])
                )
            return json_response(start_response, HTTPStatus.OK, {"message": "Password reset successfully. Please log in with your new password."})

        if path == "/api/signup" and method == "POST":
            payload = read_json(environ)
            validate_captcha(environ, payload)
            name = (payload.get("name") or "").strip()
            email = (payload.get("email") or "").strip().lower()
            password = payload.get("password") or ""
            if not name or not email or len(password) < 8:
                raise ValueError("Name, email, and an 8+ character password are required.")
            with db() as connection:
                try:
                    user_id = generate_session_id()
                    connection.execute(
                        "INSERT INTO users (id, name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                        (user_id, name, email, hash_password(password), iso_now()),
                    )
                except sqlite3.IntegrityError:
                    raise ValueError("An account with that email already exists.")
                user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            token = secrets.token_urlsafe(24)
            with db() as connection:
                connection.execute(
                    "UPDATE users SET verification_token = ?, verification_sent_at = ? WHERE id = ?",
                    (token, iso_now(), user["id"]),
                )
            threading.Thread(target=send_verification_email, args=(email, token, name), daemon=True).start()
            return json_response(start_response, HTTPStatus.CREATED, {"user": serialize_user(user), "message": "Verification email sent. Please check your inbox."})

        if path == "/api/login" and method == "POST":
            payload = read_json(environ)
            validate_captcha(environ, payload)
            email = (payload.get("email") or "").strip().lower()
            password = payload.get("password") or ""
            with db() as connection:
                user = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not user or not verify_password(password, user["password_hash"]):
                raise ValueError("Invalid email or password.")
            if not (user["verified"] if "verified" in user.keys() else 0):
                raise ValueError("Please verify your email address before logging in.")
            return json_response(start_response, HTTPStatus.OK, {"user": serialize_user(user)}, [create_session_cookie(user["id"])])

        if path == "/api/logout" and method == "POST":
            return json_response(start_response, HTTPStatus.OK, {"ok": True}, [clear_session_cookie(environ)])

        if path == "/api/me" and method == "GET":
            user = require_user(environ)
            return json_response(start_response, HTTPStatus.OK, {"user": serialize_user(user)})

        if path == "/api/me" and method == "PATCH":
            user = require_user(environ)
            payload = read_json(environ)
            updates = {}
            name = payload.get("name")
            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError("Name cannot be empty.")
                updates["name"] = name
            email = payload.get("email")
            email_changed = False
            verification_token = None
            if email is not None:
                email = email.strip().lower()
                if not email:
                    raise ValueError("Email cannot be empty.")
                if email != user["email"]:
                    email_changed = True
                    updates["email"] = email
                    updates["verified"] = 0
                    verification_token = secrets.token_urlsafe(24)
                    updates["verification_token"] = verification_token
                    updates["verification_sent_at"] = iso_now()
                    updates["verified_at"] = None
                else:
                    updates["email"] = email
            password = payload.get("password")
            if password:
                if len(password) < 8:
                    raise ValueError("Password must be at least 8 characters.")
                updates["password_hash"] = hash_password(password)
            if not updates:
                raise ValueError("No profile changes were submitted.")
            with db() as connection:
                if "email" in updates and email_changed:
                    existing = connection.execute(
                        "SELECT id FROM users WHERE email = ? AND id != ?",
                        (updates["email"], user["id"]),
                    ).fetchone()
                    if existing:
                        raise ValueError("An account with that email already exists.")
                set_clause = ", ".join(f"{key} = ?" for key in updates)
                params = list(updates.values()) + [user["id"]]
                connection.execute(f"UPDATE users SET {set_clause} WHERE id = ?", params)
                updated = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            if email_changed:
                email_name = updates.get("name") or user["name"]
                threading.Thread(target=send_verification_email, args=(updates["email"], verification_token, email_name), daemon=True).start()
            return json_response(start_response, HTTPStatus.OK, {"user": serialize_user(updated), "message": "Email changed; verification is required for the new address."})

        if path == "/api/suno/callback" and method == "POST":
            return json_response(start_response, HTTPStatus.GONE, {"error": "Callback-based updates are deprecated. Use polling via /api/tracks/{id}/refresh instead."})

        if path.startswith("/api/tracks/") and method == "GET":
            user = require_user(environ)
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "tracks" and parts[3] == "refresh":
                track_id = int(parts[2])
                with db() as connection:
                    track = connection.execute(
                        """
                        SELECT tracks.*, healing_sessions.user_id, healing_sessions.updated_at as session_updated_at
                        FROM tracks
                        JOIN healing_sessions ON healing_sessions.id = tracks.session_id
                        WHERE tracks.id = ? AND healing_sessions.user_id = ?
                        """,
                        (track_id, user["id"]),
                    ).fetchone()
                    if not track:
                        return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "Track not found."})
                    needs_refresh = track["provider_task_id"] and (
                        not track["audio_url"] or not str(track["audio_url"]).startswith(AUDIO_URL_PATH)
                    )
                    if needs_refresh:
                        last_poll_time = datetime.fromisoformat(track["session_updated_at"])
                        time_since_last_poll = utc_now() - last_poll_time
                        if time_since_last_poll.total_seconds() >= MIN_POLL_INTERVAL_SECONDS:
                            update = poll_suno_task(track["provider_task_id"])
                            update["task_id"] = track["provider_task_id"]
                            persist_suno_update(connection, update, schedule_background=True)

                            refreshed = connection.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
                            try:
                                provider_status = (refreshed["status"] or "").upper()
                                audio_url = refreshed["audio_url"]
                            except Exception:
                                provider_status = ""
                                audio_url = None

                            if is_suno_terminal_status(provider_status) and audio_url and not str(audio_url).startswith(AUDIO_URL_PATH):
                                try:
                                    local_path = download_audio_file(audio_url, refreshed["provider_task_id"], refreshed["id"])
                                    local_audio_url = f"{AUDIO_URL_PATH}/{local_path.name}"
                                    connection.execute(
                                        """
                                        UPDATE tracks
                                        SET audio_url = ?, status = ?, provider_response_json = COALESCE(provider_response_json, ?)
                                        WHERE id = ?
                                        """,
                                        (local_audio_url, "COMPLETED", json.dumps({}), refreshed["id"]),
                                    )
                                    connection.execute("UPDATE healing_sessions SET updated_at = ? WHERE id = ?", (iso_now(), refreshed["session_id"]))
                                except ProviderError:
                                    threading.Thread(
                                        target=_background_download_and_persist,
                                        args=(refreshed["id"], update["task_id"], audio_url),
                                        daemon=True,
                                    ).start()
                    session = session_payload(connection, track["session_id"], user["id"])
                return json_response(start_response, HTTPStatus.OK, {"session": session})

        if path == "/api/sessions" and method == "GET":
            user = require_user(environ)
            with db() as connection:
                rows = connection.execute(
                    """
                    SELECT healing_sessions.*, COUNT(tracks.id) AS track_count
                    FROM healing_sessions
                    LEFT JOIN tracks ON tracks.session_id = healing_sessions.id
                    WHERE healing_sessions.user_id = ?
                    GROUP BY healing_sessions.id
                    ORDER BY healing_sessions.updated_at DESC
                    """,
                    (user["id"],),
                ).fetchall()
            sessions = []
            for row in rows:
                intake = json.loads(row["intake_json"])
                sessions.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "mood": intake["mood"],
                        "need": intake["need"],
                        "track_count": row["track_count"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )
            return json_response(start_response, HTTPStatus.OK, {"sessions": sessions})

        if path == "/api/sessions" and method == "POST":
            user = require_user(environ)
            payload = read_json(environ)
            validate_captcha(environ, payload)
            intake = payload.get("intake") or {}
            validate_intake(intake)
            now = iso_now()
            with db() as connection:
                session_id = generate_session_id()
                connection.execute(
                    "INSERT INTO healing_sessions (id, user_id, title, intake_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, user["id"], "", json.dumps(intake), now, now),
                )
                _, track_title = create_track(connection, session_id, 1, intake)
                connection.execute(
                    "UPDATE healing_sessions SET title = ?, updated_at = ? WHERE id = ?",
                    (track_title, iso_now(), session_id),
                )
                session = session_payload(connection, session_id, user["id"])
            return json_response(start_response, HTTPStatus.CREATED, {"session": session})

        if path == "/api/sessions/bulk" and method == "DELETE":
            user = require_user(environ)
            payload = read_json(environ)
            ids = payload.get("ids") or []
            if not isinstance(ids, list) or not ids:
                raise ValueError("No session ids provided for bulk delete.")
            with db() as connection:
                connection.execute(
                    f"DELETE FROM healing_sessions WHERE user_id = ? AND id IN ({','.join('?' for _ in ids)})",
                    (user["id"],) + tuple(ids),
                )
            return json_response(start_response, HTTPStatus.OK, {"message": "Sessions deleted."})

        if path.startswith("/api/sessions/"):
            user = require_user(environ)
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "sessions":
                session_id = parts[2]
                if len(parts) == 3 and method == "GET":
                    with db() as connection:
                        session = session_payload(connection, session_id, user["id"])
                    if not session:
                        return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "Session not found."})
                    return json_response(start_response, HTTPStatus.OK, {"session": session})
                if len(parts) == 3 and method == "PATCH":
                    payload = read_json(environ)
                    updates = {}
                    if "title" in payload:
                        title = (payload.get("title") or "").strip()
                        if not title:
                            raise ValueError("Title cannot be empty.")
                        updates["title"] = title
                    if "favorite" in payload:
                        fav = payload.get("favorite")
                        updates["favorite"] = 1 if fav else 0
                    if "folder_id" in payload:
                        folder_id = payload.get("folder_id")
                        if folder_id is None:
                            updates["folder_id"] = None
                        else:
                            with db() as connection:
                                row = connection.execute("SELECT id FROM folders WHERE id = ? AND user_id = ?", (folder_id, user["id"])) .fetchone()
                            if not row:
                                raise ValueError("Folder not found.")
                            updates["folder_id"] = folder_id

                    if not updates:
                        raise ValueError("No valid session fields provided to update.")

                    set_clause = ", ".join(f"{k} = ?" for k in updates.keys()) + ", updated_at = ?"
                    params = list(updates.values()) + [iso_now(), session_id]
                    with db() as connection:
                        session = session_payload(connection, session_id, user["id"])
                        if not session:
                            return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "Session not found."})
                        connection.execute(f"UPDATE healing_sessions SET {set_clause} WHERE id = ?", params)
                        updated = session_payload(connection, session_id, user["id"])
                    return json_response(start_response, HTTPStatus.OK, {"session": updated})
                if len(parts) == 3 and method == "DELETE":
                    with db() as connection:
                        session = session_payload(connection, session_id, user["id"])
                        if not session:
                            return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "Session not found."})
                        connection.execute("DELETE FROM healing_sessions WHERE id = ?", (session_id,))
                    return json_response(start_response, HTTPStatus.OK, {"message": "Session deleted successfully."})
                if len(parts) == 4 and parts[3] == "feedback" and method == "POST":
                    payload = read_json(environ)
                    validate_captcha(environ, payload)
                    rating = payload.get("rating")
                    if rating not in ("up", "down"):
                        raise ValueError("Rating must be up or down.")
                    feedback_data = {
                        "rating": rating,
                        "feedbackText": payload.get("feedbackText") or "",
                    }
                    with db() as connection:
                        session = session_payload(connection, session_id, user["id"])
                        if not session:
                            return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "Session not found."})
                        track_id = int(payload.get("trackId") or session["tracks"][-1]["id"])
                        connection.execute(
                            """
                            INSERT INTO feedback (session_id, track_id, rating, feedback_text, skipped, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (session_id, track_id, rating, feedback_data["feedbackText"], 1 if payload.get("skipped") else 0, iso_now()),
                        )
                        next_version = len(session["tracks"]) + 1
                        create_track(connection, session_id, next_version, session["intake"], feedback_data)
                        connection.execute("UPDATE healing_sessions SET updated_at = ? WHERE id = ?", (iso_now(), session_id))
                        updated = session_payload(connection, session_id, user["id"])
                    return json_response(start_response, HTTPStatus.CREATED, {"session": updated})

        if path == "/api/folders" and method == "GET":
            user = require_user(environ)
            with db() as connection:
                rows = connection.execute(
                    "SELECT * FROM folders WHERE user_id = ? ORDER BY updated_at DESC",
                    (user["id"],),
                ).fetchall()
            folders = [ {"id": r["id"], "name": r["name"], "color": r["color"], "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows ]
            return json_response(start_response, HTTPStatus.OK, {"folders": folders})

        if path == "/api/folders" and method == "POST":
            user = require_user(environ)
            payload = read_json(environ)
            name = (payload.get("name") or "").strip()
            color = (payload.get("color") or "#92d8c4").strip()
            if not name:
                raise ValueError("Folder name is required.")
            now = iso_now()
            with db() as connection:
                cursor = connection.execute(
                    "INSERT INTO folders (user_id, name, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (user["id"], name, color, now, now),
                )
                folder = connection.execute("SELECT * FROM folders WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return json_response(start_response, HTTPStatus.CREATED, {"folder": {"id": folder["id"], "name": folder["name"], "color": folder["color"], "created_at": folder["created_at"], "updated_at": folder["updated_at"]}})

        if path.startswith("/api/folders/"):
            user = require_user(environ)
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "folders":
                folder_id = int(parts[2])
                if len(parts) == 3 and method == "PATCH":
                    payload = read_json(environ)
                    updates = {}
                    if "name" in payload:
                        name = (payload.get("name") or "").strip()
                        if not name:
                            raise ValueError("Folder name cannot be empty.")
                        updates["name"] = name
                    if "color" in payload:
                        color = (payload.get("color") or "").strip()
                        updates["color"] = color
                    if not updates:
                        raise ValueError("No valid folder fields provided.")
                    set_clause = ", ".join(f"{k} = ?" for k in updates.keys()) + ", updated_at = ?"
                    params = list(updates.values()) + [iso_now(), folder_id]
                    with db() as connection:
                        row = connection.execute("SELECT * FROM folders WHERE id = ? AND user_id = ?", (folder_id, user["id"])) .fetchone()
                        if not row:
                            return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "Folder not found."})
                        connection.execute(f"UPDATE folders SET {set_clause} WHERE id = ?", params)
                        updated = connection.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
                    return json_response(start_response, HTTPStatus.OK, {"folder": {"id": updated["id"], "name": updated["name"], "color": updated["color"], "created_at": updated["created_at"], "updated_at": updated["updated_at"]}})
                if len(parts) == 4 and parts[3] == "addSessions" and method == "POST":
                    payload = read_json(environ)
                    ids = payload.get("ids") or []
                    if not isinstance(ids, list) or not ids:
                        raise ValueError("No session ids provided.")
                    with db() as connection:
                        row = connection.execute("SELECT * FROM folders WHERE id = ? AND user_id = ?", (folder_id, user["id"])) .fetchone()
                        if not row:
                            return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "Folder not found."})
                        connection.execute(
                            f"UPDATE healing_sessions SET folder_id = ? WHERE user_id = ? AND id IN ({','.join('?' for _ in ids)})",
                            (folder_id, user["id"]) + tuple(ids),
                        )
                    return json_response(start_response, HTTPStatus.OK, {"message": "Sessions added to folder."})

        return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "Not found."})
    except PermissionError as error:
        return json_response(start_response, HTTPStatus.UNAUTHORIZED, {"error": str(error)})
    except ProviderError as error:
        return json_response(start_response, HTTPStatus.BAD_GATEWAY, {"error": str(error)})
    except (ValueError, json.JSONDecodeError) as error:
        return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": str(error)})


def serve_static(environ, start_response, path):
    blocked_parts = {"data", "__pycache__"}

    if path == "/player" or path == "/player/" or path.startswith("/player/"):
        base = (ROOT / "player").resolve()
        rel = path[len("/player") :]
        safe_rel = "index.html" if rel in ("", "/") else rel.lstrip("/")
        file_path = (base / safe_rel).resolve()
        if not str(file_path).startswith(str(base)) or not file_path.is_file():
            file_path = base / "index.html"
    else:
        safe_path = "index.html" if path == "/" else path.lstrip("/")
        file_path = (ROOT / safe_path).resolve()

    is_blocked = any(part in blocked_parts for part in file_path.relative_to(ROOT).parts) if str(file_path).startswith(str(ROOT)) else True
    is_public_asset = file_path.suffix in MIME_TYPES
    if is_blocked or not is_public_asset or not str(file_path).startswith(str(ROOT)) or not file_path.is_file():
        file_path = ROOT / "index.html"
    body = file_path.read_bytes()
    headers = [
        ("Content-Type", MIME_TYPES.get(file_path.suffix, "application/octet-stream")),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "same-origin"),
    ]
    start_response(f"{HTTPStatus.OK.value} {HTTPStatus.OK.phrase}", headers)
    return [body]


def serve_audio(environ, start_response, path):
    filename = path[len(AUDIO_URL_PATH) :].lstrip("/")
    if not filename or ".." in filename or filename.startswith("/"):
        start_response(f"{HTTPStatus.NOT_FOUND.value} {HTTPStatus.NOT_FOUND.phrase}", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found."]
    file_path = (AUDIO_DIR / filename).resolve()
    if not str(file_path).startswith(str(AUDIO_DIR.resolve())) or not file_path.is_file():
        start_response(f"{HTTPStatus.NOT_FOUND.value} {HTTPStatus.NOT_FOUND.phrase}", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found."]
    file_size = file_path.stat().st_size
    range_header = environ.get("HTTP_RANGE")

    def send_full():
        body = file_path.read_bytes()
        headers = [
            ("Content-Type", MIME_TYPES.get(file_path.suffix, "application/octet-stream")),
            ("Content-Length", str(len(body))),
            ("Accept-Ranges", "bytes"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "same-origin"),
        ]
        start_response(f"{HTTPStatus.OK.value} {HTTPStatus.OK.phrase}", headers)
        return [body]

    if not range_header:
        return send_full()

    try:
        unit, ranges = range_header.split("=", 1)
        if unit.strip() != "bytes":
            return send_full()
        start_str, end_str = ranges.split("-", 1)
        start = int(start_str) if start_str.strip() else None
        end = int(end_str) if end_str.strip() else None
    except Exception:
        return send_full()

    if start is None and end is not None:
        if end == 0:
            start = 0
        else:
            start = max(0, file_size - end)
        end = file_size - 1
    if start is None:
        return send_full()
    if end is None or end >= file_size:
        end = file_size - 1
    if start > end or start < 0 or end < 0:
        headers = [("Content-Range", f"bytes */{file_size}"), ("Content-Type", "text/plain; charset=utf-8")]
        start_response(f"{HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE.value} {HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE.phrase}", headers)
        return [b"Requested Range Not Satisfiable"]

    length = end - start + 1
    with open(file_path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read(length)

    headers = [
        ("Content-Type", MIME_TYPES.get(file_path.suffix, "application/octet-stream")),
        ("Content-Range", f"bytes {start}-{end}/{file_size}"),
        ("Content-Length", str(len(chunk))),
        ("Accept-Ranges", "bytes"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "same-origin"),
    ]
    start_response(f"{HTTPStatus.PARTIAL_CONTENT.value} {HTTPStatus.PARTIAL_CONTENT.phrase}", headers)
    return [chunk]


def app(environ, start_response):
    parsed = urlparse(environ.get("PATH_INFO", "/"))
    path = parsed.path
    method = environ.get("REQUEST_METHOD", "GET")
    if path.startswith("/api/"):
        return handle_api(environ, start_response, path, method)
    if path.startswith(AUDIO_URL_PATH + "/"):
        return serve_audio(environ, start_response, path)
    if path.startswith("/cap/"):
        return handle_captcha(environ, start_response, path, method)
    return serve_static(environ, start_response, path)


def handle_captcha(environ, start_response, path, method):
    if path == "/cap/challenge" and method == "POST":
        challenge = cap_server.create_challenge()
        return json_response(start_response, HTTPStatus.OK, challenge)

    if path == "/cap/redeem" and method == "POST":
        payload = read_json(environ)
        token = (payload.get("token") if isinstance(payload, dict) else None)
        solutions = (payload.get("solutions") if isinstance(payload, dict) else None)
        if not token or solutions is None:
            raise ValueError("Captcha redeem request must include token and solutions.")
        result = cap_server.redeem(token, solutions)
        return json_response(start_response, HTTPStatus.OK, {"success": True, **result})

    start_response(f"{HTTPStatus.NOT_FOUND.value} {HTTPStatus.NOT_FOUND.phrase}", [("Content-Type", "application/json; charset=utf-8")])
    return [json.dumps({"error": "Not found."}).encode("utf-8")]


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5173"))
    with make_server("", port, app) as server:
        server.serve_forever()
