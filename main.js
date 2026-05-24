const { app, BrowserWindow, dialog, shell, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");
const net = require("net");

let mainWindow = null;
let flaskProcess = null;
const PORT = 8899;

// Ensure Homebrew and user Python paths are available when launched from DMG
const userSitePackages = path.join(os.homedir(), "Library/Python/3.9/lib/python/site-packages");
const userBin = path.join(os.homedir(), "Library/Python/3.9/bin");
// Remove old paths first, then prepend in correct order (homebrew first for latest yt-dlp)
for (const p of ["/opt/homebrew/bin", userBin]) {
  process.env.PATH = process.env.PATH.split(":").filter(x => x !== p).join(":");
}
// Prepend in reverse order so homebrew ends up first
process.env.PATH = `/opt/homebrew/bin:${userBin}:${process.env.PATH}`;
if (!process.env.PYTHONPATH || !process.env.PYTHONPATH.includes(userSitePackages)) {
  process.env.PYTHONPATH = `${userSitePackages}:${process.env.PYTHONPATH || ""}`;
}

// Auto-detect proxy from system settings or shell profile
function detectProxy() {
  // 1. Already set in environment
  if (process.env.https_proxy || process.env.http_proxy) return;

  // 2. Try reading from macOS system proxy
  try {
    const { execSync } = require("child_process");
    const output = execSync("networksetup -getwebproxy Wi-Fi", { encoding: "utf8", timeout: 3000 });
    const enabled = output.match(/Enabled:\s*(Yes|No)/i);
    const server = output.match(/Server:\s*(.+)/);
    const port = output.match(/Port:\s*(\d+)/);
    if (enabled && enabled[1].toLowerCase() === "yes" && server && port) {
      const proxy = `http://${server[1].trim()}:${port[1].trim()}`;
      process.env.http_proxy = proxy;
      process.env.https_proxy = proxy;
      console.log(`[proxy] Using system proxy: ${proxy}`);
      return;
    }
  } catch {}

  // 3. Try reading from shell profile (safe: only allow known profile names)
  try {
    const { execSync } = require("child_process");
    const shellPath = process.env.SHELL || "/bin/zsh";
    const profile = shellPath.includes("zsh") ? ".zshrc" : ".bashrc";
    // Validate profile name to prevent injection
    if (!/^\.(zshrc|bashrc)$/.test(profile)) return;
    const content = execSync(`source ~/${profile} 2>/dev/null; echo "$http_proxy|$https_proxy"`, {
      encoding: "utf8", timeout: 3000, shell: "/bin/zsh"
    });
    const [http, https] = content.trim().split("|");
    if (http) process.env.http_proxy = http;
    if (https) process.env.https_proxy = https;
    if (http || https) console.log(`[proxy] Using profile proxy: ${http || https}`);
  } catch {}
}
detectProxy();

function getResourcePath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "app");
  }
  return path.join(__dirname, "app");
}

function getPythonPath() {
  const venvPython = path.join(getResourcePath(), "venv", "bin", "python3");
  if (fs.existsSync(venvPython)) {
    return venvPython;
  }
  return "python3";
}

function checkDependencies() {
  return new Promise((resolve) => {
    const checks = [
      { cmd: "python3", args: ["--version"], name: "Python 3" },
      { cmd: "yt-dlp", args: ["--version"], name: "yt-dlp" },
      { cmd: "ffmpeg", args: ["-version"], name: "ffmpeg" },
    ];

    let missing = [];
    let checked = 0;

    checks.forEach(({ cmd, args, name }) => {
      const proc = spawn(cmd, args, { stdio: "pipe" });
      proc.on("error", () => {
        missing.push(name);
        checked++;
        if (checked === checks.length) resolve(missing);
      });
      proc.on("close", (code) => {
        if (code !== 0) missing.push(name);
        checked++;
        if (checked === checks.length) resolve(missing);
      });
    });
  });
}

