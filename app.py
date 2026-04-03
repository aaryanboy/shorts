import os
import pickle
import flask
from flask import Flask, redirect, request, url_for, jsonify
from google.auth.transport.requests import Request
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
import tempfile
import subprocess
import shutil
import yt_dlp
import json
import re

# ── Config ──────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "shorts-automation-secret-key"

# Allow HTTP for local dev (remove in production)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRETS_FILE = "credentials.json"   # OAuth client secret from Google Cloud
TOKEN_FILE = "token.pkl"                    # Persisted credentials (login once)
VIDEO_DIR = "videos"                        # Folder containing .mp4 files
TEMP_DIR = os.path.join(VIDEO_DIR, "temp")  # Folder for downloaded TikToks

# Ensure directories exist
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)


def clean_error_message(msg):
    """Remove ANSI escape sequences (colors) from error messages."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', msg).strip()


# ── Helpers ─────────────────────────────────────────────────────────
def load_credentials():
    """Load saved credentials from token.pkl. Returns None if not found or invalid."""
    if not os.path.exists(TOKEN_FILE):
        return None

    with open(TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
        except Exception:
            return None

    return creds


def save_credentials(creds):
    """Persist credentials to token.pkl."""
    with open(TOKEN_FILE, "wb") as f:
        pickle.dump(creds, f)


def get_youtube_client(creds):
    """Build and return an authenticated YouTube API client."""
    return build("youtube", "v3", credentials=creds)


def get_ffmpeg_exe():
    """Find ffmpeg executable from system PATH."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    # Fallback: try imageio_ffmpeg if installed
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"  # Hope it's on PATH


