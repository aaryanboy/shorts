import os
import pickle
import flask
from flask import Flask, redirect, request, url_for, jsonify
import google.oauth2.credentials
from google.auth.transport.requests import Request
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
import tempfile
import subprocess
import imageio_ffmpeg
import yt_dlp
import json
import re
import requests as http_requests
from werkzeug.middleware.proxy_fix import ProxyFix

# ── Config ──────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "shorts-automation-secret-key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

if os.environ.get("FLASK_ENV") == "development":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid"
]
CLIENT_SECRETS_FILE = "credentials.json"
ACCOUNTS_FILE = "accounts.json"
TOKENS_DIR = "tokens"
VIDEO_DIR = "videos"
TEMP_DIR = os.path.join(VIDEO_DIR, "temp")

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(TOKENS_DIR, exist_ok=True)


# ── Account Helpers ─────────────────────────────────────────────────

def load_accounts():
    """Load the list of authenticated accounts from accounts.json."""
    if not os.path.exists(ACCOUNTS_FILE):
        return {}
    with open(ACCOUNTS_FILE, "r") as f:
        return json.load(f)

def save_accounts(accounts):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f)

def token_path(email):
    safe = email.replace("@", "_at_").replace(".", "_")
    return os.path.join(TOKENS_DIR, f"token_{safe}.pkl")

def load_credentials_for(email):
    path = token_path(email)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        creds = pickle.load(f)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials_for(creds, email)
        except Exception:
            return None
    return creds

def save_credentials_for(creds, email):
    with open(token_path(email), "wb") as f:
        pickle.dump(creds, f)

def get_email_from_creds(creds):
    """Fetch the user's email using Google's userinfo API."""
    try:
        resp = http_requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"}
        )
        return resp.json().get("email")
    except Exception:
        return None

def get_active_account():
    return flask.session.get("active_account")

def get_youtube_client(creds):
    return build("youtube", "v3", credentials=creds)


# ── OAuth Flow ───────────────────────────────────────────────────────

def get_flow(state=None):
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

    if os.environ.get("FLASK_ENV") == "development":
        redirect_uri = "http://localhost:5000/callback"
    else:
        redirect_uri = "https://god69851-shorts.hf.space/callback"

    if client_id and client_secret:
        client_config = {
            "web": {
                "client_id": client_id,
                "project_id": "shorts-automation",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_secret": client_secret,
                "redirect_uris": [redirect_uri]
            }
        }
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            client_config, scopes=SCOPES, state=state
        )
    else:
        flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE, scopes=SCOPES, state=state
        )

    flow.redirect_uri = redirect_uri
    return flow


# ── Video Helpers ────────────────────────────────────────────────────

