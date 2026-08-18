let cardData = [];
let parseController = null;
let taskRefreshTimer = null;
const notifiedJobs = new Set();

// Settings
let settingsLoaded = false;
let settingsMessageTimer = null;
let currentAppUpdateState = null;
let dismissedAppUpdateVersion = null;

function openSettings() {
  document.getElementById('settingsDrawer').classList.add('open');
  document.getElementById('settingsBackdrop').classList.add('open');
  document.getElementById('settingsDrawer').setAttribute('aria-hidden', 'false');
  document.body.classList.add('drawer-open');
  if (!settingsLoaded) loadSettings();
  setTimeout(() => document.querySelector('#settingsDrawer .icon-btn').focus(), 50);
}

function closeSettings() {
  document.getElementById('settingsDrawer').classList.remove('open');
  document.getElementById('settingsBackdrop').classList.remove('open');
  document.getElementById('settingsDrawer').setAttribute('aria-hidden', 'true');
  document.body.classList.remove('drawer-open');
  document.getElementById('settingsLauncher').focus();
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && document.getElementById('settingsDrawer').classList.contains('open')) {
    closeSettings();
  }
});

function updateAuthFields() {
  const mode = document.getElementById('authMode').value;
  document.getElementById('browserCookieField').hidden = mode !== 'browser';
  document.getElementById('cookieFileField').hidden = mode !== 'file';
}

async function loadSettings() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    document.getElementById('dlPath').value = data.download_dir || '';
    document.getElementById('proxyUrl').value = data.proxy_url || '';
    document.getElementById('cookiesBrowser').value = data.cookies_browser || 'chrome';
    document.getElementById('cookiesFile').value = data.cookies_file || '';
    document.getElementById('maxConcurrent').value = String(data.max_concurrent || 2);
    document.getElementById('authMode').value = data.cookies_file ? 'file' : data.cookies_browser ? 'browser' : 'none';
    updateAuthFields();
    settingsLoaded = true;
  } catch (err) {
    setSettingsMsg('err', `无法加载设置：${friendlyError(err.message)}`);
  }
}

async function postConfig(updates, successText) {
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Could not save settings');
    setSettingsMsg('ok', successText || '已自动保存', true);
    return data;
  } catch (err) {
    setSettingsMsg('err', friendlyError(err.message));
    return null;
  }
}

async function savePath() {
  const input = document.getElementById('dlPath');
  const data = await postConfig({ download_dir: input.value }, '下载文件夹已保存');
  if (data) input.value = data.download_dir || '';
}

async function saveProxy() {
  const input = document.getElementById('proxyUrl');
  const data = await postConfig({ proxy_url: input.value }, input.value ? '代理设置已保存' : '已切换为直接连接');
  if (data) input.value = data.proxy_url || '';
}

async function saveConcurrency() {
  const value = Number(document.getElementById('maxConcurrent').value);
  const data = await postConfig({ max_concurrent: value }, `同时下载任务数已设为 ${value}`);
  if (data) document.getElementById('maxConcurrent').value = String(data.max_concurrent || 2);
}