function killPortProcess(port) {
  // Validate port is a number to prevent injection
  if (typeof port !== 'number' || port < 1 || port > 65535) return;
  try {
    const { execSync } = require("child_process");
    const output = execSync(`lsof -ti :${port}`, { encoding: "utf8", timeout: 3000 }).trim();
    if (output) {
      const pids = output.split("\n").filter(Boolean);
      for (const pid of pids) {
        try {
          process.kill(parseInt(pid), "SIGKILL");
          console.log(`[cleanup] Killed old process ${pid} on port ${port}`);
        } catch {}
      }
    }
  } catch {}
}

function waitForPort(port, timeout = 15000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tryConnect = () => {
      const socket = new net.Socket();
      socket.once("connect", () => {
        socket.destroy();
        resolve();
      });
      socket.once("error", () => {
        socket.destroy();
        if (Date.now() - start > timeout) {
          reject(new Error("Flask server did not start in time"));
        } else {
          setTimeout(tryConnect, 300);
        }
      });
      socket.connect(port, "127.0.0.1");
    };
    tryConnect();
  });
}

function startFlaskServer() {
  killPortProcess(PORT);
  const appPath = getResourcePath();
  const python = getPythonPath();
  const appPy = path.join(appPath, "app.py");

  return new Promise((resolve, reject) => {
    let stderrOutput = "";
    flaskProcess = spawn(python, [appPy], {
      cwd: appPath,
      env: { ...process.env, PORT: String(PORT), HOST: "127.0.0.1" },
      stdio: ["ignore", "pipe", "pipe"],
    });

    flaskProcess.on("error", (err) => {
      reject(new Error(`Failed to start Python: ${err.message}`));
    });

    flaskProcess.on("close", (code) => {
      if (code && code !== 0 && !mainWindow) {
        reject(new Error(`Python process exited with code ${code}\n\n${stderrOutput}`));
      }
    });

    flaskProcess.stderr.on("data", (data) => {
      stderrOutput += data.toString();
      console.error(`Flask stderr: ${data}`);
    });

    waitForPort(PORT).then(resolve).catch(reject);
  });
}

async function getDownloadDir() {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/api/config`);
    const data = await res.json();
    return data.download_dir || null;
  } catch {
    return null;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1000,
    height: 750,
    title: "MediaDrop",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // Cache-bust so Chromium always loads the latest template
  mainWindow.webContents.session.clearCache().catch(() => {});
  mainWindow.loadURL(`http://127.0.0.1:${PORT}?v=${Date.now()}`);

  // Auto-save downloads to configured path instead of showing system dialog
  mainWindow.webContents.session.on("will-download", async (event, item) => {
    const downloadDir = await getDownloadDir();
    if (downloadDir) {
      const filename = item.getFilename();
      const savePath = path.join(downloadDir, filename);
      item.setSavePath(savePath);
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
}

ipcMain.handle("select-folder", async () => {
  const result = await dialog.showOpenDialog(mainWindow || undefined, {
    properties: ["openDirectory", "createDirectory"],
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

app.whenReady().then(async () => {
  const missing = await checkDependencies();

  if (missing.length > 0) {
    await dialog.showMessageBox({
      type: "error",
      title: "Missing Dependencies",
      message: `MediaDrop requires the following to be installed:\n\n${missing.join(", ")}\n\nInstall with:\n  brew install ${missing.map((m) => m.toLowerCase().replace(" ", "-")).join(" ")}`,
      buttons: ["OK"],
    });
    app.quit();
    return;
  }

  try {
    await startFlaskServer();
    createWindow();
  } catch (err) {
    await dialog.showMessageBox({
      type: "error",
      title: "Startup Error",
      message: `Failed to start MediaDrop server:\n\n${err.message}`,
      buttons: ["OK"],
    });
    app.quit();
  }
});

app.on("window-all-closed", () => {
  if (flaskProcess) {
    // Kill Flask and all its child processes (yt-dlp)
    try {
      process.kill(-flaskProcess.pid, "SIGTERM");
    } catch {
      flaskProcess.kill("SIGTERM");
    }
    flaskProcess = null;
  }
  // Also kill any process on our port (safety net)
  killPortProcess(PORT);
  app.quit();
});

app.on("activate", () => {
  if (mainWindow === null) {
    createWindow();
  }
});
