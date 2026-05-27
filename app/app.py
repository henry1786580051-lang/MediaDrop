import os
import re
import glob
import json
import subprocess
import threading
import signal
import sys
import atexit
import socket

from flask import Flask, request, jsonify, send_file, render_template


def get_base_dir():
    """Return the directory for writable data (config.json, downloads/).

    Frozen (PyInstaller): MEDIADROP_DATA_DIR env var from Electron, or next to executable.
    Dev: directory containing this script.
    """
    if getattr(sys, "frozen", False):
        data_dir = os.environ.get("MEDIADROP_DATA_DIR")
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            return data_dir
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_dir():
    """Return the directory for read-only bundled resources (templates/, static/).

    Frozen: sys._MEIPASS (PyInstaller temp extraction dir).
    Dev: directory containing this script.
    """
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def get_ytdlp_path():
    """Return path to yt-dlp binary. Bundled in frozen mode, system PATH in dev."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
        candidate = os.path.join(exe_dir, name)
        if os.path.exists(candidate):
            return candidate
    return "yt-dlp"


def get_ffmpeg_dir():
    """Return directory containing bundled ffmpeg, or None if not bundled."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        if os.path.exists(os.path.join(exe_dir, name)):
            return exe_dir
    return None


app = Flask(
    __name__,
    template_folder=os.path.join(get_resource_dir(), "templates"),
    static_folder=os.path.join(get_resource_dir(), "static"),
)


@app.errorhandler(BrokenPipeError)
@app.errorhandler(ConnectionResetError)
def handle_broken_pipe(e):
    """Handle broken pipe — occurs when Electron window closes mid-transfer."""
    print(f"[error] Broken pipe: {e}", file=sys.stderr, flush=True)
    return jsonify({"error": "Connection lost. Please try again."}), 400

CONFIG_FILE = os.path.join(get_base_dir(), "config.json")

# In-memory job tracker. Each entry: { status, proc, progress, file, filename, ... }
# Not persisted — jobs are lost on restart, which is acceptable for a desktop app.
jobs = {}


def cleanup_all_jobs():
    """Kill all running yt-dlp processes on app exit."""
    for job_id, job in list(jobs.items()):
        proc = job.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
    print("[cleanup] All subprocesses killed", file=sys.stderr, flush=True)


atexit.register(cleanup_all_jobs)


# Windows pause/resume via NtSuspendProcess/NtResumeProcess (undocumented but stable since NT4)
# SIGSTOP/SIGCONT are Unix-only and not available on Windows.
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _PROCESS_SUSPEND_RESUME = 0x0800

    # Set proper argtypes/restype so 64-bit HANDLE values aren't truncated
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE

    _ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    def _suspend_process(proc):
        handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, proc.pid)
        if not handle:
            print(f"[pause] OpenProcess failed for pid {proc.pid}: last_error={ctypes.get_last_error()}", file=sys.stderr, flush=True)
            return
        _ntdll.NtSuspendProcess(handle)
        _kernel32.CloseHandle(handle)

    def _resume_process(proc):
        handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, proc.pid)
        if not handle:
            print(f"[pause] OpenProcess failed for pid {proc.pid}: last_error={ctypes.get_last_error()}", file=sys.stderr, flush=True)
            return
        _ntdll.NtResumeProcess(handle)
        _kernel32.CloseHandle(handle)
else:
    def _suspend_process(proc):
        proc.send_signal(signal.SIGSTOP)

    def _resume_process(proc):
        proc.send_signal(signal.SIGCONT)


def cleanup_old_jobs():
    """Remove completed/errored jobs older than 100 entries."""
    if len(jobs) > 100:
        done_keys = [k for k, v in jobs.items() if v.get("status") in ("done", "error", "cancelled")]
        for k in done_keys[:len(done_keys) - 50]:
            del jobs[k]


def get_cookie_args(cfg=None):
    """Return yt-dlp cookie arguments based on config."""
    if cfg is None:
        cfg = load_config()
    browser = cfg.get("cookies_browser", "")
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def load_config():
    """Load config from disk, merging with defaults. Returns full dict."""
    default_dir = os.path.join(get_base_dir(), "downloads")
    defaults = {"download_dir": default_dir, "proxy_url": "", "cookies_browser": ""}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            # Merge with defaults so new keys are always present
            return {**defaults, **cfg}
        except Exception:
            pass
    return defaults