def trim_video_for_shorts(video_path, meta=None, max_duration=178.0):
    try:
        duration, w, h = 0, 0, 0
        if meta and meta.get("duration") and meta.get("width") and meta.get("height"):
            duration = float(meta["duration"])
            w = int(meta["width"])
            h = int(meta["height"])

        if duration == 0 or w == 0 or h == 0:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            result = subprocess.run([ffmpeg_exe, "-i", video_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            d_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
            if d_match:
                duration = int(d_match.group(1)) * 3600 + int(d_match.group(2)) * 60 + float(d_match.group(3))
            res_match = re.search(r"Stream #.*: Video: .*, (\d+)x(\d+)[,\s]", result.stderr)
            if res_match:
                w, h = int(res_match.group(1)), int(res_match.group(2))

        if w == 0 or h == 0:
            return video_path, False

        aspect_ratio = w / h
        needs_padding = abs(aspect_ratio - (9/16)) > 0.05

        if duration > max_duration or needs_padding:
            temp_fd, temp_path = tempfile.mkstemp(suffix=".mp4")
            os.close(temp_fd)
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [ffmpeg_exe, "-y", "-i", video_path]
            if duration > max_duration:
                cmd.extend(["-t", str(max_duration)])
            if needs_padding:
                cmd.extend([
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac"
                ])
            else:
                cmd.extend(["-c", "copy"])
            cmd.append(temp_path)
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return temp_path, True

        return video_path, False
    except Exception as e:
        print(f"  FFmpeg error: {e}")
        return video_path, False


def upload_video(youtube, video_path, title="", description="", tags=None, privacy="private"):
    if not os.path.isfile(video_path):
        return {"error": f"File not found: {video_path}"}
    if not title:
        title = os.path.splitext(os.path.basename(video_path))[0]
    if "#Shorts" not in (description or ""):
        description = f"{description} #Shorts".strip()
    if tags is None:
        tags = ["Shorts"]
    elif "Shorts" not in tags:
        tags.append("Shorts")

    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": "22"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            print(f"  Uploading… {int(status.progress() * 100)}%")
    video_id = response["id"]
    url = f"https://youtube.com/shorts/{video_id}"
    print(f"  ✓ Uploaded: {url}")
    return {"id": video_id, "url": url}


# ── Routes ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    accounts = load_accounts()
    active = get_active_account()
    success = request.args.get("success")

    # Build accounts list HTML
    accounts_html = ""
    if accounts:
        for email, info in accounts.items():
            is_active = email == active
            active_badge = '<span style="background:#2ed573;color:#000;font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;margin-left:8px;">ACTIVE</span>' if is_active else ''
            accounts_html += f"""
            <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;
                        background:rgba(255,255,255,0.04);border:1px solid {'rgba(242,42,92,0.5)' if is_active else 'rgba(255,255,255,0.08)'};
                        border-radius:12px;margin-bottom:8px;transition:all 0.2s;">
                <div style="display:flex;align-items:center;gap:10px;text-align:left;">
                    <div style="width:36px;height:36px;border-radius:50%;background:linear-gradient(135deg,#f22a5c,#7b2ff7);
                                display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;flex-shrink:0;">
                        {email[0].upper()}
                    </div>
                    <div>
                        <div style="font-size:13px;font-weight:600;">{email}{active_badge}</div>
                        <div style="font-size:11px;color:#8892b0;">YouTube Channel</div>
                    </div>
                </div>
                <div style="display:flex;gap:6px;">
                    {'<span style="color:#8892b0;font-size:12px;padding:6px 10px;">✓</span>' if is_active else f'<a href="/set-active/{email}" style="background:rgba(242,42,92,0.15);color:#f22a5c;border:1px solid rgba(242,42,92,0.3);text-decoration:none;font-size:12px;font-weight:600;padding:6px 12px;border-radius:8px;">Use</a>'}
                    <a href="/remove-account/{email}" onclick="return confirm('Remove {email}?')" 
                       style="background:rgba(255,255,255,0.05);color:#8892b0;border:1px solid rgba(255,255,255,0.1);
                              text-decoration:none;font-size:12px;padding:6px 10px;border-radius:8px;">✕</a>
                </div>
            </div>
            """

    upload_section = ""
    if active:
        upload_section = f"""
        <div style="margin-top:20px;padding-top:20px;border-top:1px solid rgba(255,255,255,0.08);">
            <p style="color:#8892b0;font-size:12px;margin-bottom:12px;">Posting to: <strong style="color:#f22a5c;">{active}</strong></p>
            <div id="upload-form">
                <div style="display:flex;gap:8px;margin-bottom:12px;">
                    <input type="url" id="url-input" placeholder="Paste TikTok / Reel URL..." 
                           style="flex:1;padding:14px 16px;border-radius:12px;border:1px solid rgba(255,255,255,0.1);
                                  background:rgba(0,0,0,0.3);color:white;font-size:14px;font-family:Inter,sans-serif;outline:none;">
                    <button onclick="navigator.clipboard.readText().then(t=>document.getElementById('url-input').value=t)"
                            style="background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.1);color:white;
                                   border-radius:12px;padding:0 14px;cursor:pointer;font-size:18px;" title="Paste">📋</button>
                </div>
                <button onclick="submitPost()"
                        style="width:100%;background:linear-gradient(135deg,#f22a5c,#c0143c);color:white;border:none;
                               padding:14px;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;
                               transition:all 0.2s;letter-spacing:0.3px;">
                    🚀 Post to YouTube Shorts
                </button>
            </div>
        </div>
        <script>
        function submitPost() {{
            const url = document.getElementById('url-input').value.trim();
            if (!url) {{ alert('Please paste a URL first'); return; }}
            document.getElementById('loader-overlay').style.display = 'flex';
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = '/tiktok';
            const input = document.createElement('input');
            input.type = 'hidden'; input.name = 'url'; input.value = url;
            form.appendChild(input);
            document.body.appendChild(form);
            form.submit();
        }}
        </script>
        """
    elif accounts:
        upload_section = """
        <div style="margin-top:20px;padding-top:20px;border-top:1px solid rgba(255,255,255,0.08);
                    color:#8892b0;font-size:13px;text-align:center;">
            👆 Select an account above to start posting
        </div>
        """

    no_accounts_html = ""
    if not accounts:
        no_accounts_html = """
        <div style="text-align:center;padding:20px 0;color:#8892b0;">
            <div style="font-size:40px;margin-bottom:12px;">📺</div>
            <p style="font-size:14px;">No accounts added yet.<br>Add a Google account to get started.</p>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Shorts Bot</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg: #080a0e;
                --surface: rgba(255,255,255,0.04);
                --border: rgba(255,255,255,0.08);
                --primary: #f22a5c;
                --primary-dim: rgba(242,42,92,0.15);
                --text: #ffffff;
                --muted: #8892b0;
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: 'Inter', sans-serif;
                background: var(--bg);
                color: var(--text);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
                background: radial-gradient(ellipse at 70% 0%, #1a0a2e 0%, var(--bg) 55%),
                            radial-gradient(ellipse at 0% 100%, #0d1a2e 0%, transparent 50%);
            }}
            .card {{
                width: 100%;
                max-width: 440px;
                background: rgba(255,255,255,0.03);
                border: 1px solid var(--border);
                border-radius: 28px;
                padding: 32px 28px;
                backdrop-filter: blur(20px);
                box-shadow: 0 30px 60px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.05) inset;
            }}
            .header {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 28px;
            }}
            .logo {{
                display: flex;
                align-items: center;
                gap: 10px;
            }}
            .logo-icon {{
                width: 40px; height: 40px;
                background: linear-gradient(135deg, #f22a5c, #7b2ff7);
                border-radius: 12px;
                display: flex; align-items: center; justify-content: center;
                font-size: 18px;
            }}
            .logo h1 {{ font-size: 20px; font-weight: 800; letter-spacing: -0.5px; }}
            .logo span {{ font-size: 11px; color: var(--muted); font-weight: 500; display:block; margin-top:1px; }}
            .add-btn {{
                background: var(--primary-dim);
                color: var(--primary);
                border: 1px solid rgba(242,42,92,0.3);
                padding: 8px 16px;
                border-radius: 10px;
                font-size: 13px;
                font-weight: 700;
                cursor: pointer;
                text-decoration: none;
                transition: all 0.2s;
                white-space: nowrap;
            }}
            .add-btn:hover {{ background: rgba(242,42,92,0.25); transform: translateY(-1px); }}
            .section-label {{
                font-size: 11px;
                font-weight: 700;
                color: var(--muted);
                letter-spacing: 1px;
                text-transform: uppercase;
                margin-bottom: 10px;
            }}
            /* Loader */
            #loader-overlay {{
                position: fixed; inset: 0;
                background: rgba(8,10,14,0.92);
                backdrop-filter: blur(12px);
                display: none; flex-direction: column;
                align-items: center; justify-content: center;
                z-index: 1000;
            }}
            .spinner {{
                width: 52px; height: 52px;
                border: 3px solid rgba(255,255,255,0.08);
                border-top-color: var(--primary);
                border-radius: 50%;
                animation: spin 0.8s linear infinite;
                margin-bottom: 20px;
            }}
            @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
            .loader-title {{ font-size: 18px; font-weight: 700; margin-bottom: 8px; }}
            .loader-sub {{ color: var(--muted); font-size: 13px; text-align: center; max-width: 260px; line-height: 1.6; }}
            /* Toast */
            .toast {{
                position: fixed; top: 24px; right: 24px;
                background: rgba(46,213,115,0.12);
                border: 1px solid rgba(46,213,115,0.4);
                color: #2ed573;
                padding: 14px 20px;
                border-radius: 14px;
                font-weight: 600;
                font-size: 14px;
                transform: translateX(140%);
                animation: slideIn 0.4s cubic-bezier(0.34,1.56,0.64,1) forwards,
                           slideOut 0.4s ease 5s forwards;
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
                z-index: 999;
                display: flex; align-items: center; gap: 8px;
            }}
            @keyframes slideIn {{ to {{ transform: translateX(0); }} }}
            @keyframes slideOut {{ to {{ transform: translateX(140%); }} }}
        </style>
    </head>
    <body>
        {'<div class="toast">✅ Successfully uploaded to YouTube Shorts!</div>' if success else ''}

        <div id="loader-overlay">
            <div class="spinner"></div>
            <div class="loader-title">Processing Video</div>
            <p class="loader-sub">Downloading, trimming with FFmpeg, and uploading to YouTube… Please wait!</p>
        </div>

        <div class="card">
            <div class="header">
                <div class="logo">
                    <div class="logo-icon">▶</div>
                    <div>
                        <h1>Shorts Bot</h1>
                        <span>{len(accounts)} account{'s' if len(accounts) != 1 else ''} connected</span>
                    </div>
                </div>
                <a href="/add-account" target="_blank" class="add-btn">+ Add Account</a>
            </div>

            <div class="section-label">Connected Accounts</div>
            {no_accounts_html}
            {accounts_html}
            {upload_section}
        </div>
    </body>
    </html>
    """


@app.route("/add-account")
def add_account():
    """Start OAuth flow to add a new account."""
    flow = get_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    flask.session["state"] = state
    flask.session["code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/login")
def login():
    return redirect(url_for("add_account"))


@app.route("/callback")
def callback():
    state = flask.session.get("state")
    flow = get_flow(state=state)
    flow.code_verifier = flask.session.get("code_verifier")
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials

    # Get email from Google
    email = get_email_from_creds(creds)
    if not email:
        return "Could not retrieve email from Google. Please try again.", 400

    # Save credentials and register account
    save_credentials_for(creds, email)
    accounts = load_accounts()
    accounts[email] = {"email": email}
    save_accounts(accounts)

    # Auto-set as active if it's the first account
    if len(accounts) == 1 or not get_active_account():
        flask.session["active_account"] = email

    # Close popup and refresh parent
    return """
    <html><body>
    <script>
        if (window.opener) {
            window.opener.location.reload();
            window.close();
        } else {
            window.location.href = '/';
        }
    </script>
    <p>Account added! You can close this tab.</p>
    </body></html>
    """


@app.route("/set-active/<email>")
def set_active(email):
    accounts = load_accounts()
    if email in accounts:
        flask.session["active_account"] = email
    return redirect(url_for("index"))


@app.route("/remove-account/<email>")
def remove_account(email):
    accounts = load_accounts()
    if email in accounts:
        del accounts[email]
        save_accounts(accounts)
    path = token_path(email)
    if os.path.exists(path):
        os.remove(path)
    # If removed account was active, switch to another
    if get_active_account() == email:
        flask.session.pop("active_account", None)
        if accounts:
            flask.session["active_account"] = list(accounts.keys())[0]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    flask.session.clear()
    return redirect(url_for("index"))


@app.route("/tiktok", methods=["POST"])
def download_tiktok():
    target_url = request.form.get("url")
    active = get_active_account()

    if not target_url:
        return jsonify({"error": "No URL provided"})
    if not active:
        return jsonify({"error": "No active account selected"})

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(TEMP_DIR, '%(id)s.%(ext)s'),
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'writeinfojson': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"Downloading: {target_url}")
            info = ydl.extract_info(target_url, download=True)
            return redirect(url_for("upload", account=active))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/upload")
def upload():
    account = request.args.get("account") or get_active_account()
    if not account:
        return redirect(url_for("index"))

    creds = load_credentials_for(account)
    if not creds or not creds.valid:
        return redirect(url_for("add_account"))

    youtube = get_youtube_client(creds)

    videos_to_upload = []
    for folder in [VIDEO_DIR, TEMP_DIR]:
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                full = os.path.join(folder, f)
                if f.lower().endswith(".mp4") and os.path.isfile(full):
                    videos_to_upload.append(full)

    if not videos_to_upload:
        return jsonify({"error": "No .mp4 files found."})

    results = []
    for original_path in videos_to_upload:
        vid = os.path.basename(original_path)
        print(f"Processing: {vid}")

        yt_title = os.path.splitext(vid)[0]
        yt_desc = ""
        yt_tags = []

        base_name = os.path.splitext(original_path)[0]
        json_path = base_name + ".info.json"

        meta = None
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                raw_title = meta.get("title", "")
                yt_desc = meta.get("description", raw_title)
                yt_tags = meta.get("tags", [])
                yt_title = (raw_title[:90] + "...") if len(raw_title) > 95 else raw_title
                if not yt_title:
                    yt_title = "TikTok Video"

        upload_path, is_temp = trim_video_for_shorts(original_path, meta=meta)

        upload_success = False
        try:
            result = upload_video(youtube, upload_path, title=yt_title, description=yt_desc, tags=yt_tags)
            results.append({"file": vid, "trimmed": is_temp, **result})
            upload_success = True
        except HttpError as e:
            results.append({"file": vid, "error": str(e)})

        if is_temp and os.path.exists(upload_path):
            try:
                os.remove(upload_path)
            except OSError:
                pass

        if upload_success and TEMP_DIR in original_path:
            for cleanup in [json_path, original_path]:
                if os.path.exists(cleanup):
                    try:
                        os.remove(cleanup)
                    except OSError:
                        pass

    if any("error" not in r for r in results):
        return redirect(url_for("index", success=1))
    return jsonify({"errors": results})


# ── Main ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("YouTube Shorts Auto-Upload Bot")
    if os.environ.get("FLASK_ENV") == "development":
        app.run(host="localhost", port=5000, debug=True)
    else:
        app.run(host="0.0.0.0", port=7860)