function exportDiagnostics() {
  const link = document.createElement('a');
  link.href = '/api/diagnostics';
  link.download = 'mediadrop-diagnostics.json';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function changeAuthMode() {
  const mode = document.getElementById('authMode').value;
  updateAuthFields();
  if (mode === 'none') {
    await postConfig({ cookies_browser: '', cookies_file: '' }, '已停用 YouTube Cookie');
  } else if (mode === 'browser') {
    await postConfig({ cookies_browser: document.getElementById('cookiesBrowser').value || 'chrome', cookies_file: '' }, '已启用浏览器 Cookie');
  } else {
    await postConfig({ cookies_browser: '' }, '已启用 cookies.txt 认证');
  }
}

async function saveCookies() {
  const browser = document.getElementById('cookiesBrowser').value;
  await postConfig({ cookies_browser: browser, cookies_file: '' }, `已选择 ${browser} Cookie`);
}

async function saveCookieFile() {
  const input = document.getElementById('cookiesFile');
  const data = await postConfig({ cookies_file: input.value, cookies_browser: '' }, input.value ? 'Cookie 文件已保存' : 'Cookie 文件已清除');
  if (data) input.value = data.cookies_file || '';
}

async function autoDetectProxy() {
  const input = document.getElementById('proxyUrl');
  if (!window.electronAPI || !window.electronAPI.detectProxy) {
    setSettingsMsg('err', '自动检测代理仅可在桌面应用中使用');
    return;
  }
  const proxy = await window.electronAPI.detectProxy();
  if (!proxy) {
    setSettingsMsg('err', '未检测到可用代理', true);
    return;
  }
  input.value = proxy;
  await saveProxy();
}

async function browsePath() {
  if (!window.electronAPI || !window.electronAPI.selectFolder) {
    setSettingsMsg('err', '选择文件夹仅可在桌面应用中使用');
    return;
  }
  const folder = await window.electronAPI.selectFolder();
  if (folder) {
    document.getElementById('dlPath').value = folder;
    await savePath();
  }
}

async function browseCookieFile() {
  if (!window.electronAPI || !window.electronAPI.selectCookieFile) {
    setSettingsMsg('err', '选择文件仅可在桌面应用中使用');
    return;
  }
  const file = await window.electronAPI.selectCookieFile();
  if (file) {
    document.getElementById('cookiesFile').value = file;
    await saveCookieFile();
  }
}

function setSettingsMsg(kind, message, autoClear = false) {
  const msg = document.getElementById('settingsMsg');
  clearTimeout(settingsMessageTimer);
  msg.className = `settings-msg ${kind}`;
  msg.textContent = message;
  if (autoClear) settingsMessageTimer = setTimeout(() => { msg.textContent = ''; }, 2400);
}

function renderAppUpdateState(state) {
  if (!state || typeof state !== 'object') return;
  currentAppUpdateState = state;
  const banner = document.getElementById('appUpdateBanner');
  const current = state.currentVersion ? `v${state.currentVersion}` : '未知版本';
  const checkButton = document.getElementById('checkAppUpdateBtn');
  document.getElementById('appVersion').textContent = current;
  checkButton.disabled = state.status === 'checking';
  checkButton.textContent = state.status === 'checking' ? '正在检查...' : '检查应用更新';

  if (state.status === 'available' || state.status === 'unsupported') {
    banner.hidden = dismissedAppUpdateVersion === state.latestVersion;
    document.getElementById('appUpdateTitle').textContent = `发现新版本 v${state.latestVersion}`;
    document.getElementById('appUpdateDetail').textContent = state.status === 'available'
      ? `${state.assetName}${state.assetSize ? ` · ${fmtSize(state.assetSize)}` : ''}`
      : '当前平台没有匹配的安装包，可前往 Release 页面查看。';
    document.getElementById('appUpdateDownloadBtn').textContent = state.status === 'available' ? '下载安装包' : '查看 Release';
    if (state.manual) setSettingsMsg('', `发现 MediaDrop v${state.latestVersion}`);
    return;
  }

  banner.hidden = true;
  if (state.status === 'checking' && state.manual) {
    setSettingsMsg('', '正在检查 MediaDrop 更新...');
  } else if (state.status === 'up-to-date' && state.manual) {
    setSettingsMsg('ok', `MediaDrop ${current} 已是最新版本`);
  } else if (state.status === 'error' && state.manual) {
    setSettingsMsg('err', `检查更新失败：${friendlyError(state.error || '未知错误')}`);
  }
}

async function checkAppUpdate() {
  if (!window.electronAPI?.checkAppUpdate) {
    setSettingsMsg('err', '应用更新检查仅可在桌面应用中使用');
    return;
  }
  try {
    renderAppUpdateState(await window.electronAPI.checkAppUpdate());
  } catch (error) {
    setSettingsMsg('err', `检查更新失败：${friendlyError(error.message)}`);
  }
}

async function openAppUpdateDownload() {
  if (!window.electronAPI?.openAppUpdateDownload) return;
  try {
    const opened = await window.electronAPI.openAppUpdateDownload();
    if (!opened) setSettingsMsg('err', '没有可用的安装包下载地址');
  } catch (error) {
    setSettingsMsg('err', `无法打开下载地址：${friendlyError(error.message)}`);
  }
}

function dismissAppUpdate() {
  dismissedAppUpdateVersion = currentAppUpdateState?.latestVersion || null;
  document.getElementById('appUpdateBanner').hidden = true;
}

async function testCookies() {
  setSettingsMsg('', '正在测试 YouTube 登录状态...');
  try {
    const firstUrl = parseUrls(document.getElementById('urls').value)[0] || '';
    const res = await fetch('/api/test-cookies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: firstUrl }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      setSettingsMsg('err', friendlyError(data.error || 'Cookie test failed'));
    } else {
      const source = data.using_cookies ? '已配置 Cookie' : '未使用 Cookie';
      setSettingsMsg('ok', `YouTube 登录状态正常（${source}）`);
    }
  } catch (err) {
    setSettingsMsg('err', friendlyError(err.message));
  }
}

async function checkYtdlp() {
  const version = document.getElementById('ytdlpVersion');
  const updateButton = document.getElementById('updateYtdlpBtn');
  version.textContent = '正在检查...';
  setSettingsMsg('', '正在检查 yt-dlp 更新...');
  try {
    const res = await fetch('/api/ytdlp/version');
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Could not check yt-dlp');
    version.textContent = data.current || '未知版本';
    updateButton.hidden = !data.update_available;
    const status = data.update_available ? `发现新版本 ${data.latest || ''}` : 'yt-dlp 已是最新版本';
    setSettingsMsg(data.update_available ? '' : 'ok', status);
  } catch (err) {
    version.textContent = '检查失败';
    setSettingsMsg('err', friendlyError(err.message));
  }
}

async function updateYtdlp() {
  setSettingsMsg('', '正在更新 yt-dlp...');
  try {
    const res = await fetch('/api/ytdlp/update', { method: 'POST' });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'yt-dlp update failed');
    const current = data.version || data.latest || '最新版本';
    document.getElementById('ytdlpVersion').textContent = current;
    document.getElementById('updateYtdlpBtn').hidden = true;
    setSettingsMsg('ok', `yt-dlp 已更新至 ${current}`);
  } catch (err) {
    setSettingsMsg('err', friendlyError(err.message));
  }
}

// Drag and drop
const dropZone = document.getElementById('dropZone');
dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  e.stopPropagation();
  dropZone.classList.add('drag-over');
});
dropZone.addEventListener('dragleave', e => {
  e.preventDefault();
  e.stopPropagation();
  dropZone.classList.remove('drag-over');
});
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  e.stopPropagation();
  dropZone.classList.remove('drag-over');
  const files = e.dataTransfer.files;
  if (files.length) {
    let filePath = null;
    if (window.electronAPI && window.electronAPI.getPathForFile) {
      filePath = window.electronAPI.getPathForFile(files[0]);
    } else {
      filePath = files[0].path;
    }
    if (filePath) {
      document.getElementById('dlPath').value = filePath;
      savePath();
    }
  }
});

function fmtSize(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

function fmtSpeed(bytesPerSecond, fallback) {
  if (Number.isFinite(bytesPerSecond) && bytesPerSecond > 0) {
    return `${fmtSize(bytesPerSecond)}/秒`;
  }
  return fallback || '';
}

function roundedEta(seconds) {
  const bucket = seconds < 120 ? 5 : seconds < 600 ? 15 : 60;
  return Math.max(bucket, Math.round(seconds / bucket) * bucket);
}

function fmtEtaDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return secs ? `${minutes} 分 ${secs} 秒` : `${minutes} 分钟`;
  return `${secs} 秒`;
}

