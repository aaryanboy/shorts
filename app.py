import os
import flask
from flask import Flask, redirect, request, url_for, jsonify
from datetime import timedelta
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
from werkzeug.middleware.proxy_fix import ProxyFix



# ── Config ──────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "shorts-automation-secret-key")
app.permanent_session_lifetime = timedelta(days=90)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

if os.environ.get("FLASK_ENV") == "development":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRETS_FILE = "credentials.json"

VIDEO_DIR = "videos"
TEMP_DIR = os.path.join(VIDEO_DIR, "temp")

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

ACCOUNT_SLOTS = ["account_1", "account_2", "account_3", "account_4"]
DEFAULT_NAMES  = {"account_1": "Account 1", "account_2": "Account 2",
                  "account_3": "Account 3", "account_4": "Account 4"}


def get_account_name(slot):
    return flask.session.get(f"name_{slot}", DEFAULT_NAMES.get(slot, slot))


def load_credentials(slot):
    key = f"credentials_{slot}"
    if key not in flask.session:
        return None

    creds_data = flask.session[key]
    creds = google.oauth2.credentials.Credentials(
        token=creds_data.get('token'),
        refresh_token=creds_data.get('refresh_token'),
        token_uri=creds_data.get('token_uri'),
        client_id=creds_data.get('client_id'),
        client_secret=creds_data.get('client_secret'),
        scopes=creds_data.get('scopes')
    )

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds, slot)
        except Exception:
            return None

    return creds


def save_credentials(creds, slot):
    flask.session.permanent = True
    flask.session[f"credentials_{slot}"] = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }


def get_youtube_client(creds):
    return build("youtube", "v3", credentials=creds)


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


