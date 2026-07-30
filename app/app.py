import os
import re
import glob
import hashlib
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
import math
import platform
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from flask import Flask, request, jsonify, send_file, render_template, redirect, make_response

try:
    from job_store import JobStore
except ImportError:
    from app.job_store import JobStore


def get_base_dir():
    """Return the directory for writable data (config.json, downloads/).

    MEDIADROP_DATA_DIR can override the location for packaged or development runs.
    Frozen fallback: next to executable. Development fallback: this script's directory.
    """
    data_dir = os.environ.get("MEDIADROP_DATA_DIR")
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        return os.path.realpath(data_dir)
    if getattr(sys, "frozen", False):
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


def select_ytdlp_asset_name(platform_name, machine):
    if platform_name == "win32":
        if str(machine or "").lower() in ("arm64", "aarch64"):
            return "yt-dlp_arm64.exe"
        return "yt-dlp.exe"
    if platform_name == "darwin":
        return "yt-dlp_macos"
    return "yt-dlp"


def get_ytdlp_asset_name():
    return select_ytdlp_asset_name(sys.platform, platform.machine())


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

DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
API_TOKEN = os.environ.get("MEDIADROP_API_TOKEN", "")

# Runtime task objects live in memory; serializable state is journaled to SQLite.
jobs = {}
download_queue = []
active_downloads = set()
queue_lock = threading.Lock()
filename_lock = threading.Lock()
store_lock = threading.Lock()
CACHE_DIR_NAME = ".mediadrop-cache"
shutdown_event = threading.Event()
_job_store = None
_job_store_path = None


def get_job_store():
    global _job_store, _job_store_path
    path = os.path.join(get_base_dir(), "jobs.sqlite3")
    with store_lock:
        if _job_store is None or _job_store_path != os.path.realpath(path):
            _job_store = JobStore(path)
            _job_store_path = os.path.realpath(path)
        return _job_store


def persist_job(job_id, force=False):
    job = jobs.get(job_id)
    if not job:
        return
    now = time.monotonic()
    if not force and now - job.get("_last_persist", 0) < 2:
        return
    job.setdefault("created_at", time.time())
    get_job_store().save(job_id, job)
    job["_last_persist"] = now


def job_payload(job_id, job):
    progress = job.get("progress") or empty_progress()
    return {
        "id": job_id,
        "status": job.get("status", "unknown"),
        "url": job.get("url", ""),
        "format": job.get("format", "video"),
        "format_id": job.get("format_id"),
        "video_range_mode": job.get("video_range_mode", "auto"),
        "title": job.get("title", ""),
        "preset": job.get("preset", "recommended"),
        "options": job.get("options") or {},
        "progress": progress,
        "filename": job.get("filename"),
        "file": job.get("file") if job.get("file") and os.path.isfile(job.get("file")) else None,
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
        "resumed": bool(job.get("resumed")),
    }


@app.before_request
def require_local_api_token():
    if not API_TOKEN or not request.path.startswith("/api/"):
        return None
    supplied = request.headers.get("X-MediaDrop-Token") or request.cookies.get("mediadrop_token")
    if supplied != API_TOKEN:
        return jsonify({"error": "Unauthorized local API request"}), 403
    return None


@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: http: https:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


def cleanup_all_jobs():
    """Stop child processes while preserving resumable task caches."""
    if shutdown_event.is_set():
        return
    shutdown_event.set()
    for job_id, job in list(jobs.items()):
        if job.get("status") not in ("done", "error", "cancelled", "missing"):
            job["status"] = "interrupted"
            job["error"] = None
            persist_job(job_id, force=True)
        proc = job.get("proc")
        if proc and proc.poll() is None:
            try:
                terminate_process_tree(proc)
                proc.wait(timeout=3)
            except Exception:
                pass
    for job in list(jobs.values()):
        thread = job.get("thread")
        if thread and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=3)
    print("[cleanup] Download processes stopped; resumable caches preserved", file=sys.stderr, flush=True)


atexit.register(cleanup_all_jobs)