function progressPhaseLabel(phase) {
  const labels = {
    merging: '正在合并音视频',
    converting_audio: '正在转换 MP3',
    converting_thumbnail: '正在转换封面',
    postprocessing: '正在处理媒体',
    complete: '处理完成',
  };
  return labels[phase] || '';
}

function fmtEtaEstimate(progress, isPaused) {
  if (isPaused || progress.state === 'paused') return '估算已暂停';
  const phaseLabel = progressPhaseLabel(progress.phase);
  if (phaseLabel) return phaseLabel;
  if (progress.state === 'stalled') return '网络波动，正在重新估算';

  const seconds = Number(progress.eta_seconds);
  if (!Number.isFinite(seconds) || seconds <= 0 || progress.state === 'estimating') {
    return '正在估算剩余时间';
  }
  if (progress.eta_confidence === 'medium') {
    const lower = roundedEta(seconds * 0.85);
    const upper = Math.max(lower, roundedEta(seconds * 1.2));
    return `预计 ${fmtEtaDuration(lower)} 至 ${fmtEtaDuration(upper)}`;
  }
  return `预计剩余 ${fmtEtaDuration(roundedEta(seconds))}`;
}

function setCardFormat(idx, fmt) {
  cardData[idx].format = fmt;
  renderCard(idx);
}

function currentCardFormat(idx) {
  const active = document.querySelector(`#card-${idx} .fmt-pill.active`);
  const fmt = active ? active.dataset.format : '';
  if (fmt) {
    cardData[idx].format = fmt;
    return fmt;
  }
  return cardData[idx].format || 'video';
}

function buildDownloadRequest(card, format = card.format || 'video') {
  const options = card.options || {};
  let requestOptions = {};
  if (format === 'video') {
    requestOptions = {
      container: options.container || 'mp4',
      subtitles: Boolean(options.subtitles),
      subtitle_languages: options.subtitle_languages || 'zh.*,en.*',
      metadata: Boolean(options.metadata),
      chapters: Boolean(options.chapters),
      embed_thumbnail: Boolean(options.embed_thumbnail),
    };
  } else if (format === 'audio') {
    requestOptions = {
      audio_quality: options.audio_quality || '192',
      metadata: Boolean(options.metadata),
      embed_thumbnail: Boolean(options.embed_thumbnail),
    };
  }
  return {
    format,
    formatId: format === 'video' ? (card.selectedFormatId || null) : null,
    videoRangeMode: format === 'video' ? (card.videoRangeMode || 'auto') : null,
    preset: format === 'video' ? (card.preset || 'recommended') : 'custom',
    options: requestOptions,
  };
}

function downloadRequestKey(request) {
  return JSON.stringify([
    request.format,
    request.formatId || null,
    request.formatId ? null : request.videoRangeMode || null,
    request.preset || null,
    request.options || {},
  ]);
}

function completedDownloadFor(card, format = card.format || 'video') {
  const key = downloadRequestKey(buildDownloadRequest(card, format));
  return card.completedDownloads?.[key] || null;
}

function parseUrls(text) {
  return [...new Set(text.split(/[\s,]+/).map(u => u.trim()).filter(u => u.startsWith('http')))];
}

function fmtDur(s) {
  if (!s) return '';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function attr(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function safeMediaUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? attr(url.href) : '';
  } catch {
    return '';
  }
}

function friendlyError(err) {
  err = String(err || '未知错误');
  if (err.includes('Unsupported URL')) return '暂不支持此链接';
  if (err.includes('Video unavailable') || err.includes('not available')) return '视频不可用或属于私密内容';
  if (err.includes('Private video')) return '这是一个私密视频';
  if (err.includes('HTTP Error 403') || err.includes('403: Forbidden')) return '下载地址被拒绝；应用已自动重试。仍失败时请关闭 VPN/代理，或切换 Cookie 设置后重试';
  if (err.includes('HTTP Error 429') || err.includes('Too Many Requests')) return '请求过于频繁，请稍等几分钟再重试并降低同时下载数量';
  if (err.includes('No space left') || err.includes('not enough space')) return '磁盘空间不足，请清理空间后重试';
  if (err.includes('being used by another process') || err.includes('WinError 32')) return '文件被其他程序占用，请检查安全软件或同步程序后重试';
  if (err.includes('HTTP Error 404')) return '未找到该视频';
  if (err.includes('Sign in to confirm')) return 'YouTube 需要登录认证，请在设置中选择已登录的浏览器或 cookies.txt';
  if (err.includes('Operation not permitted') && err.includes('Safari')) return '暂不支持 Safari，请登录 Chrome 或 Firefox 后在设置中选择对应浏览器';
  if (err.toLowerCase().includes('cookie')) return '浏览器 Cookie 无法读取或已失效；请彻底关闭浏览器后重试，或改用 Firefox / cookies.txt';
  if (err.toLowerCase().includes('copyright')) return '视频因版权限制而无法下载';
  if (err.toLowerCase().includes('geo')) return '当前地区无法播放此视频';
  if (err.includes('timed out') || err.includes('Timed out')) return '请求超时，请重试';
  if (err.includes('network') || err.includes('Network') || err.includes('Failed to fetch')) return '网络连接失败，请检查网络或代理设置';
  if (err.includes('Could not load settings')) return '无法加载设置';
  if (err.includes('Could not save settings')) return '无法保存设置';
  if (err.includes('Cookie file does not exist')) return '所选 Cookie 文件不存在';
  if (err.includes('Unsupported browser')) return '不支持所选浏览器';
  if (err.includes('Download directory')) return '下载文件夹无效或无法访问';
  if (err.includes('Cookie test failed')) return 'Cookie 测试失败';
  if (err.includes('Could not check yt-dlp')) return '无法检查 yt-dlp 更新';
  if (err.includes('Could not check latest yt-dlp release')) return '无法获取最新的 yt-dlp 版本信息';
  if (err.includes('Could not download yt-dlp')) return '无法下载 yt-dlp 更新';
  if (err.includes('yt-dlp update failed')) return 'yt-dlp 更新失败';
  if (err.includes('Lost connection to server')) return '与下载服务的连接已中断';
  if (err.includes('Connection lost')) return '连接已中断，请重试';
  if (err.includes('No URL provided')) return '请先输入媒体链接';
  if (err.includes('Invalid URL')) return '链接格式无效';
  if (err.includes('Invalid video range mode')) return '动态范围选项无效';
  if (err.includes('Job not found')) return '未找到下载任务';
  if (err.includes('File not ready')) return '文件尚未准备完成';
  if (err.includes('Access denied')) return '没有权限访问该文件';
  if (err.includes('Process already finished')) return '下载任务已经结束';
  if (err.includes('Timed out fetching video info')) return '解析媒体信息超时';
  if (err.includes('Timed out testing cookies')) return '测试 Cookie 超时';
  if (err.includes('Cannot create directory')) return '无法创建下载文件夹';
  if (err.includes('Download failed')) return '下载失败，请重试';
  return err.length > 120 ? err.slice(0, 120) + '...' : err;
}