def trim_video_for_shorts(video_path, meta=None, max_duration=178.0):
    try:
        duration, w, h = 0, 0, 0

        if meta and meta.get("duration") and meta.get("width") and meta.get("height"):
            duration = float(meta["duration"])
            w = int(meta["width"])
            h = int(meta["height"])

        if duration == 0 or w == 0 or h == 0:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            result = subprocess.run([ffmpeg_exe, "-i", video_path],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            d_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
            if d_match:
                duration = int(d_match.group(1)) * 3600 + int(d_match.group(2)) * 60 + float(d_match.group(3))

            res_match = re.search(r"Stream #.*: Video: .*, (\d+)x(\d+)[,\s]", result.stderr)
            if res_match:
                w, h = int(res_match.group(1)), int(res_match.group(2))

        if w == 0 or h == 0:
            return video_path, False

        needs_padding = abs((w / h) - (9 / 16)) > 0.05

        if duration > max_duration or needs_padding:
            temp_fd, temp_path = tempfile.mkstemp(suffix=".mp4")
            os.close(temp_fd)

            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [ffmpeg_exe, "-y", "-i", video_path]

            if duration > max_duration:
                cmd.extend(["-t", str(max_duration)])

            if needs_padding:
                cmd.extend([
                    "-vf",
                    "scale=1080:1920:force_original_aspect_ratio=decrease,"
                    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac"
                ])
            else:
                cmd.extend(["-c", "copy"])

            cmd.append(temp_path)
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return temp_path, True

        return video_path, False
    except Exception as e:
        print(f"  Error processing video with FFmpeg: {e}")
        return video_path, False


def upload_video(youtube, video_path, title="", description="", tags=None, privacy="public"):
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
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
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


def do_upload_for_slot(slot, videos_to_upload):
    creds = load_credentials(slot)
    if not creds or not creds.valid:
        return None, "not_authenticated"

    youtube = get_youtube_client(creds)
    results = []

    for original_path in videos_to_upload:
        vid = os.path.basename(original_path)
        yt_title, yt_desc, yt_tags = os.path.splitext(vid)[0], "", []

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
            result = upload_video(youtube, upload_path,
                                  title=yt_title, description=yt_desc, tags=yt_tags)
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
            for p in [json_path, original_path]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    return results, None


# ── Routes ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    success = request.args.get("success")

    account_cards_html = ""
    for slot in ACCOUNT_SLOTS:
        creds = load_credentials(slot)
        logged_in = creds is not None and creds.valid
        name = get_account_name(slot)
        dot_color = "#2ed573" if logged_in else "#ff4757"
        status_text = "Authenticated" if logged_in else "Not authenticated"

        if logged_in:
            card_content = f"""
            <div style="display:flex; gap:8px; margin-bottom:14px;">
                <input type="url" class="input-field" id="url-input-{slot}"
                       placeholder="Paste TikTok URL..." autocomplete="off"
                       style="margin-bottom:0; flex:1;">
                <button type="button"
                        onclick="navigator.clipboard.readText().then(t=>document.getElementById('url-input-{slot}').value=t)"
                        style="background:var(--border-color); border:1px solid var(--border-color);
                               color:white; border-radius:12px; padding:0 14px; cursor:pointer;
                               font-size:16px; white-space:nowrap;" title="Paste">📋</button>
            </div>
            <button class="btn" onclick="submitTikTok('{slot}')">Post</button>
            <br>
            <a href="/logout/{slot}" class="logout-link">Sign Out</a>
            """
        else:
            card_content = f"""
            <a href="/login/{slot}" class="btn btn-login">Login with Google</a>
            """

        account_cards_html += f"""
        <div class="account-card">
            <div class="account-header">
                <div style="display:flex; align-items:center; gap:8px; flex:1;">
                    <span class="pencil-icon" onclick="startRename('{slot}')" title="Rename">✏️</span>
                    <span class="account-label" id="label-{slot}">{name}</span>
                    <div id="rename-form-{slot}" style="display:none; flex:1;">
                        <input type="text" id="rename-input-{slot}" value="{name}"
                               class="rename-input"
                               onkeydown="if(event.key==='Enter') saveRename('{slot}'); if(event.key==='Escape') cancelRename('{slot}');">
                        <button onclick="saveRename('{slot}')" class="rename-btn">✓</button>
                        <button onclick="cancelRename('{slot}')" class="rename-btn cancel">✕</button>
                    </div>
                </div>
                <span style="color:{dot_color}; font-size:12px; white-space:nowrap; margin-left:8px;">● {status_text}</span>
            </div>
            {card_content}
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Shorts Automation</title>
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
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                padding: 20px;
            }}
            .card {{
                background: var(--surface-color);
                border: 1px solid var(--border-color);
                border-radius: 24px;
                padding: 36px 28px;
                width: 100%;
                max-width: 460px;
                backdrop-filter: blur(12px);
                box-shadow: 0 20px 40px rgba(0,0,0,0.4);
            }}
            h1 {{
                font-size: 26px; font-weight: 800;
                margin-bottom: 24px; letter-spacing: -0.5px;
                text-align: center;
            }}
            .account-card {{
                background: rgba(255,255,255,0.03);
                border: 1px solid var(--border-color);
                border-radius: 16px;
                padding: 18px;
                margin-bottom: 14px;
            }}
            .account-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 14px;
            }}
            .account-label {{
                font-weight: 700;
                font-size: 15px;
            }}
            .pencil-icon {{
                cursor: pointer;
                font-size: 13px;
                opacity: 0.5;
                transition: opacity 0.2s;
                user-select: none;
            }}
            .pencil-icon:hover {{ opacity: 1; }}
            .rename-input {{
                background: rgba(0,0,0,0.3);
                border: 1px solid var(--primary);
                border-radius: 8px;
                color: white;
                padding: 4px 10px;
                font-size: 14px;
                font-family: 'Inter', sans-serif;
                font-weight: 700;
                width: 140px;
                outline: none;
            }}
            
            .rename-btn {{
                background: none;
                border: none;
                color: #2ed573;
                cursor: pointer;
                font-size: 14px;
                padding: 0 6px;
                font-weight: 700;
            }}
            .rename-btn.cancel {{ color: #ff4757; }}
            .btn {{
                background: var(--primary);
                color: white;
                border: none;
                padding: 12px 20px;
                border-radius: 12px;
                font-size: 15px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
                text-decoration: none;
                display: inline-block;
                width: 100%;
                text-align: center;
            }}
            .btn:hover {{ background: var(--primary-hover); transform: translateY(-2px); }}
            .btn-login {{ background: #4285F4; }}
            .btn-login:hover {{ background: #2b70e4; }}
            .input-field {{
                width: 100%;
                padding: 13px 16px;
                border-radius: 12px;
                border: 1px solid var(--border-color);
                background: rgba(0,0,0,0.2);
                color: white;
                font-size: 14px;
                font-family: 'Inter', sans-serif;
                outline: none;
                transition: border-color 0.2s;
            }}
            .input-field:focus {{ border-color: var(--primary); }}
            .input-field::placeholder {{ color: #555; }}
            .logout-link {{
                color: var(--text-muted);
                text-decoration: none;
                font-size: 12px;
                margin-top: 10px;
                display: inline-block;
            }}
            .logout-link:hover {{ color: white; }}
            #loader-overlay {{
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(13,15,18,0.92);
                backdrop-filter: blur(8px);
                display: none; flex-direction: column;
                align-items: center; justify-content: center;
                z-index: 1000;
            }}
            .spinner {{
                width: 50px; height: 50px;
                border: 4px solid rgba(255,255,255,0.1);
                border-top-color: var(--primary);
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin-bottom: 20px;
            }}
            @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
            .toast {{
                position: fixed; top: 24px; right: 24px;
                background: rgba(46,213,115,0.15);
                border: 1px solid #2ed573;
                color: #2ed573;
                padding: 14px 22px;
                border-radius: 12px;
                font-weight: 600;
                transform: translateX(130%);
                animation: slideIn 0.4s ease forwards, slideOut 0.4s ease 5s forwards;
                box-shadow: 0 10px 30px rgba(0,0,0,0.3);
                z-index: 999;
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
                Fetching metadata, applying FFmpeg magic, and posting to YouTube...<br>Please wait!
            </p>
        </div>

        <div class="card">
            <h1>Shorts Bot</h1>
            {account_cards_html}
        </div>

        <script>
        function submitTikTok(slot) {{
            const url = document.getElementById('url-input-' + slot).value.trim();
            if (!url) {{ alert('Please paste a TikTok URL first.'); return; }}
            document.getElementById('loader-overlay').style.display = 'flex';
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = '/tiktok';
            [['url', url], ['slot', slot]].forEach(([n, v]) => {{
                const i = document.createElement('input');
                i.type = 'hidden'; i.name = n; i.value = v;
                form.appendChild(i);
            }});
            document.body.appendChild(form);
            form.submit();
        }}

        function startRename(slot) {{
            document.getElementById('label-' + slot).style.display = 'none';
            document.getElementById('rename-form-' + slot).style.display = 'flex';
            document.getElementById('rename-input-' + slot).focus();
            document.getElementById('rename-input-' + slot).select();
        }}

        function cancelRename(slot) {{
            document.getElementById('rename-form-' + slot).style.display = 'none';
            document.getElementById('label-' + slot).style.display = 'inline';
        }}

        function saveRename(slot) {{
            const newName = document.getElementById('rename-input-' + slot).value.trim();
            if (!newName) {{ cancelRename(slot); return; }}
            fetch('/rename', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{slot: slot, name: newName}})
            }})
            .then(r => r.json())
            .then(data => {{
                if (data.ok) {{
                    document.getElementById('label-' + slot).textContent = newName;
                }}
                cancelRename(slot);
            }});
        }}
        </script>
    </body>
    </html>
    """


@app.route("/rename", methods=["POST"])
def rename():
    data = request.get_json()
    slot = data.get("slot")
    name = data.get("name", "").strip()
    if slot not in ACCOUNT_SLOTS or not name:
        return jsonify({"ok": False})
    flask.session.permanent = True
    flask.session[f"name_{slot}"] = name[:30]  # max 30 chars
    return jsonify({"ok": True})


@app.route("/login/<slot>")
def login(slot):
    if slot not in ACCOUNT_SLOTS:
        return "Invalid account slot", 400

    flow = get_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    flask.session["state"] = state
    flask.session["login_slot"] = slot
    flask.session["code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/callback")
def callback():
    state = flask.session.get("state")
    slot = flask.session.get("login_slot", "account_1")

    if not state:
        return "Error: Session state is missing. Please enable cookies and try again.", 400

    flow = get_flow(state=state)
    flow.code_verifier = flask.session.get("code_verifier")

    auth_response = request.url
    if os.environ.get("FLASK_ENV") != "development" and auth_response.startswith("http://"):
        auth_response = auth_response.replace("http://", "https://", 1)

    try:
        flow.fetch_token(authorization_response=auth_response)
    except Exception as e:
        return f"OAuth Error: {str(e)}<br><br>URL used: {auth_response}", 500

    save_credentials(flow.credentials, slot)
    return redirect(url_for("index"))


@app.route("/logout/<slot>")
def logout(slot):
    if slot in ACCOUNT_SLOTS:
        flask.session.pop(f"credentials_{slot}", None)
    return redirect(url_for("index"))


@app.route("/tiktok", methods=["POST"])
def download_tiktok():
    target_url = request.form.get("url")
    slot = request.form.get("slot", "account_1")

    if not target_url:
        return jsonify({"error": "No URL provided"})
    if slot not in ACCOUNT_SLOTS:
        return jsonify({"error": "Invalid account slot"})

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
            print(f"Downloading TikTok: {target_url}")
            ydl.extract_info(target_url, download=True)
            return redirect(url_for("upload", slot=slot))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/upload")
def upload():
    slot = request.args.get("slot", "account_1")

    creds = load_credentials(slot)
    if not creds or not creds.valid:
        return redirect(url_for("login", slot=slot))

    videos_to_upload = []
    for folder in [VIDEO_DIR, TEMP_DIR]:
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                if f.lower().endswith(".mp4") and os.path.isfile(os.path.join(folder, f)):
                    videos_to_upload.append(os.path.join(folder, f))

    if not videos_to_upload:
        return jsonify({"error": "No .mp4 files found."})

    results, err = do_upload_for_slot(slot, videos_to_upload)
    if err == "not_authenticated":
        return redirect(url_for("login", slot=slot))

    if any("error" not in r for r in results):
        return redirect(url_for("index", success=1))

    return jsonify({"errors": results})



# ── Main ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("YouTube Shorts Auto-Upload Bot")
    if os.environ.get("FLASK_ENV") == "development":
        app.run(host="localhost", port=5000, debug=True)
    else:
        app.run(host="0.0.0.0", port=7860)