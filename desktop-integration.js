const path = require('node:path');
const fs = require('node:fs');

const ACTIVE = new Set(['queued', 'starting', 'downloading', 'paused', 'interrupted']);
function summarizeJobs(jobs) {
  const active = jobs.filter(job => ACTIVE.has(job.status));
  const transferring = active.filter(job => job.status !== 'paused');
  const known = active.map(job => job.progress?.percent).filter(Number.isFinite);
  return { count: active.length, busy: transferring.length > 0,
    progress: !active.length ? -1 : known.length === active.length ? Math.min(1, Math.max(0, known.reduce((a, b) => a + b, 0) / known.length / 100)) : 2 };
}
function visibleBounds(saved, displays) {
  if (!saved || !['x', 'y', 'width', 'height'].every(key => Number.isFinite(saved[key]))) return {};
  const display = displays.find(({ workArea: a }) => saved.x < a.x + a.width && saved.x + saved.width > a.x && saved.y < a.y + a.height && saved.y + saved.height > a.y);
  if (!display) return {};
  const a = display.workArea, width = Math.min(a.width, Math.max(760, saved.width)), height = Math.min(a.height, Math.max(540, saved.height));
  return { width, height, x: Math.max(a.x, Math.min(saved.x, a.x + a.width - width)), y: Math.max(a.y, Math.min(saved.y, a.y + a.height - height)) };
}