document.getElementById('urls').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); }
});

async function go() {
  if (parseController) {
    parseController.abort();
    return;
  }
  const urls = parseUrls(document.getElementById('urls').value);
  if (!urls.length) return;

  const btn = document.getElementById('goBtn');
  const container = document.getElementById('cards');
  parseController = new AbortController();
  btn.textContent = '取消解析';
  container.innerHTML = '';
  cardData = [];

  urls.forEach(url => cardData.push({ url, status: 'loading' }));
  cardData.forEach((_card, idx) => renderCard(idx));
  let nextIndex = 0;
  const worker = async () => {
    while (nextIndex < urls.length) {
      const idx = nextIndex++;
      const url = urls[idx];
    try {
      const res = await fetch('/api/info', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
        signal: parseController.signal,
      });
      const data = await res.json();
      if (data.error) {
        cardData[idx] = { ...cardData[idx], status: 'info-error', error: data.error };
      } else {
        cardData[idx] = {
          ...cardData[idx],
          status: 'ready',
          format: 'video',
          title: data.title || '',
          thumbnail: data.thumbnail || '',
          duration: data.duration,
          uploader: data.uploader || '',
          formats: data.formats || [],
          hasHdr: Boolean(data.has_hdr),
          skipBrowserCookies: Boolean(data.skip_browser_cookies),
          cookieWarning: data.cookie_warning || '',
          videoRangeMode: 'auto',
          selectedFormatId: null,
          preset: 'recommended',
          options: { container: 'mp4', audio_quality: '192', subtitle_languages: 'zh.*,en.*' },
          completedDownloads: {},
        };
      }
    } catch (err) {
      cardData[idx] = { ...cardData[idx], status: 'info-error', error: err.name === 'AbortError' ? '已取消解析' : err.message };
    }
    renderCard(idx);
    }
  };

  await Promise.all(Array.from({ length: Math.min(4, urls.length) }, worker));

  if (cardData.filter(c => c.status === 'ready').length > 1) {
    renderDownloadAll();
  }

  parseController = null;
  btn.textContent = '解析链接';
}

