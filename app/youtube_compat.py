"""Bundled YouTube SABR engine and privately owned loopback PO-token provider."""
import atexit
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


def asset_root():
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent / 'bundled-bin'
    return base / 'youtube'


def engine_path():
    return asset_root() / ('yt-dlp-sabr.exe' if sys.platform == 'win32' else 'yt-dlp-sabr')


def default_enabled():
    return bool(getattr(sys, 'frozen', False) or os.environ.get('MEDIADROP_JS_RUNTIME'))


class Provider:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.port = None

    def start(self, root, runtime):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return self.port
            self._close()
            ready = queue.Queue()
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            process = subprocess.Popen(
                [runtime, str(root / 'server/build/main.js'), '--host', '127.0.0.1', '--port', '0'],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                cwd=str(root / 'server'),
            )
            self.process = process

            def drain():
                try:
                    for line in process.stdout:
                        if line.startswith('MEDIADROP_POT_READY '):
                            try:
                                port = int(line.split()[1])
                                if 0 < port < 65536:
                                    ready.put(port)
                            except (ValueError, IndexError):
                                pass
                    ready.put(None)
                finally:
                    process.stdout.close()

            threading.Thread(target=drain, daemon=True).start()
            try:
                self.port = ready.get(timeout=20)
                if self.port is None or process.poll() is not None:
                    raise RuntimeError('provider exited')
                return self.port
            except (queue.Empty, RuntimeError) as exc:
                self._close()
                raise RuntimeError('YouTube 增强组件启动失败，请重新启动应用或在设置中切换标准模式。') from exc

    def _close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None
        self.port = None

    def close(self):
        with self.lock:
            self._close()


provider = Provider()
atexit.register(provider.close)


def command(runtime):
    root = asset_root()
    binary = engine_path()
    if sys.platform == 'win32':
        runtime = str(root / 'node.exe')
    if not runtime or not os.path.isfile(runtime) or not binary.is_file() or not (root / 'server/build/main.js').is_file():
        raise RuntimeError('YouTube 增强组件不完整，请安装完整的新版 MediaDrop。')
    port = provider.start(root, runtime)
    return [str(binary), '--ignore-config', '--plugin-dirs', str(root),
            '--extractor-args', 'youtube:formats=duplicate;player_client=web;webpage_client=web',
            '--extractor-args', 'youtubepot-bgutilhttp:base_url=http://127.0.0.1:%s' % port]