def get_ffprobe_exe():
    """Find ffprobe executable from system PATH."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    # Derive from ffmpeg path
    ffmpeg = get_ffmpeg_exe()
    ffprobe_path = ffmpeg.replace("ffmpeg", "ffprobe")
    if os.path.exists(ffprobe_path):
        return ffprobe_path
    return "ffprobe"


def probe_video(video_path):
    """Use ffprobe to get video duration and dimensions. Returns (duration, width, height)."""
    try:
        cmd = [
            get_ffprobe_exe(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_entries", "format=duration:stream=width,height",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        
        duration = float(data["format"]["duration"])
        # Find the video stream with width/height
        w, h = 0, 0
        for stream in data.get("streams", []):
            if "width" in stream and "height" in stream:
                w = int(stream["width"])
                h = int(stream["height"])
                break
        return duration, w, h
    except Exception as e:
        print(f"  ffprobe failed: {e}")
        return None, 0, 0


def trim_video_for_shorts(video_path, max_duration=178.0, duration=None, width=None, height=None):
    """
    Checks the video duration and aspect ratio.
    If duration/width/height are provided (e.g. from yt-dlp), uses those directly.
    Otherwise falls back to ffprobe. Trims/pads via FFmpeg if needed.
    Returns (path_to_upload, is_temp_file).
    """
    try:
        # Use provided metadata or fall back to ffprobe
        if duration is None or width is None or height is None:
            duration, width, height = probe_video(video_path)
            if duration is None:
                print("  Could not read video metadata, uploading as-is.")
                return video_path, False

        aspect_ratio = width / height if height > 0 else 1
        needs_padding = abs(aspect_ratio - (9/16)) > 0.05

        if duration > max_duration or needs_padding:
            temp_fd, temp_path = tempfile.mkstemp(suffix=".mp4")
            os.close(temp_fd)

            ffmpeg_exe = get_ffmpeg_exe()
            cmd = [ffmpeg_exe, "-y", "-i", video_path]

            if duration > max_duration:
                cmd.extend(["-t", str(max_duration)])

            if needs_padding:
                print(f"  Video is {width}x{height} (not 9:16). Encoding with black bars...")
                cmd.extend([
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac"
                ])
            else:
                print(f"  Video is {duration:.1f}s. Stream-copying first {max_duration}s...")
                cmd.extend(["-c", "copy"])

            cmd.append(temp_path)
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return temp_path, True

        return video_path, False
    except Exception as e:
        print(f"  Error processing video with FFmpeg: {e}")
        return video_path, False


def upload_video(youtube, video_path, title="", description="", tags=None, privacy="public"):
    """Upload a single video to YouTube as a Short."""
    if not os.path.isfile(video_path):
        return {"error": f"File not found: {video_path}"}

    # Default metadata for Shorts
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
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)

    req = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            print(f"  Uploading… {int(status.progress() * 100)}%")

    video_id = response["id"]
    # Provide the explicit Shorts URL format
    url = f"https://youtube.com/shorts/{video_id}"
    print(f"  ✓ Uploaded: {url}")
    return {"id": video_id, "url": url}


# ── Routes ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    creds = load_credentials()
    logged_in = creds is not None and creds.valid
    success = request.args.get("success")
    error = request.args.get("error")
    
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
                --surface-color: rgba(255, 255, 255, 0.05);
                --border-color: rgba(255, 255, 255, 0.1);
                --primary: #f22a5c;
                --primary-hover: #d21c48;
                --text-main: #ffffff;
                --text-muted: #8892b0;
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: 'Inter', sans-serif;
                background-color: var(--bg-color);
                color: var(--text-main);
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                padding: 20px;
                background: radial-gradient(circle at top right, #1f1122 0%, var(--bg-color) 60%);
            }}
            .card {{
                background: var(--surface-color);
                border: 1px solid var(--border-color);
                border-radius: 24px;
                padding: 40px 30px;
                width: 100%;
                max-width: 420px;
                text-align: center;
                backdrop-filter: blur(12px);
                box-shadow: 0 20px 40px rgba(0,0,0,0.4);
            }}
            h1 {{ font-size: 28px; font-weight: 800; margin-bottom: 8px; letter-spacing: -0.5px; }}
            p.status {{ color: var(--text-muted); font-size: 14px; margin-bottom: 30px; }}
            
            .btn {{
                background: var(--primary);
                color: white;
                border: none;
                padding: 14px 24px;
                border-radius: 12px;
                font-size: 16px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
                text-decoration: none;
                display: inline-block;
                width: 100%;
            }}
            .btn:hover {{ background: var(--primary-hover); transform: translateY(-2px); }}
            
            .btn-login {{
                background: #4285F4;
                margin-top: 10px;
            }}
            .btn-login:hover {{ background: #2b70e4; }}

            .input-field {{
                width: 100%;
                padding: 16px 20px;
                border-radius: 12px;
                border: 1px solid var(--border-color);
                background: rgba(0,0,0,0.2);
                color: white;
                font-size: 15px;
                margin-bottom: 20px;
                font-family: 'Inter', sans-serif;
                outline: none;
                transition: border-color 0.2s;
            }}
            .input-field:focus {{ border-color: var(--primary); }}
            .input-field::placeholder {{ color: #666; }}

            .logout-link {{
                color: var(--text-muted);
                text-decoration: none;
                font-size: 13px;
                margin-top: 20px;
                display: inline-block;
                transition: color 0.2s;
            }}
            .logout-link:hover {{ color: white; }}
            
            /* Loader Overlay */
            #loader-overlay {{
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(13, 15, 18, 0.9);
                backdrop-filter: blur(8px);
                display: none; flex-direction: column; align-items: center; justify-content: center;
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
            
            /* Toast Notification */
            .toast {{
                position: fixed; top: 30px; right: 30px;
                background: rgba(46, 213, 115, 0.15);
                border: 1px solid #2ed573;
                color: #2ed573;
                padding: 16px 24px;
                border-radius: 12px;
                font-weight: 600;
                transform: translateX(120%);
                animation: slideIn 0.5s ease forwards, slideOut 0.5s ease 5s forwards;
                box-shadow: 0 10px 30px rgba(0,0,0,0.3);
                z-index: 999;
            }}
            @keyframes slideIn {{ to {{ transform: translateX(0); }} }}
            @keyframes slideOut {{ to {{ transform: translateX(120%); }} }}

            .toast.error {{
                background: rgba(255, 71, 87, 0.15);
                border: 1px solid #ff4757;
                color: #ff4757;
            }}
        </style>
    </head>
    <body>
        { '<div class="toast">✅ Successfully uploaded </div>' if success else '' }
        { f'<div class="toast error">❌ {error}</div>' if error else '' }

        <div id="loader-overlay">
            <div class="spinner"></div>
            <h3 style="margin-bottom: 8px;">Processing Video</h3>
            <p style="color:var(--text-muted); font-size:14px; text-align:center; max-width:80%;">
                Fetching metadata, injecting stream bytes, applying FFmpeg magic, and posting to YouTube...<br>Please wait!
            </p>
        </div>

        <div class="card">
            <h1>Shorts Bot</h1>
            <p class="status">● {"Authenticated" if logged_in else "Not authenticated"}</p>
            
            {f'''
            <form action="/tiktok" method="POST" onsubmit="document.getElementById('loader-overlay').style.display='flex'">
                <div style="display:flex; gap:8px; margin-bottom:20px;">
                    <input type="url" class="input-field" id="url-input" name="url" placeholder="Paste TikTok URL..." required autocomplete="off" style="margin-bottom:0;">
                    <button type="button" onclick="navigator.clipboard.readText().then(t=>document.getElementById('url-input').value=t)" style="background:var(--border-color); border:1px solid var(--border-color); color:white; border-radius:12px; padding:0 16px; cursor:pointer; font-size:18px; white-space:nowrap;" title="Paste">📋</button>
                </div>
                <button type="submit" class="btn">Post</button>
            </form>
            ''' if logged_in else "<a href='/login' class='btn btn-login'>Login with Google</a>"}
            
            { "<a href='/logout' class='logout-link'>Sign Out</a>" if logged_in else "" }
        </div>
    </body>
    </html>
    """