function installDesktop({ electron, getMainWindow, createMainWindow, readJobs, openSettingsWindow }) {
  const { app, Menu, ipcMain, nativeTheme, systemPreferences, screen, powerSaveBlocker, powerMonitor, Notification, shell, nativeImage, dialog } = electron;
  const boundsPath = path.join(app.getPath('userData'), 'window-state.json');
  let quitting = false, confirming = false, blocker = null, timer = null, initialized = false, lastJobs = [], completed = new Set(), failed = new Set(), monitoring = false;
  const showMain = () => { let win = getMainWindow(); if (!win || win.isDestroyed()) { createMainWindow(); win = getMainWindow(); } if (win.isMinimized()) win.restore(); win.show(); win.focus(); return win; };
  const command = name => { const win = showMain(); if (win.webContents.isLoading()) win.webContents.once('did-finish-load', () => win.webContents.send('desktop-command', name)); else win.webContents.send('desktop-command', name); };
  function appearance() {
    let accent = '#176bd8'; try { accent = '#' + systemPreferences.getAccentColor().slice(0, 6); } catch {}
    const accessibility = process.platform === 'darwin' ? systemPreferences.getAnimationSettings() : {};
    return { platform: process.platform, accent, contrast: nativeTheme.shouldUseHighContrastColors, reduceMotion: accessibility.prefersReducedMotion };
  }
  function publishAppearance() { for (const win of electron.BrowserWindow.getAllWindows()) win.webContents.send('desktop-appearance', appearance()); }
  nativeTheme.on('updated', publishAppearance);
  systemPreferences.on('accent-color-changed', publishAppearance);
  systemPreferences.on('color-changed', publishAppearance);
  function savedBounds() { try { return visibleBounds(JSON.parse(fs.readFileSync(boundsPath, 'utf8')), screen.getAllDisplays()); } catch { return {}; } }
  function attachWindow(win) {
    let saveTimer;
    const save = () => { clearTimeout(saveTimer); saveTimer = setTimeout(() => { if (!win.isDestroyed() && !win.isFullScreen()) { try { fs.writeFileSync(boundsPath, JSON.stringify(win.getNormalBounds())); } catch {} } }, 300); };
    win.on('resize', save); win.on('move', save);
    win.on('close', event => { if (process.platform === 'darwin' && !quitting) { event.preventDefault(); win.hide(); } });
    win.on('closed', () => clearTimeout(saveTimer));
  }
  async function knownFile(file) {
    if (typeof file !== 'string' || !path.isAbsolute(file)) return false;
    try { const jobs = await readJobs(); return jobs.some(job => job.status === 'done' && job.file === file) && fs.statSync(file).isFile(); } catch { return false; }
  }
  ipcMain.handle('desktop-appearance', appearance);
  ipcMain.handle('open-settings-window', (_event, tab) => { openSettingsWindow(tab); return true; });
  ipcMain.handle('preview-file', async (_event, file) => { if (!await knownFile(file)) return false; const win = showMain(); if (process.platform === 'darwin') { win.previewFile(file); return true; } return !(await shell.openPath(file)); });
  ipcMain.handle('open-media-file', async (_event, file) => { if (!await knownFile(file)) return false; return !(await shell.openPath(file)); });
  ipcMain.on('drag-media-file', async (event, file) => { if (!await knownFile(file) || event.sender.isDestroyed()) return; const icon = await app.getFileIcon(file, { size: 'normal' }).catch(() => nativeImage.createEmpty()); if (!icon.isEmpty() && !event.sender.isDestroyed()) event.sender.startDrag({ file, icon }); });
  function setActivity(jobs) {
    const summary = summarizeJobs(jobs), win = getMainWindow();
    if (win && !win.isDestroyed()) win.setProgressBar(summary.progress);
    app.dock?.setBadge(summary.count ? String(summary.count) : '');
    if (summary.busy && blocker === null) blocker = powerSaveBlocker.start('prevent-app-suspension');
    if (!summary.busy && blocker !== null) { powerSaveBlocker.stop(blocker); blocker = null; }
  }
  async function monitor() {
    if (monitoring || quitting) return;
    monitoring = true;
    clearTimeout(timer);
    try {
      const jobs = await readJobs(); lastJobs = jobs; setActivity(jobs);
      const fresh = jobs.filter(job => job.status === 'done' && !completed.has(job.id));
      fresh.forEach(job => completed.add(job.id));
      const win = getMainWindow();
      if (initialized && fresh.length && (!win || !win.isVisible() || !win.isFocused()) && Notification.isSupported()) {
        const notice = new Notification({ title: fresh.length > 1 ? `${fresh.length} 个下载已完成` : 'MediaDrop 下载完成', body: fresh.length > 1 ? '文件已保存，点击查看任务。' : fresh[0].filename || fresh[0].title || '文件已保存', silent: true });
        notice.on('click', () => command('select-job:' + fresh[0].id)); notice.show();
      }
      const errors = jobs.filter(job => job.status === 'error' && !failed.has(job.id));
      errors.forEach(job => failed.add(job.id));
      jobs.filter(job => ACTIVE.has(job.status)).forEach(job => failed.delete(job.id));
      if (initialized && errors.length && (!win || !win.isVisible() || !win.isFocused()) && Notification.isSupported()) {
        const notice = new Notification({ title: '有下载需要处理', body: errors.length > 1 ? `${errors.length} 个任务未能完成，点击查看。` : errors[0].title || '点击查看失败任务。', silent: true });
        notice.on('click', () => command('select-job:' + errors[0].id)); notice.show();
      }
      initialized = true;
    } catch { /* A temporary service interruption must not release an active sleep assertion. */ }
    monitoring = false;
    if (!quitting) timer = setTimeout(monitor, 2000);
  }
  powerMonitor.on('resume', () => { commandIfVisible('refresh'); void monitor(); });
  function commandIfVisible(name) { const win = getMainWindow(); if (win && !win.isDestroyed()) win.webContents.send('desktop-command', name); }
  app.on('before-quit', event => {
    if (quitting) return;
    event.preventDefault();
    if (confirming) return;
    confirming = true;
    (async () => {
      let jobs;
      try { jobs = await readJobs(); } catch { jobs = lastJobs; }
      const { count } = summarizeJobs(jobs);
      if (count) {
        const result = await dialog.showMessageBox(showMain(), { type: 'question', message: `仍有 ${count} 个下载任务`, detail: '退出会停止正在运行的下载。也可以关闭窗口，让任务在后台继续。', buttons: ['继续下载', '退出 MediaDrop'], defaultId: 0, cancelId: 0 });
        if (result.response !== 1) { confirming = false; return; }
      }
      quitting = true; clearTimeout(timer);
      if (blocker !== null) { powerSaveBlocker.stop(blocker); blocker = null; }
      app.quit();
    })().catch(() => { confirming = false; });
  });
  const mac = process.platform === 'darwin';
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(mac ? [{ label: 'MediaDrop', submenu: [{ role: 'about' }, { type: 'separator' }, { label: '设置…', accelerator: 'CmdOrCtrl+,', click: openSettingsWindow }, { type: 'separator' }, { role: 'services' }, { type: 'separator' }, { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' }, { type: 'separator' }, { role: 'quit' }] }] : []),
    { label: '文件', submenu: [{ label: '新建下载…', accelerator: 'CmdOrCtrl+N', click: () => command('new') }, { label: '查看进行中的任务', click: () => command('show-active') }, ...(!mac ? [{ label: '设置…', accelerator: 'CmdOrCtrl+,', click: openSettingsWindow }] : []), { type: 'separator' }, { role: 'close' }] },
    { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: '显示', submenu: [{ label: '切换侧边栏', accelerator: 'CmdOrCtrl+Alt+S', click: () => command('sidebar') }, { label: '切换任务信息', accelerator: 'CmdOrCtrl+Alt+I', click: () => command('inspector') }, { type: 'separator' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' }, { role: 'togglefullscreen' }] },
    { label: '窗口', submenu: [{ role: 'minimize' }, { role: 'zoom' }, { label: '显示 MediaDrop', click: showMain }, ...(mac ? [{ type: 'separator' }, { role: 'front' }] : [])] },
  ]));
  void monitor();
  return { savedBounds, attachWindow, isQuitting: () => quitting };
}
module.exports = { summarizeJobs, visibleBounds, installDesktop };