function renderCard(idx) {
  const c = cardData[idx];
  let el = document.getElementById(`card-${idx}`);
  if (!el) {
    el = document.createElement('div');
    el.id = `card-${idx}`;
    el.className = 'card';
    document.getElementById('cards').appendChild(el);
  }

  // Loading skeleton
  if (c.status === 'loading') {
    el.className = 'card';
    el.innerHTML = `
      <div class="card-thumb loading"></div>
      <div class="card-body">
        <div class="skeleton-line medium"></div>
        <div class="skeleton-line short"></div>
      </div>
    `;
    return;
  }

  // Error state
  if (c.status === 'info-error') {
    el.className = 'card card-error';
    el.innerHTML = `
      <div class="card-thumb">
        <div class="card-error-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        </div>
      </div>
      <div class="card-body">
        <div class="card-title card-title-error">无法解析媒体信息</div>
        <div class="card-error-msg">${esc(friendlyError(c.error || ''))}</div>
        <div class="card-error-url">${esc(c.url)}</div>
      </div>
    `;
    return;
  }

  // Ready / downloading / done / error states
  el.className = 'card';
  const cardFmt = c.format || 'video';
  const isAudio = cardFmt === 'audio';
  const isImage = cardFmt === 'image';

  let thumbHtml;
  if (isAudio) {
    thumbHtml = `<div class="no-thumb no-thumb-accent"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3"/></svg></div>`;
  } else if (isImage) {
    thumbHtml = safeMediaUrl(c.thumbnail) ? `<img src="${safeMediaUrl(c.thumbnail)}" alt="">` : `<div class="no-thumb no-thumb-accent"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg></div>`;
  } else if (c.thumbnail) {
    thumbHtml = safeMediaUrl(c.thumbnail) ? `<img src="${safeMediaUrl(c.thumbnail)}" alt="">` : `<div class="no-thumb"></div>`;
  } else {
    thumbHtml = `<div class="no-thumb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m21 15-5-5L5 21"/></svg></div>`;
  }

  // Format pills
  const fmtPills = `<div class="fmt-pills">
    <button class="fmt-pill${cardFmt === 'video' ? ' active' : ''}" data-click-action="set-card-format" data-index="${idx}" data-format="video">MP4</button>
    <button class="fmt-pill${cardFmt === 'audio' ? ' active' : ''}" data-click-action="set-card-format" data-index="${idx}" data-format="audio">MP3</button>
    <button class="fmt-pill${cardFmt === 'image' ? ' active' : ''}" data-click-action="set-card-format" data-index="${idx}" data-format="image">JPG</button>
  </div>`;

  const rangeMode = c.videoRangeMode || 'auto';
  const rangePills = `<div class="range-pills">
    <button class="range-pill${rangeMode === 'auto' ? ' active' : ''}" data-click-action="set-video-range" data-index="${idx}" data-range="auto">自动</button>
    <button class="range-pill${rangeMode === 'hdr' ? ' active' : ''}" data-click-action="set-video-range" data-index="${idx}" data-range="hdr"${c.hasHdr ? '' : ' disabled'}>仅 HDR</button>
    <button class="range-pill${rangeMode === 'sdr' ? ' active' : ''}" data-click-action="set-video-range" data-index="${idx}" data-range="sdr">仅 SDR</button>
  </div>`;

  const preset = c.preset || 'recommended';
  const presets = [
    ['recommended', '推荐'], ['highest', '最高画质'], ['smallest', '节省空间'], ['compatible', '兼容模式']
  ];
  const presetPills = `<div class="preset-row">${presets.map(([value, label]) =>
    `<button class="q-chip${preset === value && !c.selectedFormatId ? ' active' : ''}" data-click-action="set-preset" data-index="${idx}" data-preset="${value}">${label}</button>`
  ).join('')}</div>`;

  // Quality chips (only for video)
  let qualityChips = '';
  if (!isAudio && !isImage && c.formats && c.formats.length > 0) {
    const visibleFormats = rangeMode === 'hdr'
      ? c.formats.filter(f => f.hdr)
      : rangeMode === 'sdr'
        ? c.formats.filter(f => !f.hdr)
        : c.formats;
    qualityChips = visibleFormats.map(f => {
      const size = f.filesize ? ` · ${f.filesize_is_estimate ? '约 ' : ''}${fmtSize(f.filesize)}` : '';
      const fps = f.fps ? ` · ${Math.round(f.fps)}fps` : '';
      return `<button class="q-chip${f.id === c.selectedFormatId ? ' active' : ''}" title="${attr((f.vcodec || '') + fps + size)}" data-click-action="pick-format" data-index="${idx}" data-format-id="${attr(String(f.id))}">${esc(f.label)}${esc(size)}</button>`;
    }
    ).join('');
  }

  const options = c.options || {};
  const advancedOptions = `<details class="advanced-options">
    <summary>高级选项</summary>
    <div class="advanced-grid">
      ${!isAudio && !isImage ? `<label class="select-field"><span>封装</span><select data-change-action="set-option" data-index="${idx}" data-option="container"><option value="mp4"${options.container === 'mp4' ? ' selected' : ''}>MP4</option><option value="mkv"${options.container === 'mkv' ? ' selected' : ''}>MKV</option><option value="webm"${options.container === 'webm' ? ' selected' : ''}>WebM</option></select></label>` : ''}
      ${isAudio ? `<label class="select-field"><span>音质</span><select data-change-action="set-option" data-index="${idx}" data-option="audio_quality"><option value="128"${options.audio_quality === '128' ? ' selected' : ''}>128 kbps</option><option value="192"${(options.audio_quality || '192') === '192' ? ' selected' : ''}>192 kbps</option><option value="256"${options.audio_quality === '256' ? ' selected' : ''}>256 kbps</option><option value="320"${options.audio_quality === '320' ? ' selected' : ''}>320 kbps</option></select></label>` : ''}
      ${!isImage ? `<label class="check-field"><input type="checkbox" data-change-action="set-option" data-index="${idx}" data-option="metadata"${options.metadata ? ' checked' : ''}>写入元数据</label><label class="check-field"><input type="checkbox" data-change-action="set-option" data-index="${idx}" data-option="embed_thumbnail"${options.embed_thumbnail ? ' checked' : ''}>嵌入封面</label>` : ''}
      ${!isAudio && !isImage ? `<label class="check-field"><input type="checkbox" data-change-action="set-option" data-index="${idx}" data-option="subtitles"${options.subtitles ? ' checked' : ''}>嵌入字幕</label><label class="check-field"><input type="checkbox" data-change-action="set-option" data-index="${idx}" data-option="chapters"${options.chapters ? ' checked' : ''}>保留章节</label><label class="text-field"><span>字幕语言</span><input type="text" value="${attr(options.subtitle_languages || 'zh.*,en.*')}" data-change-action="set-option" data-index="${idx}" data-option="subtitle_languages"></label>` : ''}
    </div>
  </details>`;

  let actionHtml = '';
  let progressHtml = '';

  if (c.status === 'ready') {
    actionHtml = `${fmtPills}
      ${!isAudio && !isImage ? rangePills : ''}
      ${!isAudio && !isImage ? presetPills : ''}
      ${!isAudio && !isImage && qualityChips ? '<div class="quality-row">' + qualityChips + '</div>' : ''}
      ${advancedOptions}
      <button class="card-dl-btn" data-click-action="download-card" data-index="${idx}">下载</button>`;
  } else if (c.status === 'queued') {
    progressHtml = `
      <div class="dl-controls">
        <span class="card-status downloading">等待下载</span>
        <button class="card-dl-btn small cancel" data-click-action="cancel-card" data-index="${idx}">取消</button>
      </div>`;
  } else if (c.status === 'downloading' || c.status === 'paused') {
    const p = c.progress || {};
    const numericPct = Number.isFinite(p.percent) ? Math.max(0, Math.min(100, p.percent)) : null;
    const pct = numericPct != null ? numericPct.toFixed(1) : '--';
    const speed = fmtSpeed(p.speed_bps, p.speed);
    const downloaded = fmtSize(p.downloaded || 0);
    const total = p.total ? fmtSize(p.total) : '';
    const isPaused = c.status === 'paused';
    const statusIcon = isPaused ? '⏸' : '<span class="spin"></span>';
    const phaseLabel = progressPhaseLabel(p.phase);
    const etaText = fmtEtaEstimate(p, isPaused);
    const progressDetail = phaseLabel ? etaText : [speed, etaText].filter(Boolean).join(' · ');
    const totalText = p.total_is_estimate ? `约 ${total}` : total;
    const statusText = isPaused
      ? '已暂停'
      : phaseLabel || (total ? `${downloaded} / ${totalText}` : downloaded !== '0 B' ? `已下载 ${downloaded}` : '正在下载');
    progressHtml = `
      <div class="dl-progress">
        <progress class="progress-bar" max="100" value="${numericPct || 0}" aria-label="下载进度"></progress>
        <div class="progress-info"><span>${pct}%</span><span>${progressDetail}</span></div>
      </div>
      <div class="dl-controls">
        <span class="card-status downloading">${statusIcon} ${statusText}</span>
        <button class="card-dl-btn small" data-click-action="toggle-pause" data-index="${idx}">${isPaused ? '继续' : '暂停'}</button>
        <button class="card-dl-btn small cancel" data-click-action="cancel-card" data-index="${idx}">取消</button>
      </div>`;
  } else if (c.status === 'done') {
    const completed = completedDownloadFor(c, cardFmt);
    const completionStatus = completed
      ? `<span class="card-status done">已保存：${esc(completed.filename || '')}</span>`
      : '';
    actionHtml = `${completionStatus}
      ${fmtPills}
      ${!isAudio && !isImage ? rangePills : ''}
      ${!isAudio && !isImage ? presetPills : ''}
      ${!isAudio && !isImage && qualityChips ? '<div class="quality-row">' + qualityChips + '</div>' : ''}
      ${advancedOptions}
      <div class="done-btns">
        <button class="card-dl-btn" data-click-action="download-card" data-index="${idx}">${completed ? '重新下载' : '下载'}</button>
      </div>`;
  } else if (c.status === 'error') {
    actionHtml = `${fmtPills}
      ${!isAudio && !isImage ? rangePills : ''}
      ${!isAudio && !isImage ? presetPills : ''}
      ${!isAudio && !isImage && qualityChips ? '<div class="quality-row">' + qualityChips + '</div>' : ''}
      ${advancedOptions}
      <button class="card-dl-btn" data-click-action="download-card" data-index="${idx}">重试</button>
      <span class="card-status error">${esc(friendlyError(c.error || 'Download failed'))}</span>`;
  } else if (c.status === 'cancelled') {
    actionHtml = `<span class="card-status error">已取消</span>
      ${fmtPills}
      ${!isAudio && !isImage ? rangePills : ''}
      ${!isAudio && !isImage ? presetPills : ''}
      ${!isAudio && !isImage && qualityChips ? '<div class="quality-row">' + qualityChips + '</div>' : ''}
      ${advancedOptions}
      <button class="card-dl-btn" data-click-action="download-card" data-index="${idx}">下载</button>`;
  }

  el.innerHTML = `
    <div class="card-thumb">${thumbHtml}</div>
    <div class="card-body">
      <div class="card-title">${esc(c.title || '未命名媒体')}</div>
      <div class="card-meta">${esc(c.uploader)}${c.duration ? ' · ' + fmtDur(c.duration) : ''}</div>
      ${c.cookieWarning ? `<div class="cookie-warning">${esc(c.cookieWarning)}</div>` : ''}
      <div class="card-actions">${actionHtml}</div>
      ${progressHtml}
    </div>
  `;
}