def save_config(updates):
    """Merge updates into existing config and write to disk."""
    cfg = load_config()
    cfg.update(updates)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)


def make_unique_filename(directory, base_name, ext):
    """Generate a unique filename by appending (1), (2), etc. if base_name.ext exists."""
    candidate = os.path.join(directory, f"{base_name}{ext}")
    if not os.path.exists(candidate):
        return candidate, f"{base_name}{ext}"
    counter = 1
    while True:
        name = f"{base_name} ({counter}){ext}"
        candidate = os.path.join(directory, name)
        if not os.path.exists(candidate):
            return candidate, name
        counter += 1


def is_proxy_reachable(proxy_url):
    """Check if the proxy server is actually listening. Timeout 1s."""
    try:
        # Parse host:port from URL like "http://127.0.0.1:7897"
        match = re.match(r"https?://([^:/]+):(\d+)", proxy_url)
        if not match:
            return False
        host, port = match.group(1), int(match.group(2))
        with socket.create_connection((host, port), timeout=1):
            return True
    except (OSError, ValueError):
        return False


def get_proxy_url():
    """Get proxy URL from config, returning empty string if proxy is unreachable.

    This prevents yt-dlp from failing when the user has shut down their proxy
    software since the last time MediaDrop was configured.
    """
    cfg = load_config()
    proxy = cfg.get("proxy_url", "") or ""
    if proxy and not is_proxy_reachable(proxy):
        print(f"[proxy] Configured proxy {proxy} is unreachable, using direct connection",
              file=sys.stderr, flush=True)
        return ""
    return proxy


def get_download_dir():
    cfg = load_config()
    d = cfg["download_dir"]
    os.makedirs(d, exist_ok=True)
    return d


def parse_progress(line):
    """Extract download progress from a yt-dlp output line.

    Expected format: " 45.2% of ~123.45MiB at 1.23MiB/s ETA 01:23"
    Returns dict with percent, speed, eta or None if line doesn't match.
    """
    m = re.search(r"(\d+\.?\d*)%\s+of\s+~?\s*([\d.]+\w+i?B)\s+at\s+([\d.]+\w+/s|Unknown\s*\w*/s?)\s+ETA\s+([\d:]+|Unknown)", line)
    if not m:
        return None
    pct = float(m.group(1))
    speed_str = m.group(3) if m.group(3) != "Unknown" else None
    eta_str = m.group(4) if m.group(4) != "Unknown" else None
    return {"percent": pct, "speed": speed_str, "eta": eta_str}


def parse_size(s):
    """Parse human-readable size string ('123.45MiB') to byte count."""
    if not s:
        return 0
    s = s.strip().upper().replace("IB", "B")
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    m = re.match(r"([\d.]+)\s*(\w+)", s)
    if not m:
        return 0
    val, unit = float(m.group(1)), m.group(2)
    return int(val * units.get(unit, 1))


def sanitize_filename(title, ext):
    """Generate a filesystem-safe filename from video title, capped at 80 chars."""
    if not title:
        return None
    safe = re.sub(r'[\\/:*?"<>|]', "", title).strip()[:80].strip()
    return f"{safe}{ext}" if safe else None


def is_valid_url(url):
    """Basic URL validation."""
    return bool(re.match(r'^https?://', url))