def descendant_process_ids(root_pid, process_pairs):
    """Return all descendants from (pid, parent_pid) process pairs."""
    children = {}
    for pid, parent_pid in process_pairs:
        children.setdefault(parent_pid, []).append(pid)
    result = []
    pending = list(children.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        result.append(pid)
        pending.extend(children.get(pid, ()))
    return result


def set_processes_suspended(process_ids, suspended, setter):
    """Apply a Windows suspend state consistently across a process tree."""
    changed = []
    failed = []
    for pid in process_ids:
        if setter(pid, suspended):
            changed.append(pid)
            continue
        failed.append(pid)
        if suspended:
            for changed_pid in reversed(changed):
                setter(changed_pid, False)
            break
    if failed:
        action = "suspend" if suspended else "resume"
        raise OSError(f"Could not {action} download process tree ({', '.join(map(str, failed))})")


# Windows pause/resume via NtSuspendProcess/NtResumeProcess (undocumented but stable since NT4)
# SIGSTOP/SIGCONT are Unix-only and not available on Windows.
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _PROCESS_SUSPEND_RESUME = 0x0800
    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    # Set proper argtypes/restype so 64-bit HANDLE values aren't truncated
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtSuspendProcess.restype = wintypes.LONG
    _ntdll.NtResumeProcess.restype = wintypes.LONG

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL

    def _windows_process_pairs():
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot == _INVALID_HANDLE_VALUE:
            return []
        pairs = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while has_entry:
                pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                has_entry = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        return pairs

    def _set_windows_process_suspended(pid, suspended):
        handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            return False
        try:
            operation = _ntdll.NtSuspendProcess if suspended else _ntdll.NtResumeProcess
            return operation(handle) == 0
        finally:
            _kernel32.CloseHandle(handle)

    def _suspend_process(proc):
        descendants = descendant_process_ids(proc.pid, _windows_process_pairs())
        set_processes_suspended(descendants + [proc.pid], True, _set_windows_process_suspended)

    def _resume_process(proc):
        descendants = descendant_process_ids(proc.pid, _windows_process_pairs())
        set_processes_suspended([proc.pid] + descendants, False, _set_windows_process_suspended)
else:
    def _suspend_process(proc):
        os.killpg(os.getpgid(proc.pid), signal.SIGSTOP)

    def _resume_process(proc):
        os.killpg(os.getpgid(proc.pid), signal.SIGCONT)


def terminate_process_tree(proc):
    """Terminate yt-dlp and any ffmpeg children it started."""
    if not proc or proc.poll() is not None:
        return
    if sys.platform == "win32" and getattr(proc, "pid", None):
        result = subprocess.run(
            ["taskkill", "/pid", str(proc.pid), "/t", "/f"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return
    elif getattr(proc, "pid", None):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    proc.kill()


def cleanup_old_jobs():
    """Bound runtime and journal history without removing active tasks."""
    if len(jobs) <= 250:
        return
    terminal = sorted(
        (
            (job_id, job)
            for job_id, job in jobs.items()
            if job.get("status") in ("done", "error", "cancelled", "missing")
        ),
        key=lambda item: item[1].get("created_at", 0),
    )
    for job_id, _job in terminal[:max(0, len(jobs) - 200)]:
        jobs.pop(job_id, None)
        get_job_store().delete(job_id)
    get_job_store().trim(keep=200)


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


def is_browser_cookie_error(error_text):
    """Identify browser cookie extraction failures that are safe to retry without cookies."""
    text = str(error_text or "").lower()
    if "cookie" not in text:
        return False
    markers = (
        "could not copy",
        "permission denied",
        "failed to decrypt",
        "dpapi",
        "app-bound",
        "app bound",
        "database is locked",
        "cookie database",
        "keyring",
        "nonetype' object has no attribute 'decode",
    )
    return any(marker in text for marker in markers)


def uses_browser_cookies(cookie_args):
    return bool(cookie_args) and cookie_args[0] == "--cookies-from-browser"


def get_download_cookie_args(options=None, cfg=None):
    cookie_args = get_cookie_args(cfg)
    if (options or {}).get("skip_browser_cookies") and uses_browser_cookies(cookie_args):
        return []
    return cookie_args


def load_config():
    """Load config from disk, merging with defaults. Returns full dict."""
    default_dir = os.path.join(get_base_dir(), "downloads")
    defaults = {
        "download_dir": default_dir,
        "proxy_url": "",
        "cookies_browser": "",
        "cookies_file": "",
        "max_concurrent": DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    }
    config_file = os.path.join(get_base_dir(), "config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, encoding="utf-8") as f:
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
    config_file = os.path.join(get_base_dir(), "config.json")
    temp_file = f"{config_file}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, config_file)


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
        parsed = urllib.parse.urlsplit(proxy_url)
        if parsed.scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
            return False
        if not parsed.hostname or not parsed.port:
            return False
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            return True
    except (OSError, ValueError):
        return False


def is_supported_proxy_url(proxy_url):
    try:
        parsed = urllib.parse.urlsplit(str(proxy_url or ""))
        return (
            parsed.scheme in ("http", "https", "socks4", "socks5", "socks5h")
            and bool(parsed.hostname and parsed.port)
        )
    except ValueError:
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


PROGRESS_PREFIX = "__MEDIADROP_PROGRESS__"
POSTPROCESS_PREFIX = "__MEDIADROP_POSTPROCESS__"


def _positive_number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _format_speed(bytes_per_second):
    if not bytes_per_second or bytes_per_second <= 0:
        return None
    units = ("B/s", "KiB/s", "MiB/s", "GiB/s")
    value = float(bytes_per_second)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.1f}{unit}"


def _format_eta(seconds):
    if seconds is None:
        return None
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def empty_progress():
    return {
        "percent": None,
        "speed": None,
        "speed_bps": None,
        "eta": None,
        "eta_seconds": None,
        "eta_confidence": "low",
        "downloaded": 0,
        "total": 0,
        "total_is_estimate": False,
        "phase": "downloading",
        "phase_index": 1,
        "state": "estimating",
    }


class DownloadProgressEstimator:
    """Estimate throughput and ETA from monotonic byte samples.

    yt-dlp's displayed ETA follows short-term throughput and is intentionally
    reactive. MediaDrop uses longer, robust windows and an asymmetric EWMA so
    brief bursts do not make the remaining time jump around.
    """

    WINDOW_SECONDS = 30.0
    WARMUP_SECONDS = 4.0
    STALL_SECONDS = 8.0
    MIN_SAMPLES = 4

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.samples = deque()
        self.rate_history = deque()
        self.phase_started_at = None
        self.last_sample_at = None
        self.last_byte_at = None
        self.last_phase_downloaded = 0
        self.phase_total = 0
        self.phase_total_is_estimate = False
        self.total_estimates = deque(maxlen=9)
        self.completed_downloaded = 0
        self.completed_total = 0
        self.completed_total_is_estimate = False
        self.phase_index = 1
        self.phase = "downloading"
        self.smoothed_speed = None
        self.last_speed_at = None
        self.smoothed_eta = None
        self.last_eta_at = None
        self.paused = False

    def _now(self, now):
        return self.clock() if now is None else float(now)

    def _begin_sampling_period(self, now, cumulative_downloaded):
        self.samples.clear()
        self.rate_history.clear()
        self.samples.append((now, cumulative_downloaded))
        self.phase_started_at = now
        self.last_sample_at = now
        self.last_byte_at = now
        self.last_speed_at = now
        self.smoothed_eta = None
        self.last_eta_at = now

    def _start_next_phase(self, now, downloaded):
        self.completed_downloaded += self.last_phase_downloaded
        self.completed_total += max(self.phase_total, self.last_phase_downloaded)
        self.completed_total_is_estimate = (
            self.completed_total_is_estimate or self.phase_total_is_estimate
        )
        self.phase_index += 1
        self.phase_total = 0
        self.phase_total_is_estimate = False
        self.total_estimates.clear()
        self.last_phase_downloaded = 0
        self._begin_sampling_period(now, self.completed_downloaded + downloaded)

    def _window_rates(self, now, cumulative_downloaded):
        rates = []
        samples = list(self.samples)
        for horizon in (2.0, 4.0, 8.0, 15.0, 30.0):
            eligible = [sample for sample in samples if sample[0] >= now - horizon]
            if not eligible:
                continue
            anchor_time, anchor_bytes = eligible[0]
            elapsed = now - anchor_time
            if elapsed >= min(1.0, horizon / 2):
                rates.append(max(0.0, cumulative_downloaded - anchor_bytes) / elapsed)
        return rates

    def _update_speed(self, now, cumulative_downloaded):
        if self.last_sample_at is not None and now <= self.last_sample_at:
            return

        previous_bytes = self.samples[-1][1] if self.samples else cumulative_downloaded
        if cumulative_downloaded > previous_bytes:
            self.last_byte_at = now
        self.samples.append((now, cumulative_downloaded))
        self.last_sample_at = now
        while self.samples and self.samples[0][0] < now - self.WINDOW_SECONDS:
            self.samples.popleft()

        rates = self._window_rates(now, cumulative_downloaded)
        if not rates:
            return
        robust_rate = statistics.median(rates)
        self.rate_history.append((now, robust_rate))
        while self.rate_history and self.rate_history[0][0] < now - self.WINDOW_SECONDS:
            self.rate_history.popleft()

        coverage = now - self.phase_started_at if self.phase_started_at is not None else 0
        if self.smoothed_speed is None or coverage < self.WARMUP_SECONDS:
            self.smoothed_speed = robust_rate
            self.last_speed_at = now
            return
        elapsed = max(0.001, now - (self.last_speed_at or now))
        half_life = 3.0 if robust_rate < self.smoothed_speed else 10.0
        alpha = 1.0 - math.exp(-math.log(2) * elapsed / half_life)
        self.smoothed_speed += alpha * (robust_rate - self.smoothed_speed)
        self.last_speed_at = now

    def _confidence(self, now):
        if self.phase_started_at is None:
            return "low"
        coverage = now - self.phase_started_at
        rates = [rate for _, rate in self.rate_history if rate > 0]
        if coverage < self.WARMUP_SECONDS or len(self.samples) < self.MIN_SAMPLES or not rates:
            return "low"
        median = statistics.median(rates)
        deviation = statistics.median(abs(rate - median) for rate in rates) / median if median else 1.0
        total_is_estimate = self.completed_total_is_estimate or self.phase_total_is_estimate
        if coverage >= 15 and deviation <= 0.25 and not total_is_estimate:
            return "high"
        return "medium"

    def _snapshot(self, now):
        downloaded = self.completed_downloaded + self.last_phase_downloaded
        total = self.completed_total + self.phase_total if self.phase_total else 0
        percent = min(99.9, downloaded * 100.0 / total) if total else None
        stalled = (
            not self.paused
            and self.last_byte_at is not None
            and now - self.last_byte_at >= self.STALL_SECONDS
        )
        confidence = self._confidence(now)

        if self.paused:
            state = "paused"
        elif self.phase != "downloading":
            state = self.phase
        elif stalled:
            state = "stalled"
        elif confidence == "low" or not total or not self.smoothed_speed:
            state = "estimating"
        else:
            state = "downloading"

        eta = None if state != "downloading" else self.smoothed_eta
        speed = None if self.phase != "downloading" else self.smoothed_speed
        return {
            "percent": percent,
            "speed": _format_speed(speed),
            "speed_bps": int(speed) if speed and speed > 0 else None,
            "eta": _format_eta(eta),
            "eta_seconds": int(round(eta)) if eta is not None else None,
            "eta_confidence": confidence,
            "downloaded": int(downloaded),
            "total": int(total),
            "total_is_estimate": self.completed_total_is_estimate or self.phase_total_is_estimate,
            "phase": self.phase,
            "phase_index": self.phase_index,
            "state": state,
        }

    def update(self, progress, now=None):
        with self.lock:
            now = self._now(now)
            downloaded = int(_positive_number(progress.get("downloaded_bytes")))
            exact_total = int(_positive_number(progress.get("total_bytes")))
            estimated_total = int(_positive_number(progress.get("total_bytes_estimate")))

            reset_threshold = max(1024 * 1024, self.last_phase_downloaded * 0.05)
            if downloaded + reset_threshold < self.last_phase_downloaded:
                self._start_next_phase(now, downloaded)

            if exact_total:
                self.phase_total = max(downloaded, exact_total)
                self.phase_total_is_estimate = False
                self.total_estimates.clear()
            elif estimated_total:
                self.total_estimates.append(estimated_total)
                self.phase_total = max(downloaded, int(statistics.median(self.total_estimates)))
                self.phase_total_is_estimate = True

            self.phase = "downloading"
            cumulative_downloaded = self.completed_downloaded + downloaded
            if self.phase_started_at is None:
                self._begin_sampling_period(now, cumulative_downloaded)
            else:
                self._update_speed(now, cumulative_downloaded)
            self.last_phase_downloaded = max(self.last_phase_downloaded, downloaded)

            remaining = max(0, self.completed_total + self.phase_total - cumulative_downloaded)
            confidence = self._confidence(now)
            if confidence != "low" and self.smoothed_speed and self.smoothed_speed > 1024 and remaining:
                raw_eta = remaining / self.smoothed_speed
                if self.smoothed_eta is None:
                    self.smoothed_eta = raw_eta
                else:
                    elapsed = max(0.001, now - (self.last_eta_at or now))
                    expected = max(0.0, self.smoothed_eta - elapsed)
                    half_life = 3.0 if raw_eta > expected else 10.0
                    alpha = 1.0 - math.exp(-math.log(2) * elapsed / half_life)
                    self.smoothed_eta = expected + alpha * (raw_eta - expected)
                self.last_eta_at = now

            return self._snapshot(now)

    def set_phase(self, phase, now=None):
        with self.lock:
            now = self._now(now)
            self.phase = phase
            self.smoothed_eta = None
            return self._snapshot(now)

    def snapshot(self, now=None):
        with self.lock:
            return self._snapshot(self._now(now))

    def pause(self, now=None):
        with self.lock:
            now = self._now(now)
            self.paused = True
            return self._snapshot(now)

    def resume(self, now=None):
        with self.lock:
            now = self._now(now)
            self.paused = False
            cumulative = self.completed_downloaded + self.last_phase_downloaded
            self._begin_sampling_period(now, cumulative)
            return self._snapshot(now)


def parse_structured_progress(line, prefix=PROGRESS_PREFIX):
    marker_at = line.find(prefix)
    if marker_at < 0:
        return None
    try:
        payload = json.loads(line[marker_at + len(prefix):])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_progress(line):
    """Legacy parser retained for older or site-specific yt-dlp output."""
    m = re.search(r"(\d+\.?\d*)%\s+of\s+~?\s*([\d.]+\w+i?B)\s+at\s+([\d.]+\w+/s|Unknown\s*\w*/s?)\s+ETA\s+([\d:]+|Unknown)", line)
    if not m:
        return None
    total = parse_size(m.group(2))
    percent = float(m.group(1))
    return {
        "downloaded_bytes": int(total * percent / 100) if total else 0,
        "total_bytes_estimate": total,
    }


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
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title).strip().rstrip(". ")[:80].rstrip(". ")
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    if safe and safe.split(".", 1)[0].upper() in reserved:
        safe = f"_{safe}"[:80]
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


def cleanup_cache_root(preserve_ids=()):
    """Remove orphaned cache entries while retaining resumable task directories."""
    preserved = {
        re.sub(r"[^a-zA-Z0-9_.-]", "_", str(job_id))[:160]
        for job_id in preserve_ids
    }
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
        if entry.name in preserved:
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                os.unlink(entry.path)
            else:
                cleanup_job_cache(entry.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[cleanup] Could not remove orphan {entry.path}: {exc}", file=sys.stderr, flush=True)


def restore_jobs():
    """Load recent history and requeue tasks interrupted by an app shutdown."""
    restored = []
    resumable = []
    changed = []
    for saved in reversed(get_job_store().load_recent(limit=200)):
        job_id = saved.pop("id")
        status = saved.get("status", "error")
        saved["progress"] = saved.get("progress") or empty_progress()
        if status in ("queued", "starting", "downloading", "paused", "interrupted"):
            saved["status"] = "queued"
            saved["progress"] = empty_progress()
            saved["error"] = None
            saved["resumed"] = True
            resumable.append(job_id)
            changed.append(job_id)
        elif status == "done" and saved.get("file") and not os.path.isfile(saved["file"]):
            saved["status"] = "missing"
            saved["file"] = None
            changed.append(job_id)
        jobs[job_id] = saved
        restored.append(job_id)

    cleanup_cache_root(preserve_ids=resumable)
    with queue_lock:
        download_queue.extend(resumable)
    for job_id in changed:
        persist_job(job_id, force=True)
    get_job_store().trim(keep=200)
    return restored


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
    try:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def get_ytdlp_env():
    """Build environment for yt-dlp subprocesses.

    Removes PYTHONPATH to avoid interference from Electron's bundled Python paths.
    Sets proxy env vars so yt-dlp can reach video sites through the configured proxy.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    configured_proxy = str(load_config().get("proxy_url", "") or "")
    proxy = get_proxy_url()
    if configured_proxy and not proxy:
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env.pop(key, None)
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
    assets = {
        asset.get("name", ""): asset.get("browser_download_url", "")
        for asset in data.get("assets", [])
        if asset.get("name") and asset.get("browser_download_url")
    }
    return {
        "version": data.get("tag_name", ""),
        "name": data.get("name", ""),
        "url": data.get("html_url", ""),
        "published_at": data.get("published_at", ""),
        "assets": assets,
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
    assets = latest.get("assets") or {}
    url = assets.get(asset_name)
    sums_url = assets.get("SHA2-256SUMS")
    if not url or not sums_url:
        raise RuntimeError("Latest yt-dlp release is missing executable or checksum assets")
    final_name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    tools_dir = get_tools_dir()
    tmp_path = os.path.join(tools_dir, f"{final_name}.download")
    final_path = os.path.join(tools_dir, final_name)
    try:
        with urllib.request.urlopen(sums_url, timeout=30) as response:
            sums = response.read().decode("utf-8", errors="replace")
        expected = None
        for line in sums.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
                expected = parts[0].lower()
                break
        if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeError(f"No valid SHA-256 checksum found for {asset_name}")

        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=120) as response, open(tmp_path, "wb") as target:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest().lower() != expected:
            raise RuntimeError("Downloaded yt-dlp failed SHA-256 verification")
        if sys.platform != "win32":
            os.chmod(tmp_path, 0o755)
        version = run_ytdlp_version(tmp_path)
        if not version or compare_version_strings(version, latest.get("version", "")) != 0:
            raise RuntimeError("Downloaded yt-dlp version does not match the latest release")
        os.replace(tmp_path, final_path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
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
        max_concurrent = max(1, min(4, int(load_config().get("max_concurrent", 2))))
        while download_queue and len(active_downloads) < max_concurrent:
            job_id = download_queue.pop(0)
            job = jobs.get(job_id)
            if not job or job.get("status") != "queued":
                continue
            active_downloads.add(job_id)
            job["status"] = "starting"
            persist_job(job_id, force=True)
            thread = threading.Thread(
                target=run_download,
                args=(
                    job_id,
                    job["url"],
                    job["format"],
                    job.get("format_id"),
                    job.get("title", ""),
                    job.get("video_range_mode", "auto"),
                    job.get("preset", "recommended"),
                    job.get("options") or {},
                ),
            )
            thread.daemon = True
            job["thread"] = thread
            thread.start()


def run_download(
    job_id,
    url,
    format_choice,
    format_id,
    title,
    video_range_mode="auto",
    preset="recommended",
    options=None,
):
    job = jobs[job_id]
    options = options or {}
    download_dir = get_download_dir()
    try:
        job_cache_dir = get_job_cache_dir(job_id)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"Could not create download cache: {exc}"
        persist_job(job_id, force=True)
        with queue_lock:
            active_downloads.discard(job_id)
        process_download_queue()
        return
    job["cache_dir"] = job_cache_dir
    out_template = os.path.join(job_cache_dir, f"{job_id}.%(ext)s")

    cmd = [
        get_ytdlp_path(),
        "--no-playlist",
        "--newline",
        "--progress",
        "--progress-delta", "0.5",
        "--progress-template", f"download:{PROGRESS_PREFIX}%(progress)j",
        "--progress-template", f"postprocess:{POSTPROCESS_PREFIX}%(progress)j",
        "-c",
        "-o", out_template,
    ]

    # Pass --proxy flag directly to yt-dlp in addition to env vars,
    # because some yt-dlp extractors ignore env vars and only respect --proxy.
    proxy = get_proxy_url()
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += get_download_cookie_args(options)

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
        audio_quality = str(options.get("audio_quality", "192"))
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", f"{audio_quality}K"]
    elif format_id:
        container = options.get("container", "mp4")
        ext = f".{container}"
        cmd += ["-f", f"{format_id}+bestaudio/{format_id}", "--merge-output-format", container]
    else:
        container = options.get("container", "mp4")
        ext = f".{container}"
        range_filter = {
            "hdr": "[dynamic_range!=SDR]",
            "sdr": "[dynamic_range=SDR]",
        }.get(video_range_mode, "")
        video_filter = f"bestvideo{range_filter}"
        fallback = "/best" if video_range_mode == "auto" else ""
        if preset == "highest":
            selector = f"{video_filter}+bestaudio{fallback}"
        elif preset == "smallest":
            selector = f"worstvideo{range_filter}+worstaudio" + ("/worst" if video_range_mode == "auto" else "")
        elif preset == "compatible":
            selector = f"bestvideo{range_filter}[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]"
            if video_range_mode == "auto":
                selector += "/best[ext=mp4][height<=1080]/best"
        else:
            selector = f"{video_filter}[height<=1080]+bestaudio"
            if video_range_mode == "auto":
                selector += "/best[height<=1080]/best"
        cmd += ["-f", selector, "--merge-output-format", container]

    if format_choice != "image":
        if options.get("subtitles") and format_choice == "video":
            languages = options.get("subtitle_languages", "zh.*,en.*")
            cmd += ["--write-subs", "--sub-langs", languages, "--embed-subs"]
        if options.get("metadata"):
            cmd.append("--embed-metadata")
        if options.get("chapters") and format_choice == "video":
            cmd.append("--embed-chapters")
        if options.get("embed_thumbnail"):
            cmd.append("--embed-thumbnail")

    cmd.append(url)

    try:
        print(f"[download] Starting download (format={format_choice})", file=sys.stderr, flush=True)
        last_lines = []
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=get_ytdlp_env(),
            start_new_session=sys.platform != "win32",
        )
        job["proc"] = proc
        job["paused"] = False
        estimator = DownloadProgressEstimator()
        job["estimator"] = estimator

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            last_lines.append(line)
            if len(last_lines) > 10:
                last_lines.pop(0)
            structured = parse_structured_progress(line)
            if structured and not job.get("paused"):
                job["progress"] = estimator.update(structured)
                job["status"] = "downloading"
                persist_job(job_id)
                continue

            if POSTPROCESS_PREFIX in line:
                if estimator.phase == "downloading":
                    job["progress"] = estimator.set_phase("postprocessing")
                    persist_job(job_id)
                continue

            if "[Merger]" in line:
                job["progress"] = estimator.set_phase("merging")
            elif "[ExtractAudio]" in line:
                job["progress"] = estimator.set_phase("converting_audio")
            elif "[ThumbnailsConvertor]" in line:
                job["progress"] = estimator.set_phase("converting_thumbnail")
            else:
                legacy = parse_progress(line)
                if legacy and not job.get("paused"):
                    job["progress"] = estimator.update(legacy)
                    job["status"] = "downloading"

        proc.wait()

        if shutdown_event.is_set() and job.get("status") == "interrupted":
            return

        # Don't overwrite cancelled status — user manually cancelled via UI
        if job.get("status") == "cancelled":
            return

        if proc.returncode != 0:
            job["status"] = "error"
            err_lines = [l for l in last_lines if "ERROR" in l or "error" in l.lower()]
            job["error"] = err_lines[-1] if err_lines else "\n".join(last_lines[-3:])
            print(f"[download] Failed (code {proc.returncode}): {job['error']}", file=sys.stderr, flush=True)
            persist_job(job_id, force=True)
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
        job["progress"]["eta_seconds"] = None
        job["progress"]["phase"] = "complete"
        job["progress"]["state"] = "complete"
        job["completed_at"] = time.time()
        persist_job(job_id, force=True)

    except Exception as e:
        if job.get("status") != "cancelled":
            job["status"] = "error"
            job["error"] = str(e)
            persist_job(job_id, force=True)
    finally:
        if job.get("status") in ("done", "error", "cancelled", "missing"):
            persist_job(job_id, force=True)
        if job.get("status") != "interrupted":
            cleanup_job_cache(job_cache_dir)
        with queue_lock:
            active_downloads.discard(job_id)
        if not shutdown_event.is_set():
            process_download_queue()


@app.route("/")
def index():
    if API_TOKEN and request.args.get("token") == API_TOKEN:
        response = redirect("/")
        response.set_cookie(
            "mediadrop_token", API_TOKEN, httponly=True, samesite="Strict", secure=False
        )
        return response
    if API_TOKEN and request.cookies.get("mediadrop_token") != API_TOKEN:
        return "MediaDrop local session is not authorized", 403
    resp = render_template("index.html")
    # Prevent browser/Electron from caching the template
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


VALID_FORMAT_CHOICES = {"video", "audio", "image"}
VALID_PRESETS = {"recommended", "highest", "smallest", "compatible", "custom"}
VALID_CONTAINERS = {"mp4", "mkv", "webm"}
VALID_AUDIO_QUALITIES = {"128", "192", "256", "320"}


def normalize_download_options(value):
    value = value if isinstance(value, dict) else {}
    container = str(value.get("container", "mp4")).lower()
    audio_quality = str(value.get("audio_quality", "192"))
    languages = str(value.get("subtitle_languages", "zh.*,en.*")).strip()[:80]
    return {
        "container": container if container in VALID_CONTAINERS else "mp4",
        "audio_quality": audio_quality if audio_quality in VALID_AUDIO_QUALITIES else "192",
        "subtitles": bool(value.get("subtitles")),
        "subtitle_languages": languages or "zh.*,en.*",
        "metadata": bool(value.get("metadata")),
        "chapters": bool(value.get("chapters")),
        "embed_thumbnail": bool(value.get("embed_thumbnail")),
        "skip_browser_cookies": bool(value.get("skip_browser_cookies")),
    }


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
        cookie_args = get_cookie_args()
        ffmpeg_dir = get_ffmpeg_dir()
        if ffmpeg_dir:
            cmd += ["--ffmpeg-location", ffmpeg_dir]
        cmd_with_cookies = cmd + cookie_args
        print("[info] Fetching video info", file=sys.stderr, flush=True)
        result = subprocess.run(
            cmd_with_cookies,
            capture_output=True,
            text=True,
            timeout=60,
            env=get_ytdlp_env(),
        )

        skip_browser_cookies = False
        cookie_warning = ""
        browser_cookie_failure = ""
        if (
            result.returncode != 0
            and uses_browser_cookies(cookie_args)
            and is_browser_cookie_error(result.stderr)
        ):
            browser_cookie_failure = result.stderr.strip()
            print(
                "[info] Browser cookies unavailable; retrying public info without cookies",
                file=sys.stderr,
                flush=True,
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                env=get_ytdlp_env(),
            )
            if result.returncode == 0:
                skip_browser_cookies = True
                cookie_warning = (
                    "无法读取浏览器 Cookie，本次已不使用 Cookie 解析。"
                    "登录限定视频请关闭浏览器后重试，或改用 Firefox / cookies.txt。"
                )

        if result.returncode != 0:
            err_detail = result.stderr.strip()
            print(f"[info] yt-dlp failed: {err_detail[:200]}", file=sys.stderr, flush=True)
            # Provide helpful message for Safari cookie sandbox issue
            if "Operation not permitted" in err_detail and "Safari" in err_detail:
                return jsonify({"error": "Safari is not supported due to macOS sandbox restrictions. Open Chrome or Firefox, log into YouTube, then select that browser in Settings > Cookies."}), 400
            if browser_cookie_failure:
                return jsonify({
                    "error": (
                        "Browser Cookie unavailable: the browser Cookie database could not be read or decrypted. "
                        "Close the browser completely and retry, or use Firefox / cookies.txt."
                    )
                }), 400
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
            "fps": f.get("fps"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "filesize_is_estimate": not bool(f.get("filesize")),
            "tbr": f.get("tbr"),
        })
    formats.sort(key=lambda x: (x["height"], x["hdr"]), reverse=True)

    return jsonify({
        "title": info.get("title", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration"),
        "uploader": info.get("uploader", ""),
        "formats": formats,
        "has_hdr": any(f["hdr"] for f in formats),
        "skip_browser_cookies": skip_browser_cookies,
        "cookie_warning": cookie_warning,
    })


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    video_range_mode = data.get("video_range_mode", "auto")
    title = data.get("title", "")
    preset = str(data.get("preset", "recommended")).lower()
    options = normalize_download_options(data.get("options"))
    options["skip_browser_cookies"] = bool(data.get("skip_browser_cookies"))

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400
    if format_choice not in VALID_FORMAT_CHOICES:
        return jsonify({"error": "Invalid download format"}), 400
    if video_range_mode not in ("auto", "hdr", "sdr"):
        return jsonify({"error": "Invalid video range mode"}), 400
    if preset not in VALID_PRESETS:
        return jsonify({"error": "Invalid download preset"}), 400
    if format_id:
        preset = "custom"

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
        "preset": preset,
        "options": options,
        "progress": empty_progress(),
        "created_at": time.time(),
    }
    persist_job(job_id, force=True)

    with queue_lock:
        download_queue.append(job_id)
    process_download_queue()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    estimator = job.get("estimator")
    if estimator and job.get("status") in ("downloading", "paused"):
        job["progress"] = estimator.snapshot()
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
            estimator = job.get("estimator")
            if estimator:
                job["progress"] = estimator.resume()
            persist_job(job_id, force=True)
            return jsonify({"status": "resumed"})
        else:
            _suspend_process(proc)
            job["paused"] = True
            job["status"] = "paused"
            estimator = job.get("estimator")
            if estimator:
                job["progress"] = estimator.pause()
            persist_job(job_id, force=True)
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
        terminate_process_tree(proc)
    start_next = False
    with queue_lock:
        if job_id in download_queue:
            download_queue.remove(job_id)
            start_next = True
        elif job_id not in active_downloads:
            start_next = True
    job["status"] = "cancelled"
    job["completed_at"] = time.time()
    persist_job(job_id, force=True)
    # Active tasks keep their concurrency slot until the worker observes the
    # terminated process and runs its cache cleanup in finally.
    if start_next:
        process_download_queue()
    return jsonify({"status": "cancelled"})


@app.route("/api/queue")
def queue_state():
    with queue_lock:
        payload = queue_payload_locked()
    return jsonify(payload)


def queue_payload_locked():
    """Build queue state while the caller owns queue_lock."""
    return {
        "queued": list(download_queue),
        "active": list(active_downloads),
        "max_concurrent": max(1, min(4, int(load_config().get("max_concurrent", 2)))),
    }


@app.route("/api/jobs")
def list_jobs():
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        return jsonify({"error": "Invalid task limit"}), 400
    ordered = sorted(
        jobs.items(), key=lambda item: item[1].get("created_at", 0), reverse=True
    )
    return jsonify({"jobs": [job_payload(job_id, job) for job_id, job in ordered[:limit]]})


@app.route("/api/jobs/<job_id>/retry", methods=["POST"])
def retry_job(job_id):
    source = jobs.get(job_id)
    if not source or source.get("status") not in ("done", "error", "cancelled", "missing"):
        return jsonify({"error": "Task cannot be retried"}), 400
    new_id = f"retry_{time.time_ns()}_{uuid.uuid4().hex[:8]}"
    jobs[new_id] = {
        "status": "queued",
        "url": source.get("url", ""),
        "format": source.get("format", "video"),
        "format_id": source.get("format_id"),
        "video_range_mode": source.get("video_range_mode", "auto"),
        "title": source.get("title", ""),
        "preset": source.get("preset", "recommended"),
        "options": source.get("options") or {},
        "progress": empty_progress(),
        "created_at": time.time(),
    }
    persist_job(new_id, force=True)
    with queue_lock:
        download_queue.append(new_id)
    process_download_queue()
    return jsonify({"job_id": new_id})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("status") not in ("done", "error", "cancelled", "missing"):
        return jsonify({"error": "Active tasks cannot be removed"}), 400
    cache_dir = job.get("cache_dir")
    if cache_dir:
        cleanup_job_cache(cache_dir)
    jobs.pop(job_id, None)
    get_job_store().delete(job_id)
    return jsonify({"status": "deleted"})


@app.route("/api/queue/reorder", methods=["POST"])
def reorder_queue():
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    direction = data.get("direction")
    with queue_lock:
        if job_id not in download_queue or direction not in ("up", "down"):
            return jsonify({"error": "Queued task or direction is invalid"}), 400
        index = download_queue.index(job_id)
        target = index - 1 if direction == "up" else index + 1
        if 0 <= target < len(download_queue):
            download_queue[index], download_queue[target] = download_queue[target], download_queue[index]
        payload = queue_payload_locked()
    return jsonify(payload)


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


def _command_version(command, timeout=10):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, env=get_ytdlp_env()
        )
        output = (result.stdout or result.stderr).strip().splitlines()
        return output[0] if output else None
    except Exception:
        return None


def _redacted_proxy(proxy):
    if not proxy:
        return ""
    try:
        parsed = urllib.parse.urlsplit(proxy)
        if not parsed.hostname:
            return "configured (invalid URL)"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    except ValueError:
        return "configured (invalid URL)"


@app.route("/api/diagnostics")
def diagnostics():
    cfg = load_config()
    proxy = cfg.get("proxy_url", "")
    recent = sorted(
        jobs.items(), key=lambda item: item[1].get("created_at", 0), reverse=True
    )[:20]
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "app_version": os.environ.get("MEDIADROP_VERSION", "development"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "yt_dlp": run_ytdlp_version(get_ytdlp_path()),
        "ffmpeg": _command_version([
            os.path.join(get_ffmpeg_dir(), "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
            if get_ffmpeg_dir() else "ffmpeg",
            "-version",
        ]),
        "settings": {
            "download_dir": cfg.get("download_dir"),
            "proxy": _redacted_proxy(proxy),
            "cookies_browser": cfg.get("cookies_browser", ""),
            "cookies_file_configured": bool(cfg.get("cookies_file")),
            "max_concurrent": cfg.get("max_concurrent", DEFAULT_MAX_CONCURRENT_DOWNLOADS),
        },
        "tasks": [
            {
                "id": job_id[-16:],
                "status": job.get("status"),
                "format": job.get("format"),
                "preset": job.get("preset"),
                "error": job.get("error"),
                "created_at": job.get("created_at"),
            }
            for job_id, job in recent
        ],
    }
    body = json.dumps(report, ensure_ascii=False, indent=2)
    response = make_response(body)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=mediadrop-diagnostics.json"
    return response


@app.route("/api/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
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
            proxy_url = str(data["proxy_url"]).strip()
            if proxy_url and not is_supported_proxy_url(proxy_url):
                return jsonify({"error": "Proxy URL must include a supported scheme, host, and port"}), 400
            updates["proxy_url"] = proxy_url

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

        if "max_concurrent" in data:
            try:
                max_concurrent = int(data["max_concurrent"])
            except (TypeError, ValueError):
                return jsonify({"error": "Concurrent downloads must be a number"}), 400
            if not 1 <= max_concurrent <= 4:
                return jsonify({"error": "Concurrent downloads must be between 1 and 4"}), 400
            updates["max_concurrent"] = max_concurrent

        if updates:
            save_config(updates)
            if "max_concurrent" in updates:
                process_download_queue()

        cfg = load_config()
        return jsonify({
            "download_dir": cfg["download_dir"],
            "proxy_url": cfg["proxy_url"],
            "cookies_browser": cfg["cookies_browser"],
            "cookies_file": cfg["cookies_file"],
            "max_concurrent": cfg["max_concurrent"],
        })

    cfg = load_config()
    return jsonify({
        "download_dir": cfg["download_dir"],
        "proxy_url": cfg["proxy_url"],
        "cookies_browser": cfg["cookies_browser"],
        "cookies_file": cfg["cookies_file"],
        "max_concurrent": cfg["max_concurrent"],
    })


def signal_handler(sig, frame):
    """Handle termination signals."""
    print(f"[signal] Received signal {sig}, cleaning up...", file=sys.stderr, flush=True)
    cleanup_all_jobs()
    sys.exit(0)


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


if __name__ == "__main__":
    restore_jobs()
    cleanup_destination_temp_files(get_download_dir())

    # First-run: import proxy from Electron's detectProxy() if config is empty.
    # This way users don't need to manually configure their proxy on first launch.
    env_proxy = os.environ.get("PROXY_URL", "")
    if env_proxy and is_supported_proxy_url(env_proxy):
        cfg = load_config()
        if not is_supported_proxy_url(cfg.get("proxy_url")):
            save_config({"proxy_url": env_proxy})
            print(f"[startup] Saved proxy from environment: {env_proxy}", file=sys.stderr, flush=True)
    elif env_proxy:
        print("[startup] Ignored invalid proxy from environment", file=sys.stderr, flush=True)

    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"[startup] MediaDrop starting on {host}:{port}", file=sys.stderr, flush=True)
    process_download_queue()
    app.run(host=host, port=port)
