import os
import shutil
import tempfile
import subprocess

import flask
from flask import Flask, redirect, request, url_for
from datetime import timedelta
from werkzeug.middleware.proxy_fix import ProxyFix

import imageio_ffmpeg
import yt_dlp

import google.oauth2.credentials
from google.auth.transport.requests import Request
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "shorts-secret")
app.permanent_session_lifetime = timedelta(days=90)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

DEV = os.environ.get("FLASK_ENV") == "development"
if DEV:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = "http://localhost:5000/callback" if DEV else "https://god69851-shorts.hf.space/callback"
TEMP_DIR = "/tmp/shorts"
os.makedirs(TEMP_DIR, exist_ok=True)


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────
def load_credentials():
    d = flask.session.get("credentials")
    if not d:
        return None
    creds = google.oauth2.credentials.Credentials(
        token=d["token"], refresh_token=d["refresh_token"],
        token_uri=d["token_uri"], client_id=d["client_id"],
        client_secret=d["client_secret"], scopes=d["scopes"],
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
        except Exception:
            return None
    return creds


def _save_credentials(creds):
    flask.session.permanent = True
    flask.session["credentials"] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }


def _get_flow(state=None):
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csecret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if cid and csecret:
        cfg = {"web": {
            "client_id": cid,
            "client_secret": csecret,
            "project_id": "shorts",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [REDIRECT_URI],
        }}
        flow = google_auth_oauthlib.flow.Flow.from_client_config(cfg, scopes=SCOPES, state=state)
    else:
        flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
            "credentials.json", scopes=SCOPES, state=state)
    flow.redirect_uri = REDIRECT_URI
    return flow


# ── Video helpers ────────────────────────────────────────────────────
def trim_if_needed(video_path, meta=None, max_duration=178.0):
    """
    Only trim if video exceeds max_duration.
    Uses -c copy (stream copy) — zero re-encoding, minimal CPU.
    TikTok videos are already 9:16 so padding is skipped entirely.
    Returns (path_to_upload, is_temp).
    """
    try:
        duration = float((meta or {}).get("duration") or 0)

        if duration == 0:
            # Fast ffprobe-style probe — no decoding
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            probe = subprocess.run(
                [ffmpeg_exe, "-i", video_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            d_m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe.stderr)
            if d_m:
                duration = (int(d_m.group(1)) * 3600
                            + int(d_m.group(2)) * 60
                            + float(d_m.group(3)))

        if duration <= max_duration:
            return video_path, False  # nothing to do — upload as-is

        # Trim only — stream copy, no re-encode (uses ~1% CPU instead of 98%)
        fd, out_path = tempfile.mkstemp(suffix=".mp4", dir=TEMP_DIR)
        os.close(fd)

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [ffmpeg_exe, "-y", "-i", video_path,
             "-t", str(max_duration),
             "-c", "copy",          # ← stream copy: no decode/encode
             out_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return out_path, True

    except Exception as e:
        print(f"FFmpeg error: {e}")
        return video_path, False


def upload_to_youtube(youtube, video_path, title="", description="", tags=None, privacy="public"):
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"File not found: {video_path}")

    if not title:
        title = os.path.splitext(os.path.basename(video_path))[0]
    if "#Shorts" not in (description or ""):
        description = (description + " #Shorts").strip()
    if "Shorts" not in tags:
        tags.append("Shorts")

    req = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        },
        media_body=MediaFileUpload(path, chunksize=5 * 1024 * 1024, resumable=True),
    )
    response = None
    while response is None:
        _, response = req.next_chunk()
    return response["id"]