async function togglePause(idx) {
  const c = cardData[idx];
  if (!c.jobId) return;
  try {
    const res = await fetch(`/api/pause/${c.jobId}`, { method: 'POST' });
    const data = await res.json();
    if (data.error) {
      c.status = 'error';
      c.error = data.error;
    } else if (data.status === 'paused') {
      c.status = 'paused';
    } else if (data.status === 'resumed') {
      c.status = 'downloading';
    }
    renderCard(idx);
  } catch (err) {
    c.status = 'error';
    c.error = err.message;
    renderCard(idx);
  }
}

async function cancelDl(idx) {
  const c = cardData[idx];
  if (!c.jobId) return;
  // Stop poll immediately to prevent race with in-flight responses
  c.cancelRequested = true;
  if (c._pollInterval) {
    clearTimeout(c._pollInterval);
    c._pollInterval = null;
  }
  try {
    await fetch(`/api/cancel/${c.jobId}`, { method: 'POST' });
    c.status = 'cancelled';
    c.progress = {};
    renderCard(idx);
  } catch {}
}

function renderDownloadAll() {
  const existing = document.getElementById('dl-all-bar');
  if (existing) existing.remove();

  const bar = document.createElement('div');
  bar.id = 'dl-all-bar';
  bar.className = 'dl-all-bar';
  bar.innerHTML = `<button class="dl-all-btn" data-click-action="download-all">全部下载</button>`;
  document.getElementById('cards').appendChild(bar);
}

function pickFormat(idx, formatId) {
  cardData[idx].selectedFormatId = formatId;
  cardData[idx].preset = 'custom';
  renderCard(idx);
}

function setPreset(idx, preset) {
  cardData[idx].preset = preset;
  cardData[idx].selectedFormatId = null;
  renderCard(idx);
}

function setOption(idx, key, value) {
  cardData[idx].options = { ...(cardData[idx].options || {}), [key]: value };
}

function setVideoRangeMode(idx, mode) {
  const c = cardData[idx];
  c.videoRangeMode = mode;
  c.selectedFormatId = null;
  if (c.preset === 'custom') c.preset = 'recommended';
  renderCard(idx);
}