@app.route("/login")
def login():
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES
    )
    flow.redirect_uri = url_for("callback", _external=True)

    auth_url, state = flow.authorization_url(
        access_type="offline",       # Get refresh token
        include_granted_scopes="true",
        prompt="consent",            # Force consent to get refresh token
    )

    flask.session["state"] = state
    # Store code verifier for PKCE (required by Google)
    flask.session["code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/callback")
def callback():
    state = flask.session.get("state")
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, state=state
    )
    flow.redirect_uri = url_for("callback", _external=True)

    # Restore PKCE code verifier from session
    flow.code_verifier = flask.session.get("code_verifier")

    # Exchange auth code for credentials
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_credentials(creds)

    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    return redirect(url_for("index"))


@app.route("/tiktok", methods=["POST"])
def download_tiktok():
    target_url = request.form.get("url")
    if not target_url:
        return redirect(url_for("index", error="No URL provided"))

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(TEMP_DIR, '%(id)s.%(ext)s'),
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"Downloading TikTok: {target_url}")
            info = ydl.extract_info(target_url, download=True)
            filename = ydl.prepare_filename(info)

            # Grab everything from yt-dlp's info dict directly — no .info.json needed
            raw_title = info.get("title", "")
            yt_title = (raw_title[:90] + "...") if len(raw_title) > 95 else raw_title
            if not yt_title:
                yt_title = "TikTok Video"

            flask.session["dl_meta"] = {
                "file": filename,
                "duration": info.get("duration"),
                "width": info.get("width"),
                "height": info.get("height"),
                "title": yt_title,
                "description": info.get("description", raw_title),
                "tags": info.get("tags", []),
            }

            return redirect(url_for("upload"))
    except Exception as e:
        error_msg = clean_error_message(str(e))
        return redirect(url_for("index", error=error_msg))


@app.route("/upload")
def upload():
    creds = load_credentials()
    if not creds or not creds.valid:
        return redirect(url_for("login"))

    youtube = get_youtube_client(creds)
    dl_meta = flask.session.pop("dl_meta", None)

    # ── Path A: Single file from TikTok download (metadata already in session) ──
    if dl_meta and dl_meta.get("file") and os.path.isfile(dl_meta["file"]):
        original_path = dl_meta["file"]
        print(f"Processing: {os.path.basename(original_path)}")

        upload_path, is_temp = trim_video_for_shorts(
            original_path,
            duration=dl_meta.get("duration"),
            width=dl_meta.get("width"),
            height=dl_meta.get("height"),
        )

        try:
            upload_video(
                youtube, upload_path,
                title=dl_meta.get("title", "TikTok Video"),
                description=dl_meta.get("description", ""),
                tags=dl_meta.get("tags", []),
            )
        except HttpError as e:
            error_msg = clean_error_message(str(e))
            return redirect(url_for("index", error=error_msg))
        finally:
            # Clean up temp trimmed file
            if is_temp and os.path.exists(upload_path):
                os.remove(upload_path)
            # Clean up downloaded file
            if os.path.exists(original_path):
                os.remove(original_path)

        return redirect(url_for("index", success=1))

    # ── Path B: Manual uploads — scan videos/ folder ──
    videos_to_upload = [
        os.path.join(VIDEO_DIR, f)
        for f in os.listdir(VIDEO_DIR)
        if f.lower().endswith(".mp4") and os.path.isfile(os.path.join(VIDEO_DIR, f))
    ] if os.path.isdir(VIDEO_DIR) else []

    if not videos_to_upload:
        error_msg = f"No .mp4 files found in '{VIDEO_DIR}/'. For TikTok, use the home page form."
        return redirect(url_for("index", error=error_msg))

    results = []
    for original_path in videos_to_upload:
        vid = os.path.basename(original_path)
        print(f"Processing: {vid}")

        # Use filename as title for manual uploads
        yt_title = os.path.splitext(vid)[0]
        upload_path, is_temp = trim_video_for_shorts(original_path)

        try:
            result = upload_video(youtube, upload_path, title=yt_title)
            results.append({"file": vid, **result})
        except HttpError as e:
            results.append({"file": vid, "error": str(e)})
        finally:
            if is_temp and os.path.exists(upload_path):
                os.remove(upload_path)

    if any("error" not in r for r in results):
        return redirect(url_for("index", success=1))
    
    # If everything failed, collect errors and redirect
    combined_errors = " | ".join([clean_error_message(r["error"]) for r in results if "error" in r])
    return redirect(url_for("index", error=combined_errors or "Upload failed"))


# ── Main ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("YouTube Shorts Auto-Upload Bot")
    print(f"Server starting on http://0.0.0.0:{port}")
    app.run("0.0.0.0", port=port, debug=True)