# ─────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────
def render_page(logged_in, success=False, error=""):
    status_color = "#4ade80" if logged_in else "#6b7280"

    if logged_in:
        body_html = """
        <div class="input-row">
          <input type="url" id="url" placeholder="TikTok URL" autocomplete="off">
          <button class="paste-btn" onclick="doPaste()" title="Paste">&#x2398;</button>
        </div>
        <button class="post-btn" onclick="doSubmit()">Post</button>
        <a href="/logout" class="signout">sign out</a>
        """
    else:
        body_html = '<a href="/login" class="login-btn">Connect Google account</a>'

    toast_html = '<div class="toast">Uploaded successfully</div>' if success else ""
    error_html = f'<div class="error">{error}</div>' if error else ""

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Shorts Bot</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg-color: #0d0f12;
                --surface-color: rgba(255,255,255,0.05);
                --border-color: rgba(255,255,255,0.1);
                --primary: #f22a5c;
                --primary-hover: #d21c48;
                --text-main: #ffffff;
                --text-muted: #8892b0;
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: 'Inter', sans-serif;
                background: radial-gradient(circle at top right, #1f1122 0%, var(--bg-color) 60%);
                color: var(--text-main);
                display: flex; align-items: center; justify-content: center;
                min-height: 100vh; padding: 20px;
            }}
            .card {{
                background: var(--surface-color);
                border: 1px solid var(--border-color);
                border-radius: 24px; padding: 36px 28px;
                width: 100%; max-width: 420px;
                backdrop-filter: blur(12px);
                box-shadow: 0 20px 40px rgba(0,0,0,0.4);
            }}
            h1 {{ font-size: 26px; font-weight: 800; margin-bottom: 8px;
                  letter-spacing: -0.5px; text-align: center; }}
            .status-row {{ text-align: center; margin-bottom: 24px;
                           font-size: 13px; color: {dot_color}; }}
            .btn {{
                background: var(--primary); color: white; border: none;
                padding: 13px 20px; border-radius: 12px; font-size: 15px;
                font-weight: 600; cursor: pointer; transition: all 0.2s;
                text-decoration: none; display: inline-block;
                width: 100%; text-align: center;
            }}
            .btn:hover {{ background: var(--primary-hover); transform: translateY(-2px); }}
            .btn-login {{ background: #4285F4; }}
            .btn-login:hover {{ background: #2b70e4; }}
            .input-field {{
                width: 100%; padding: 13px 16px; border-radius: 12px;
                border: 1px solid var(--border-color);
                background: rgba(0,0,0,0.2); color: white;
                font-size: 14px; font-family: 'Inter', sans-serif;
                outline: none; transition: border-color 0.2s;
            }}
            .input-field:focus {{ border-color: var(--primary); }}
            .input-field::placeholder {{ color: #555; }}
            .logout-link {{
                color: var(--text-muted); text-decoration: none;
                font-size: 12px; margin-top: 10px; display: inline-block;
            }}
            .logout-link:hover {{ color: white; }}
            .error-box {{
                background: rgba(255,71,87,0.1); border: 1px solid #ff4757;
                color: #ff4757; border-radius: 12px; padding: 12px 16px;
                font-size: 13px; margin-bottom: 16px; word-break: break-word;
            }}
            #loader-overlay {{
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(13,15,18,0.92); backdrop-filter: blur(8px);
                display: none; flex-direction: column;
                align-items: center; justify-content: center; z-index: 1000;
            }}
            .spinner {{
                width: 50px; height: 50px;
                border: 4px solid rgba(255,255,255,0.1);
                border-top-color: var(--primary); border-radius: 50%;
                animation: spin 1s linear infinite; margin-bottom: 20px;
            }}
            @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
            .toast {{
                position: fixed; top: 24px; right: 24px;
                background: rgba(46,213,115,0.15); border: 1px solid #2ed573;
                color: #2ed573; padding: 14px 22px; border-radius: 12px;
                font-weight: 600;
                transform: translateX(130%);
                animation: slideIn 0.4s ease forwards, slideOut 0.4s ease 5s forwards;
                box-shadow: 0 10px 30px rgba(0,0,0,0.3); z-index: 999;
            }}
            @keyframes slideIn {{ to {{ transform: translateX(0); }} }}
            @keyframes slideOut {{ to {{ transform: translateX(130%); }} }}
        </style>
    </head>
    <body>
        {'<div class="toast">✅ Successfully uploaded!</div>' if success else ''}

        <div id="loader-overlay">
            <div class="spinner"></div>
            <h3 style="margin-bottom:8px;">Processing Video</h3>
            <p style="color:var(--text-muted); font-size:14px; text-align:center; max-width:80%;">
                Downloading, converting, and uploading to YouTube…<br>Please wait!
            </p>
        </div>

<div class="wrap">
  <div class="title">Shorts Bot</div>
  <div class="status">{"Connected" if logged_in else "Not connected"}</div>
  {error_html}
  {body_html}
</div>

        <script>
        function submitTikTok() {{
            const url = document.getElementById('tiktok-url').value.trim();
            if (!url) {{ alert('Please paste a TikTok URL first.'); return; }}
            document.getElementById('loader-overlay').style.display = 'flex';
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = '/post';
            const i = document.createElement('input');
            i.type = 'hidden'; i.name = 'url'; i.value = url;
            form.appendChild(i);
            document.body.appendChild(form);
            form.submit();
        }}
        </script>
    </body>
    </html>
    """


@app.route("/login")
def login():
    flow = _get_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    flask.session["state"] = state
    flask.session["code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/callback")
def callback():
    state = flask.session.get("state")
    if not state:
        return "Session state missing — enable cookies and try again.", 400
    flow = _get_flow(state=state)
    flow.code_verifier = flask.session.get("code_verifier")
    auth_response = request.url
    if not DEV and auth_response.startswith("http://"):
        auth_response = auth_response.replace("http://", "https://", 1)
    try:
        flow.fetch_token(authorization_response=auth_response)
    except Exception as e:
        return f"OAuth error: {e}", 500
    _save_credentials(flow.credentials)
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    flask.session.pop("credentials", None)
    return redirect(url_for("index"))


@app.route("/post", methods=["POST"])
def post():
    url = request.form.get("url", "").strip()
    if not url:
        return redirect(url_for("index", error="No URL provided."))

    creds = load_credentials()
    if not creds or not creds.valid:
        return redirect(url_for("login"))

    # ── 1. Download ──────────────────────────────────────────────────
    # Use a fresh isolated temp folder for this single request
    request_tmp = tempfile.mkdtemp(dir=TEMP_DIR)
    video_path = None
    json_path = None

    try:
        ydl_opts = {
            # Best separate video + audio streams, merged into mp4
            "format": "bestvideo[ext=mp4]+bestaudio/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }) as ydl:
            info = ydl.extract_info(url, download=True)

        # 2. Find downloaded file
        video_path = next(
            (os.path.join(tmp, f) for f in os.listdir(tmp)
             if f.endswith((".mp4", ".mkv", ".webm"))),
            None,
        )
        if not video_path:
            raise FileNotFoundError("Download produced no video file.")

        # ── 2. Read metadata ─────────────────────────────────────────
        meta = None
        yt_title = video_id
        yt_desc = ""
        yt_tags = []

        if json_path and os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            raw_title = meta.get("title", "")
            yt_title = (raw_title[:90] + "…") if len(raw_title) > 95 else raw_title
            if not yt_title:
                yt_title = "TikTok Video"
            yt_desc = meta.get("description", raw_title)
            yt_tags = meta.get("tags", [])

        # ── 3. Trim if over 3 min (stream copy — no re-encode) ───────
        upload_path, is_temp = trim_if_needed(video_path, meta=meta)

        # 5. Upload
        yt = build("youtube", "v3", credentials=creds)
        upload_to_youtube(yt, processed, title, description, tags)

        return redirect(url_for("index", success=1))

    except HttpError as e:
        return redirect(url_for("index", error=f"YouTube error: {e}"))
    except Exception as e:
        print(f"Error: {e}")
        return redirect(url_for("index", error=str(e)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app.run(
        host="localhost" if DEV else "0.0.0.0",
        port=5000 if DEV else 7860,
    )