async function dlCard(idx) {
  const c = cardData[idx];
  const requestedFormat = currentCardFormat(idx);
  const requested = buildDownloadRequest(c, requestedFormat);
  const resumeJobId = c.status === 'error' && c.resumable && c.jobId
    && c.activeRequest && downloadRequestKey(c.activeRequest) === downloadRequestKey(requested)
    ? c.jobId : null;
  c.activeRequest = requested;
  c.status = 'downloading';
  c.error = null;
  c.progress = {};
  c.cancelRequested = false;
  renderCard(idx);

  try {
    const res = await fetch(resumeJobId ? `/api/jobs/${encodeURIComponent(resumeJobId)}/retry` : '/api/download', {
      method: 'POST',
      headers: resumeJobId ? undefined : { 'Content-Type': 'application/json' },
      body: resumeJobId ? undefined : JSON.stringify({
        url: c.url,
        format: c.activeRequest.format,
        format_id: c.activeRequest.formatId,
        video_range_mode: c.activeRequest.videoRangeMode || 'auto',
        preset: c.activeRequest.preset,
        options: c.activeRequest.options,
        skip_browser_cookies: Boolean(c.skipBrowserCookies),
        title: c.title || '',
      }),
    });
    const data = await res.json();
    if (data.error) {
      c.status = 'error';
      c.error = data.error;
      renderCard(idx);
      return;
    }
    c.jobId = data.job_id;
    pollCard(idx);
  } catch (err) {
    c.status = 'error';
    c.error = err.message;
    renderCard(idx);
  }
}

function pollCard(idx) {
  const c = cardData[idx];
  const poll = async () => {
    c._pollInterval = null;
    if (c.cancelRequested) return;
    let keepPolling = true;
    try {
      const res = await fetch(`/api/status/${c.jobId}`);
      const data = await res.json();
      // Ignore stale poll response if user cancelled while we were waiting
      if (c.cancelRequested) {
        return;
      }
      if (data.progress) {
        c.progress = data.progress;
      }
      if (data.status === 'done') {
        keepPolling = false;
        const completedRequest = c.activeRequest || buildDownloadRequest(c);
        c.completedDownloads = c.completedDownloads || {};
        c.completedDownloads[downloadRequestKey(completedRequest)] = {
          filename: data.filename,
          request: completedRequest,
        };
        c.status = 'done';
        c.resumable = false;
        c.filename = data.filename;
        c.activeRequest = null;
        if (!notifiedJobs.has(c.jobId)) {
          notifiedJobs.add(c.jobId);
          if (window.electronAPI?.notify) window.electronAPI.notify('MediaDrop 下载完成', data.filename || c.title || '文件已保存');
        }
        renderCard(idx);
      } else if (data.status === 'error') {
        keepPolling = false;
        c.status = 'error';
        c.error = data.error;
        c.resumable = Boolean(data.resumable);
        renderCard(idx);
      } else if (data.status === 'cancelled') {
        keepPolling = false;
        c.status = 'cancelled';
        renderCard(idx);
      } else if (data.status === 'paused') {
        // Keep polling but don't change status (user controls it)
        c.progress = data.progress;
        renderCard(idx);
      } else if (data.status === 'queued' || data.status === 'starting') {
        c.status = data.status === 'queued' ? 'queued' : 'downloading';
        renderCard(idx);
      } else {
        c.status = 'downloading';
        renderCard(idx);
      }
    } catch {
      keepPolling = false;
      c.status = 'error';
      c.error = 'Lost connection to server';
      renderCard(idx);
    }
    if (keepPolling && !c.cancelRequested) {
      c._pollInterval = setTimeout(poll, 1000);
    }
  };
  c._pollInterval = setTimeout(poll, 1000);
}

async function dlAll() {
  const btn = document.querySelector('.dl-all-btn');
  if (btn) { btn.disabled = true; btn.textContent = '正在下载...'; }

  for (let i = 0; i < cardData.length; i++) {
    if (cardData[i].status === 'ready') {
      await dlCard(i);
    }
  }

  if (btn) { btn.disabled = false; btn.textContent = '全部下载'; }
}

const taskStatusLabels = {
  queued: '等待中', starting: '正在启动', downloading: '下载中', paused: '已暂停',
  interrupted: '恢复中', done: '已完成', error: '失败', cancelled: '已取消', missing: '文件已移动'
};
let taskCenterInitialized = false;

async function taskAction(id, action, extra = {}) {
  const options = { method: 'POST' };
  let url = action === 'pause' || action === 'cancel' ? `/api/${action}/${encodeURIComponent(id)}` : `/api/jobs/${encodeURIComponent(id)}/${action}`;
  if (action === 'delete') {
    url = `/api/jobs/${encodeURIComponent(id)}`;
    options.method = 'DELETE';
  } else if (action === 'reorder') {
    url = '/api/queue/reorder';
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify({ job_id: id, ...extra });
  }
  try {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '任务操作失败');
    await refreshTaskCenter();
  } catch (error) {
    setSettingsMsg('err', friendlyError(error.message), true);
  }
}

function taskButtons(task) {
  const id = attr(task.id);
  if (task.status === 'queued') return `<button title="上移" data-click-action="task-action" data-job-id="${id}" data-task-action="reorder" data-direction="up">&uarr;</button><button title="下移" data-click-action="task-action" data-job-id="${id}" data-task-action="reorder" data-direction="down">&darr;</button><button title="取消" data-click-action="task-action" data-job-id="${id}" data-task-action="cancel">&times;</button>`;
  if (task.status === 'downloading' || task.status === 'paused') return `<button title="${task.status === 'paused' ? '继续' : '暂停'}" data-click-action="task-action" data-job-id="${id}" data-task-action="pause">${task.status === 'paused' ? '&#9654;' : '&#10074;&#10074;'}</button><button title="取消" data-click-action="task-action" data-job-id="${id}" data-task-action="cancel">&times;</button>`;
  if (task.status === 'done') {
    const file = attr(task.file || '');
    return `<button title="在文件夹中显示" data-click-action="reveal-task-file" data-file-path="${file}">&#128193;</button><button title="重新下载" data-click-action="task-action" data-job-id="${id}" data-task-action="retry">&#8635;</button><button title="移除记录" data-click-action="task-action" data-job-id="${id}" data-task-action="delete">&times;</button>`;
  }
  return `<button title="重试" data-click-action="task-action" data-job-id="${id}" data-task-action="retry">&#8635;</button><button title="移除记录" data-click-action="task-action" data-job-id="${id}" data-task-action="delete">&times;</button>`;
}

