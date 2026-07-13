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
import shutil
import errno
import tempfile
import uuid
import urllib.error
import urllib.request

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


def get_tools_dir():
    d = os.path.join(get_base_dir(), "tools")
    os.makedirs(d, exist_ok=True)
    return d


def get_ytdlp_asset_name():
    if sys.platform == "win32":
        return "yt-dlp.exe"
    if sys.platform == "darwin":
        return "yt-dlp_macos"
    return "yt-dlp"


def get_updatable_ytdlp_path():
    name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    candidate = os.path.join(get_tools_dir(), name)
    if os.path.isfile(candidate):
        return candidate
    return None


def get_bundled_ytdlp_path():
    """Return bundled yt-dlp path if present."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
        candidate = os.path.join(exe_dir, name)
        if os.path.exists(candidate):
            return candidate
    return None


def get_ytdlp_path():
    """Return preferred yt-dlp path: user-updated, bundled, then system PATH."""
    updated = get_updatable_ytdlp_path()
    if updated:
        return updated
    bundled = get_bundled_ytdlp_path()
    if bundled:
        return bundled
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
MAX_CONCURRENT_DOWNLOADS = 2

# In-memory job tracker. Each entry: { status, proc, progress, file, filename, ... }
# Not persisted — jobs are lost on restart, which is acceptable for a desktop app.
jobs = {}
download_queue = []
active_downloads = set()
queue_lock = threading.Lock()
filename_lock = threading.Lock()
CACHE_DIR_NAME = ".mediadrop-cache"
shutdown_event = threading.Event()


def cleanup_all_jobs():
    """Kill running downloads and remove their isolated cache files."""
    shutdown_event.set()
    for job_id, job in list(jobs.items()):
        if job.get("status") not in ("done", "error", "cancelled"):
            job["status"] = "cancelled"
        proc = job.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
    for job in list(jobs.values()):
        thread = job.get("thread")
        if thread and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=3)
    cleanup_cache_root()
    print("[cleanup] Download processes and cache cleared", file=sys.stderr, flush=True)


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
    cookie_file = cfg.get("cookies_file", "")
    if cookie_file:
        cookie_file = os.path.expanduser(cookie_file)
        if os.path.isfile(cookie_file):
            return ["--cookies", cookie_file]
    browser = cfg.get("cookies_browser", "")
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def load_config():
    """Load config from disk, merging with defaults. Returns full dict."""
    default_dir = os.path.join(get_base_dir(), "downloads")
    defaults = {"download_dir": default_dir, "proxy_url": "", "cookies_browser": "", "cookies_file": ""}
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


def get_cache_root():
    """Return the only directory MediaDrop is allowed to delete recursively."""
    base = os.path.realpath(get_base_dir())
    cache_path = os.path.join(base, CACHE_DIR_NAME)
    if os.path.islink(cache_path):
        raise RuntimeError("MediaDrop cache root cannot be a symbolic link")
    os.makedirs(cache_path, exist_ok=True)
    if os.path.islink(cache_path):
        raise RuntimeError("MediaDrop cache root changed during initialization")
    root = os.path.realpath(cache_path)
    if os.path.commonpath([root, base]) != base or root == base:
        raise RuntimeError("MediaDrop cache root escaped the application data directory")
    return root


def _is_within(path, root):
    """Check path containment after resolving symlinks and traversal."""
    try:
        real_root = os.path.realpath(root)
        return os.path.commonpath([os.path.realpath(path), real_root]) == real_root
    except (OSError, ValueError):
        return False


def get_job_cache_dir(job_id):
    """Create an isolated cache directory for one download task."""
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(job_id))[:160]
    if not safe_id or safe_id in (".", ".."):
        raise ValueError("Invalid cache job ID")
    root = get_cache_root()
    job_dir = os.path.join(root, safe_id)
    if not _is_within(job_dir, root) or os.path.realpath(job_dir) == root:
        raise ValueError("Cache path escaped its root")
    os.makedirs(job_dir, exist_ok=True)
    return job_dir


def cleanup_job_cache(job_dir):
    """Delete one task cache without ever following a path outside the cache root."""
    try:
        root = get_cache_root()
    except (OSError, RuntimeError) as exc:
        print(f"[cleanup] Refusing unsafe cache root: {exc}", file=sys.stderr, flush=True)
        return False
    if not job_dir or os.path.realpath(job_dir) == root or not _is_within(job_dir, root):
        return False
    try:
        shutil.rmtree(job_dir)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"[cleanup] Could not remove cache {job_dir}: {exc}", file=sys.stderr, flush=True)
        return False


def cleanup_cache_root():
    """Remove orphaned cache entries; callers stop active jobs first."""
    try:
        root = get_cache_root()
    except (OSError, RuntimeError) as exc:
        print(f"[cleanup] Refusing unsafe cache root: {exc}", file=sys.stderr, flush=True)
        return
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                os.unlink(entry.path)
            else:
                cleanup_job_cache(entry.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[cleanup] Could not remove orphan {entry.path}: {exc}", file=sys.stderr, flush=True)


def cleanup_destination_temp_files(download_dir):
    """Remove only MediaDrop-owned publish temp files left by a hard crash."""
    try:
        entries = list(os.scandir(download_dir))
    except OSError:
        return
    for entry in entries:
        if not (entry.name.startswith(".mediadrop-") and entry.name.endswith(".tmp")):
            continue
        try:
            if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                os.unlink(entry.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[cleanup] Could not remove publish temp {entry.path}: {exc}", file=sys.stderr, flush=True)


def _unique_candidate(directory, base_name, ext, counter):
    filename = f"{base_name}{ext}" if counter == 0 else f"{base_name} ({counter}){ext}"
    return os.path.join(directory, filename), filename


def _link_without_overwrite(source, directory, base_name, ext):
    """Atomically expose source under a unique user-facing filename."""
    counter = 0
    while True:
        candidate, filename = _unique_candidate(directory, base_name, ext, counter)
        try:
            os.link(source, candidate)
            return candidate, filename
        except FileExistsError:
            counter += 1


def finalize_cached_file(source, download_dir, title, ext, job_id):
    """Commit a completed cache file without exposing a partial destination file."""
    cache_root = get_cache_root()
    source = os.path.realpath(source)
    if not _is_within(source, cache_root) or not os.path.isfile(source):
        raise ValueError("Completed file is outside the MediaDrop cache")

    os.makedirs(download_dir, exist_ok=True)
    base_name = sanitize_filename(title, "") or str(job_id)

    # Hard links are atomic, cannot overwrite existing files, and avoid copying
    # when app data and the download folder share a filesystem.
    try:
        with filename_lock:
            final_path, friendly_name = _link_without_overwrite(source, download_dir, base_name, ext)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP):
            raise
    else:
        try:
            os.unlink(source)
        except OSError:
            pass
        return final_path, friendly_name

    # Cross-filesystem fallback: copy to a hidden destination-side temp file,
    # flush it, then publish it atomically.
    safe_job_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(job_id))[:48]
    fd, temp_path = tempfile.mkstemp(prefix=f".mediadrop-{safe_job_id}-", suffix=".tmp", dir=download_dir)
    try:
        with os.fdopen(fd, "wb") as target, open(source, "rb") as cached:
            shutil.copyfileobj(cached, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        try:
            with filename_lock:
                final_path, friendly_name = _link_without_overwrite(temp_path, download_dir, base_name, ext)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES, errno.ENOTSUP):
                raise
            # Filesystems without hard-link support fall back to an atomic replace
            # while holding the process-wide filename lock.
            with filename_lock:
                final_path, friendly_name = make_unique_filename(download_dir, base_name, ext)
                if os.path.exists(final_path):
                    raise FileExistsError(final_path)
                os.replace(temp_path, final_path)
                temp_path = None
        try:
            os.unlink(source)
        except OSError:
            pass
        return final_path, friendly_name
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


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


def run_ytdlp_version(path):
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20, env=get_ytdlp_env())
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def get_latest_ytdlp_release():
    req = urllib.request.Request(
        "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "MediaDrop"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {
        "version": data.get("tag_name", ""),
        "name": data.get("name", ""),
        "url": data.get("html_url", ""),
        "published_at": data.get("published_at", ""),
    }


def compare_version_strings(a, b):
    def parts(v):
        return [int(x) for x in re.findall(r"\d+", v or "")]
    aa, bb = parts(a), parts(b)
    n = max(len(aa), len(bb))
    aa += [0] * (n - len(aa))
    bb += [0] * (n - len(bb))
    return (aa > bb) - (aa < bb)


def get_ytdlp_versions():
    current_path = get_ytdlp_path()
    current = run_ytdlp_version(current_path)
    bundled = run_ytdlp_version(get_bundled_ytdlp_path()) if get_bundled_ytdlp_path() else None
    updated = run_ytdlp_version(get_updatable_ytdlp_path()) if get_updatable_ytdlp_path() else None
    latest = get_latest_ytdlp_release()
    latest_version = latest.get("version", "")
    return {
        "current": current,
        "latest": latest_version,
        "bundled": bundled,
        "updated": updated,
        "using_updated": bool(get_updatable_ytdlp_path()),
        "update_available": bool(current and latest_version and compare_version_strings(latest_version, current) > 0),
        "release_url": latest.get("url", ""),
        "published_at": latest.get("published_at", ""),
        "path": current_path,
    }


def download_latest_ytdlp():
    latest = get_latest_ytdlp_release()
    asset_name = get_ytdlp_asset_name()
    url = f"https://github.com/yt-dlp/yt-dlp/releases/latest/download/{asset_name}"
    final_name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    tools_dir = get_tools_dir()
    tmp_path = os.path.join(tools_dir, f"{final_name}.download")
    final_path = os.path.join(tools_dir, final_name)
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    if sys.platform != "win32":
        os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, final_path)
    version = run_ytdlp_version(final_path)
    return {
        "version": version,
        "latest": latest.get("version", ""),
        "path": final_path,
        "release_url": latest.get("url", ""),
    }


def process_download_queue():
    with queue_lock:
        if shutdown_event.is_set():
            return
        while download_queue and len(active_downloads) < MAX_CONCURRENT_DOWNLOADS:
            job_id = download_queue.pop(0)
            job = jobs.get(job_id)
            if not job or job.get("status") != "queued":
                continue
            active_downloads.add(job_id)
            job["status"] = "starting"
            thread = threading.Thread(
                target=run_download,
                args=(
                    job_id,
                    job["url"],
                    job["format"],
                    job.get("format_id"),
                    job.get("title", ""),
                    job.get("video_range_mode", "auto"),
                ),
            )
            thread.daemon = True
            job["thread"] = thread
            thread.start()


def run_download(job_id, url, format_choice, format_id, title, video_range_mode="auto"):
    job = jobs[job_id]
    download_dir = get_download_dir()
    try:
        job_cache_dir = get_job_cache_dir(job_id)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"Could not create download cache: {exc}"
        with queue_lock:
            active_downloads.discard(job_id)
        process_download_queue()
        return
    job["cache_dir"] = job_cache_dir
    out_template = os.path.join(job_cache_dir, f"{job_id}.%(ext)s")

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
        ext = ".mp4"
        if video_range_mode == "hdr":
            selector = "bestvideo[dynamic_range!=SDR]+bestaudio/bestvideo+bestaudio/best"
        elif video_range_mode == "sdr":
            selector = "bestvideo[dynamic_range=SDR]+bestaudio/bestvideo+bestaudio/best"
        else:
            selector = "bestvideo+bestaudio/best"
        cmd += ["-f", selector, "--merge-output-format", "mp4"]

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
        pattern = os.path.join(job_cache_dir, f"{job_id}.*")
        files = glob.glob(pattern)
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        # Pick the actual output file by extension
        chosen = next((f for f in files if f.lower().endswith(ext)), None)
        if not chosen:
            job["status"] = "error"
            job["error"] = f"Download completed without the expected {ext} file"
            return

        # Publish only the fully completed media file. Partials and intermediate
        # streams remain isolated in the task cache and are removed in finally.
        chosen, friendly_name = finalize_cached_file(
            chosen, download_dir, title, ext, job_id
        )

        job["status"] = "done"
        job["file"] = chosen
        job["filename"] = friendly_name
        job["progress"]["percent"] = 100.0
        job["progress"]["eta"] = None

    except Exception as e:
        if job.get("status") != "cancelled":
            job["status"] = "error"
            job["error"] = str(e)
    finally:
        cleanup_job_cache(job_cache_dir)
        with queue_lock:
            active_downloads.discard(job_id)
        if not shutdown_event.is_set():
            process_download_queue()


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


def describe_dynamic_range(fmt):
    dynamic_range = str(fmt.get("dynamic_range") or "").strip()
    note = str(fmt.get("format_note") or "")
    transfer = str(fmt.get("color_transfer") or "").lower()
    combined = f"{dynamic_range} {note}".upper()

    if "DOLBY VISION" in combined or re.search(r"\bDV\b", combined):
        return True, "Dolby Vision"
    if "HDR10+" in combined:
        return True, "HDR10+"
    if "HLG" in combined or "arib-std-b67" in transfer:
        return True, "HLG"
    if "HDR10" in combined or "PQ" in combined or "smpte2084" in transfer:
        return True, "HDR10"
    if dynamic_range and dynamic_range.upper() not in ("SDR", "UNKNOWN"):
        return True, dynamic_range.upper()
    if "HDR" in combined:
        return True, "HDR"
    return False, "SDR"


def is_better_format(candidate, existing):
    candidate_https = candidate.get("protocol", "") == "https"
    existing_https = existing.get("protocol", "") == "https"
    if candidate_https != existing_https:
        return candidate_https
    return (candidate.get("tbr") or 0) > (existing.get("tbr") or 0)


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
            # Provide helpful message for Safari cookie sandbox issue
            if "Operation not permitted" in err_detail and "Safari" in err_detail:
                return jsonify({"error": "Safari is not supported due to macOS sandbox restrictions. Open Chrome or Firefox, log into YouTube, then select that browser in Settings > Cookies."}), 400
            return jsonify({"error": err_detail.split("\n")[-1]}), 400

        info = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # Keep one best stream for each resolution and dynamic-range variant.
    best_by_variant = {}
    for f in info.get("formats", []):
        height = f.get("height")
        vcodec = f.get("vcodec") or ""
        if height and vcodec and vcodec != "none":
            is_hdr, range_label = describe_dynamic_range(f)
            key = (height, range_label)
            existing = best_by_variant.get(key)
            if not existing or is_better_format(f, existing):
                enriched = dict(f)
                enriched["_is_hdr"] = is_hdr
                enriched["_range_label"] = range_label
                best_by_variant[key] = enriched

    formats = []
    for (height, _), f in best_by_variant.items():
        formats.append({
            "id": f["format_id"],
            "label": f"{height}p {f['_range_label']}",
            "height": height,
            "hdr": f["_is_hdr"],
            "dynamic_range": f["_range_label"],
            "vcodec": f.get("vcodec", ""),
            "ext": f.get("ext", ""),
        })
    formats.sort(key=lambda x: (x["height"], x["hdr"]), reverse=True)

    auto_format = formats[0]["id"] if formats else None
    hdr_format = next((f["id"] for f in formats if f["hdr"]), auto_format)
    sdr_format = next((f["id"] for f in formats if not f["hdr"]), auto_format)

    return jsonify({
        "title": info.get("title", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration"),
        "uploader": info.get("uploader", ""),
        "formats": formats,
        "default_format_ids": {"auto": auto_format, "hdr": hdr_format, "sdr": sdr_format},
        "has_hdr": any(f["hdr"] for f in formats),
    })


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    video_range_mode = data.get("video_range_mode", "auto")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400
    if video_range_mode not in ("auto", "hdr", "sdr"):
        return jsonify({"error": "Invalid video range mode"}), 400

    import time
    cleanup_old_jobs()
    # Generate a readable-enough job ID from URL hash + timestamp
    url_hash = re.sub(r"[^a-zA-Z0-9]", "_", url)[-40:]
    job_id = f"{url_hash}_{time.time_ns()}_{uuid.uuid4().hex[:8]}"
    jobs[job_id] = {
        "status": "queued",
        "url": url,
        "format": format_choice,
        "format_id": format_id,
        "video_range_mode": video_range_mode,
        "title": title,
        "progress": {"percent": None, "speed": None, "eta": None, "downloaded": 0, "total": 0},
    }

    with queue_lock:
        download_queue.append(job_id)
    process_download_queue()

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
    if not _is_within(real_path, download_dir):
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
    start_next = False
    with queue_lock:
        if job_id in download_queue:
            download_queue.remove(job_id)
            start_next = True
        elif job_id not in active_downloads:
            start_next = True
    job["status"] = "cancelled"
    # Active tasks keep their concurrency slot until the worker observes the
    # terminated process and runs its cache cleanup in finally.
    if start_next:
        process_download_queue()
    return jsonify({"status": "cancelled"})


@app.route("/api/queue")
def queue_state():
    with queue_lock:
        return jsonify({
            "queued": list(download_queue),
            "active": list(active_downloads),
            "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
        })


@app.route("/api/ytdlp/version")
def ytdlp_version():
    try:
        return jsonify(get_ytdlp_versions())
    except urllib.error.URLError as e:
        return jsonify({"error": f"Could not check latest yt-dlp release: {e.reason}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/ytdlp/update", methods=["POST"])
def ytdlp_update():
    try:
        return jsonify(download_latest_ytdlp())
    except urllib.error.URLError as e:
        return jsonify({"error": f"Could not download yt-dlp: {e.reason}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/test-cookies", methods=["POST"])
def test_cookies():
    data = request.json or {}
    url = data.get("url", "").strip() or "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400

    cmd = [get_ytdlp_path(), "--no-playlist", "--simulate", "--skip-download", "--dump-json", url]
    proxy = get_proxy_url()
    if proxy:
        cmd += ["--proxy", proxy]
    cookie_args = get_cookie_args()
    cmd += cookie_args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=get_ytdlp_env())
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timed out testing cookies"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        return jsonify({"ok": False, "error": err.split("\n")[-1], "using_cookies": bool(cookie_args)}), 400

    try:
        info = json.loads(result.stdout)
    except Exception:
        info = {}
    return jsonify({
        "ok": True,
        "using_cookies": bool(cookie_args),
        "title": info.get("title", ""),
        "extractor": info.get("extractor", ""),
    })


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

        if "cookies_file" in data:
            cookie_file = os.path.expanduser(data["cookies_file"].strip())
            if cookie_file and not os.path.isfile(cookie_file):
                return jsonify({"error": "Cookie file does not exist"}), 400
            updates["cookies_file"] = cookie_file

        if updates:
            save_config(updates)

        cfg = load_config()
        return jsonify({
            "download_dir": cfg["download_dir"],
            "proxy_url": cfg["proxy_url"],
            "cookies_browser": cfg["cookies_browser"],
            "cookies_file": cfg["cookies_file"],
        })

    cfg = load_config()
    return jsonify({
        "download_dir": cfg["download_dir"],
        "proxy_url": cfg["proxy_url"],
        "cookies_browser": cfg["cookies_browser"],
        "cookies_file": cfg["cookies_file"],
    })


def signal_handler(sig, frame):
    """Handle termination signals."""
    print(f"[signal] Received signal {sig}, cleaning up...", file=sys.stderr, flush=True)
    cleanup_all_jobs()
    sys.exit(0)


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


if __name__ == "__main__":
    # Jobs are intentionally in-memory only, so every cache entry left from a
    # previous process is orphaned and safe to remove before accepting requests.
    cleanup_cache_root()
    cleanup_destination_temp_files(get_download_dir())

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