def get_ytdlp_env():
    """Build environment for yt-dlp subprocesses.

    Removes PYTHONPATH to avoid interference from Electron's bundled Python paths.
    Sets proxy env vars so yt-dlp can reach video sites through the configured proxy.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proxy = get_proxy_url()
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    return env


def run_download(job_id, url, format_choice, format_id, title):
    job = jobs[job_id]
    download_dir = get_download_dir()
    out_template = os.path.join(download_dir, f"{job_id}.%(ext)s")

    cmd = [get_ytdlp_path(), "--no-playlist", "--newline", "--progress", "-c", "-o", out_template]

    # Pass --proxy flag directly to yt-dlp in addition to env vars,
    # because some yt-dlp extractors ignore env vars and only respect --proxy.
    proxy = get_proxy_url()
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += get_cookie_args()

    # Use bundled ffmpeg if available
    ffmpeg_dir = get_ffmpeg_dir()
    if ffmpeg_dir:
        cmd += ["--ffmpeg-location", ffmpeg_dir]

    # Build format-specific yt-dlp flags
    if format_choice == "image":
        ext = ".jpg"
        cmd += ["--write-thumbnail", "--convert-thumbnails", "jpg", "--skip-download"]
    elif format_choice == "audio":
        ext = ".mp3"
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        # User selected a specific quality — merge video+audio streams
        ext = ".mp4"
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        # Default: best available quality
        ext = ".mp4"
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        print(f"[download] Starting download (format={format_choice})", file=sys.stderr, flush=True)
        last_lines = []
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=get_ytdlp_env())
        job["proc"] = proc
        job["paused"] = False

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            last_lines.append(line)
            if len(last_lines) > 10:
                last_lines.pop(0)
            prog = parse_progress(line)
            if prog and not job.get("paused"):
                size_m = re.search(r"of\s+~?([\d.]+\w+i?B)", line)
                total = parse_size(size_m.group(1)) if size_m else 0
                pct = prog["percent"]
                job["progress"] = {
                    "percent": pct,
                    "speed": prog["speed"],
                    "eta": prog["eta"],
                    "downloaded": int(total * pct / 100) if total else 0,
                    "total": total,
                }
                job["status"] = "downloading"

        proc.wait()

        # Don't overwrite cancelled status — user manually cancelled via UI
        if job.get("status") == "cancelled":
            return

        if proc.returncode != 0:
            job["status"] = "error"
            err_lines = [l for l in last_lines if "ERROR" in l or "error" in l.lower()]
            job["error"] = err_lines[-1] if err_lines else "\n".join(last_lines[-3:])
            print(f"[download] Failed (code {proc.returncode}): {job['error']}", file=sys.stderr, flush=True)
            return

        # Find downloaded file(s) — yt-dlp may produce extra files (.part, .ytdl, etc.)
        pattern = os.path.join(download_dir, f"{job_id}.*")
        files = glob.glob(pattern)
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        # Pick the actual output file by extension
        if format_choice == "audio":
            chosen = next((f for f in files if f.endswith(".mp3")), files[0])
        elif format_choice == "image":
            chosen = next((f for f in files if f.endswith(".jpg")), files[0])
        else:
            chosen = next((f for f in files if f.endswith(".mp4")), files[0])

        # Clean up any extra files (thumbnails, temp files, etc.)
        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        # Rename from job_id to human-readable title with counter for duplicates
        base_name = sanitize_filename(title, "") or job_id
        new_path, friendly_name = make_unique_filename(download_dir, base_name, ext)
        try:
            os.rename(chosen, new_path)
            chosen = new_path
        except OSError:
            # If rename fails, keep the job_id filename
            friendly_name = os.path.basename(chosen)

        job["status"] = "done"
        job["file"] = chosen
        job["filename"] = friendly_name
        job["progress"]["percent"] = 100.0
        job["progress"]["eta"] = None

    except Exception as e:
        if job.get("status") != "cancelled":
            job["status"] = "error"
            job["error"] = str(e)


@app.route("/")
def index():
    resp = render_template("index.html")
    # Prevent browser/Electron from caching the template
    from flask import make_response
    response = make_response(resp)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400

    try:
        cmd = [get_ytdlp_path(), "--no-playlist", "-j", url]
        proxy = get_proxy_url()
        if proxy:
            cmd += ["--proxy", proxy]
        cmd += get_cookie_args()
        ffmpeg_dir = get_ffmpeg_dir()
        if ffmpeg_dir:
            cmd += ["--ffmpeg-location", ffmpeg_dir]
        print("[info] Fetching video info", file=sys.stderr, flush=True)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=get_ytdlp_env())

        if result.returncode != 0:
            err_detail = result.stderr.strip()
            print(f"[info] yt-dlp failed: {err_detail[:200]}", file=sys.stderr, flush=True)
            return jsonify({"error": err_detail.split("\n")[-1]}), 400

        info = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # Deduplicate formats by resolution — keep only the best stream per height.
    # DASH (https) is preferred over HLS (m3u8) because it allows range requests
    # and is significantly faster for seeking/resuming downloads.
    best_by_height = {}
    for f in info.get("formats", []):
        height = f.get("height")
        vcodec = f.get("vcodec") or ""
        if height and vcodec and vcodec != "none":
            proto = f.get("protocol", "")
            tbr = f.get("tbr") or 0
            existing = best_by_height.get(height)
            if not existing:
                best_by_height[height] = f
            elif proto == "https" and existing.get("protocol", "") != "https":
                best_by_height[height] = f
            elif proto == existing.get("protocol", "") and tbr > (existing.get("tbr") or 0):
                best_by_height[height] = f

    formats = [{"id": f["format_id"], "label": f"{h}p", "height": h} for h, f in best_by_height.items()]
    formats.sort(key=lambda x: x["height"], reverse=True)

    return jsonify({
        "title": info.get("title", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration"),
        "uploader": info.get("uploader", ""),
        "formats": formats,
    })


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400

    import time
    cleanup_old_jobs()
    # Generate a readable-enough job ID from URL hash + timestamp
    url_hash = re.sub(r"[^a-zA-Z0-9]", "_", url)[-40:]
    job_id = f"{url_hash}_{int(time.time()*1000)}"
    jobs[job_id] = {
        "status": "starting",
        "url": url,
        "title": title,
        "progress": {"percent": None, "speed": None, "eta": None, "downloaded": 0, "total": 0},
    }

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, title))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    resp = {
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "progress": job.get("progress", {}),
    }
    return jsonify(resp)


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    file_path = job["file"]
    # Path traversal guard: ensure resolved path stays within download directory
    real_path = os.path.realpath(file_path)
    download_dir = os.path.realpath(get_download_dir())
    if not real_path.startswith(download_dir):
        return jsonify({"error": "Access denied"}), 403
    return send_file(real_path, as_attachment=True, download_name=job.get("filename"))


@app.route("/api/pause/<job_id>", methods=["POST"])
def pause_download(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("proc"):
        return jsonify({"error": "Job not found"}), 404
    proc = job["proc"]
    if proc.poll() is not None:
        return jsonify({"error": "Process already finished"}), 400
    try:
        if job.get("paused"):
            _resume_process(proc)
            job["paused"] = False
            job["status"] = "downloading"
            return jsonify({"status": "resumed"})
        else:
            _suspend_process(proc)
            job["paused"] = True
            job["status"] = "paused"
            return jsonify({"status": "paused"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_download(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    proc = job.get("proc")
    if proc and proc.poll() is None:
        proc.kill()
    job["status"] = "cancelled"
    return jsonify({"status": "cancelled"})


VALID_BROWSERS = {"", "chrome", "firefox", "edge", "brave", "opera", "vivaldi"}


@app.route("/api/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        data = request.json
        updates = {}

        new_dir = data.get("download_dir", "").strip()
        if new_dir:
            new_dir = os.path.expanduser(new_dir)
            try:
                os.makedirs(new_dir, exist_ok=True)
            except OSError as e:
                return jsonify({"error": f"Cannot create directory: {e}"}), 400
            updates["download_dir"] = new_dir

        if "proxy_url" in data:
            updates["proxy_url"] = data["proxy_url"].strip()

        if "cookies_browser" in data:
            browser = data["cookies_browser"].strip().lower()
            if browser not in VALID_BROWSERS:
                return jsonify({"error": f"Unsupported browser: {browser}"}), 400
            updates["cookies_browser"] = browser

        if updates:
            save_config(updates)

        cfg = load_config()
        return jsonify({"download_dir": cfg["download_dir"], "proxy_url": cfg["proxy_url"], "cookies_browser": cfg["cookies_browser"]})

    cfg = load_config()
    return jsonify({"download_dir": cfg["download_dir"], "proxy_url": cfg["proxy_url"], "cookies_browser": cfg["cookies_browser"]})


def signal_handler(sig, frame):
    """Handle termination signals."""
    print(f"[signal] Received signal {sig}, cleaning up...", file=sys.stderr, flush=True)
    cleanup_all_jobs()
    sys.exit(0)


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


if __name__ == "__main__":
    # First-run: import proxy from Electron's detectProxy() if config is empty.
    # This way users don't need to manually configure their proxy on first launch.
    env_proxy = os.environ.get("PROXY_URL", "")
    if env_proxy:
        cfg = load_config()
        if not cfg.get("proxy_url"):
            save_config({"proxy_url": env_proxy})
            print(f"[startup] Saved proxy from environment: {env_proxy}", file=sys.stderr, flush=True)

    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"[startup] MediaDrop starting on {host}:{port}", file=sys.stderr, flush=True)
    app.run(host=host, port=port)