async function revealTaskFile(filePath) {
  if (!filePath || !window.electronAPI?.showItemInFolder) return;
  await window.electronAPI.showItemInFolder(filePath);
}

async function refreshTaskCenter() {
  clearTimeout(taskRefreshTimer);
  let nextRefresh = 10000;
  try {
    const response = await fetch('/api/jobs?limit=50');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '无法读取任务');
    const tasks = data.jobs || [];
    const center = document.getElementById('taskCenter');
    center.hidden = tasks.length === 0;
    if (!tasks.length) return;
    const active = tasks.filter(task => ['queued', 'starting', 'downloading', 'paused', 'interrupted'].includes(task.status));
    if (active.length) nextRefresh = 2000;
    const knownPercents = active.map(task => task.progress?.percent).filter(Number.isFinite);
    const average = knownPercents.length ? ` · 总进度 ${Math.round(knownPercents.reduce((sum, value) => sum + value, 0) / knownPercents.length)}%` : '';
    document.getElementById('taskSummary').textContent = `${active.length} 个进行中 · ${tasks.length} 条记录${average}`;
    document.getElementById('taskList').innerHTML = tasks.map(task => {
      const percent = Number.isFinite(task.progress?.percent) ? ` · ${task.progress.percent.toFixed(1)}%` : '';
      const detail = task.error ? ` · ${esc(friendlyError(task.error))}` : '';
      return `<div class="task-row"><div><div class="task-title">${esc(task.title || task.filename || '未命名任务')}</div><div class="task-meta">${taskStatusLabels[task.status] || task.status} · ${(task.format || 'video').toUpperCase()}${percent}${detail}</div></div><div class="task-actions">${taskButtons(task)}</div></div>`;
    }).join('');
    if (taskCenterInitialized) {
      tasks.filter(task => task.status === 'done' && !notifiedJobs.has(task.id)).forEach(task => {
        notifiedJobs.add(task.id);
        if (window.electronAPI?.notify) window.electronAPI.notify('MediaDrop 下载完成', task.filename || task.title || '文件已保存');
      });
    } else {
      tasks.filter(task => task.status === 'done').forEach(task => notifiedJobs.add(task.id));
      taskCenterInitialized = true;
    }
  } catch {} finally {
    taskRefreshTimer = setTimeout(refreshTaskCenter, nextRefresh);
  }
}

document.addEventListener('click', event => {
  const target = event.target.closest('[data-click-action]');
  if (!target) return;
  const index = Number(target.dataset.index);
  switch (target.dataset.clickAction) {
    case 'open-settings': openSettings(); break;
    case 'close-settings': closeSettings(); break;
    case 'open-app-update': void openAppUpdateDownload(); break;
    case 'dismiss-app-update': dismissAppUpdate(); break;
    case 'fetch': void go(); break;
    case 'browse-path': void browsePath(); break;
    case 'detect-proxy': void autoDetectProxy(); break;
    case 'browse-cookie-file': void browseCookieFile(); break;
    case 'test-cookies': void testCookies(); break;
    case 'check-app-update': void checkAppUpdate(); break;
    case 'check-ytdlp': void checkYtdlp(); break;
    case 'update-ytdlp': void updateYtdlp(); break;
    case 'export-diagnostics': exportDiagnostics(); break;
    case 'set-card-format': setCardFormat(index, target.dataset.format); break;
    case 'set-video-range': setVideoRangeMode(index, target.dataset.range); break;
    case 'set-preset': setPreset(index, target.dataset.preset); break;
    case 'pick-format': pickFormat(index, target.dataset.formatId); break;
    case 'download-card': void dlCard(index); break;
    case 'cancel-card': void cancelDl(index); break;
    case 'toggle-pause': void togglePause(index); break;
    case 'download-all': void dlAll(); break;
    case 'task-action':
      void taskAction(
        target.dataset.jobId,
        target.dataset.taskAction,
        target.dataset.direction ? { direction: target.dataset.direction } : {}
      );
      break;
    case 'reveal-task-file': void revealTaskFile(target.dataset.filePath); break;
  }
});

document.addEventListener('change', event => {
  const target = event.target.closest('[data-change-action]');
  if (!target) return;
  switch (target.dataset.changeAction) {
    case 'save-concurrency': void saveConcurrency(); break;
    case 'change-auth-mode': void changeAuthMode(); break;
    case 'save-cookies': void saveCookies(); break;
    case 'set-option':
      setOption(
        Number(target.dataset.index),
        target.dataset.option,
        target.type === 'checkbox' ? target.checked : target.value
      );
      break;
  }
});

document.addEventListener('focusout', event => {
  const target = event.target.closest('[data-blur-action]');
  if (!target) return;
  if (target.dataset.blurAction === 'save-path') void savePath();
  if (target.dataset.blurAction === 'save-proxy') void saveProxy();
  if (target.dataset.blurAction === 'save-cookie-file') void saveCookieFile();
});

const updateSettings = document.getElementById('appUpdateSettings');
if (window.electronAPI?.getAppUpdateState) {
  window.electronAPI.onAppUpdateState?.(renderAppUpdateState);
  window.electronAPI.getAppUpdateState().then(renderAppUpdateState).catch(() => {});
} else {
  updateSettings.hidden = true;
}

refreshTaskCenter();
