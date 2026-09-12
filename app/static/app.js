function uiIcon(name) {
  return `<svg class="ui-icon" aria-hidden="true"><use href="/static/icons.svg#${name}"></use></svg>`;
}

let cardData = [];
let parseController = null;
let taskRefreshTimer = null;
const notifiedJobs = new Set();

// Settings
let settingsLoaded = false;
let settingsMessageTimer = null;
let currentAppUpdateState = null;
let dismissedAppUpdateVersion = null;

function openSettings(tab) {
  if (window.electronAPI?.openSettings && !new URLSearchParams(location.search).has("settings")) { window.electronAPI.openSettings(tab); return; }
  document.getElementById("settingsDrawer").inert = false;
  document.getElementById('settingsDrawer').classList.add('open');
  document.getElementById('settingsBackdrop').classList.add('open');
  document.getElementById('settingsDrawer').setAttribute('aria-hidden', 'false');
  document.body.classList.add('drawer-open');
  if (tab) window.mediaWorkspace?.settingsTab(tab);
  if (!settingsLoaded) loadSettings();
  setTimeout(() => document.querySelector('#settingsDrawer .icon-btn').focus(), 50);
}

function closeSettings() {
  if (new URLSearchParams(location.search).has("settings")) { if (window.electronAPI?.closeSettings) void window.electronAPI.closeSettings(); else window.close(); return; }
  document.getElementById("settingsDrawer").inert = true;
  document.getElementById('settingsDrawer').classList.remove('open');
  document.getElementById('settingsBackdrop').classList.remove('open');
  document.getElementById('settingsDrawer').setAttribute('aria-hidden', 'true');
  document.body.classList.remove('drawer-open');
  document.getElementById('settingsLauncher').focus();
}

document.addEventListener('keydown', event => {
  const closeShortcut = new URLSearchParams(location.search).has('settings') && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'w';
  if ((event.key === 'Escape' || closeShortcut) && document.getElementById('settingsDrawer').classList.contains('open')) {
    event.preventDefault();
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
    const youtubeMode = document.getElementById('youtubeMode');
    if (youtubeMode) youtubeMode.value = data.youtube_enhanced ? 'enhanced' : 'standard';
    document.getElementById('cookiesBrowser').value = data.cookies_browser || 'chrome';
    document.getElementById('cookiesFile').value = data.cookies_file || '';
    document.getElementById('maxConcurrent').value = String(data.max_concurrent || 2);
    document.getElementById('authMode').value = data.cookies_file ? 'file' : data.cookies_browser ? 'browser' : 'none';
    updateAuthFields();
    settingsLoaded = true;
    window.mediaWorkspace?.setFolder(data.download_dir);
    window.mediaWorkspace?.applyPreferences(data);
    void loadEngineStatus();
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

async function loadEngineStatus() {
  const output = document.getElementById('youtubeEngineVersion');
  if (!output) return;
  try {
    const res = await fetch('/api/engines');
    if (!res.ok) throw new Error('engine status');
    const { youtube } = await res.json();
    output.textContent = youtube.available ? `${youtube.version || '版本未知'}${youtube.mode === 'sabr' ? ' · 使用中' : ' · 未启用'}` : '未安装';
    document.getElementById('youtubeEngineUpdate').hidden = !window.electronAPI;
  } catch { output.textContent = '暂时无法读取'; }
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
    const status = data.update_available ? `发现新版本 ${data.latest || ''}` : '标准引擎已是最新版本；YouTube 增强引擎随应用更新';
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
    setSettingsMsg('ok', `标准引擎已更新至 ${current}；YouTube 增强引擎随应用更新`);
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
    verifying: '正在核验媒体',
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
      embed_thumbnail: options.container !== 'webm' && Boolean(options.embed_thumbnail),
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
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : '';
  } catch {
    return '';
  }
}

function friendlyError(err) {
  err = String(err || '未知错误');
  if (err.includes('WebM does not support embedded thumbnails')) return 'WebM 不支持嵌入封面，请选择 MP4 或 MKV';
  if (err.includes('Compatibility mode requires')) return '兼容模式需要使用 MP4 或 MKV';
  if (err.includes('FFprobe is required')) return '缺少 FFprobe 组件，请重新安装完整应用';
  if (err.includes('Task has already finished')) return '任务已经完成，正在刷新状态';
  if (err.includes('Task is still finishing')) return '任务正在完成清理，请稍后重试';
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
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); go(); }
});

function stopCardPolling(card) {
  if (card._pollInterval != null) clearTimeout(card._pollInterval);
  card._pollInterval = null;
  card._pollController?.abort();
  card._pollController = null;
  card._pollGeneration = (card._pollGeneration || 0) + 1;
}

function isCurrentCardAttempt(idx, card, attempt) {
  return cardData[idx] === card && card._downloadAttempt === attempt;
}

function isWebmFormat(format) {
  return /^(vp8|vp9|vp08|vp09|av01|av1)/i.test(format.vcodec || '')
    && (!format.acodec || format.acodec === 'none' || /^(opus|vorbis)/i.test(format.acodec));
}

async function go() {
  if (parseController) {
    parseController.abort();
    return;
  }
  const urls = parseUrls(document.getElementById('urls').value);
  if (!urls.length) return;

  const selectedPreset = document.getElementById('defaultQuality')?.value || 'highest';
  const btn = document.getElementById('goBtn');
  const container = document.getElementById('cards');
  parseController = new AbortController();
  btn.textContent = '取消解析';
  const firstIndex = cardData.length;
  urls.forEach(url => cardData.push({ url, status: 'loading' }));
  urls.forEach((_url, index) => renderCard(firstIndex + index));
  window.mediaWorkspace?.selectCard(firstIndex);
  let nextIndex = 0;
  const worker = async () => {
    while (nextIndex < urls.length) {
      const offset = nextIndex++;
      const idx = firstIndex + offset;
      const url = urls[offset];
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
          youtubeEngine: data.youtube_engine,
          skipBrowserCookies: Boolean(data.skip_browser_cookies),
          cookieWarning: data.cookie_warning || '',
          videoRangeMode: 'auto',
          selectedFormatId: null,
          preset: selectedPreset,
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
  btn.disabled = !parseUrls(document.getElementById('urls').value).length;
}

function renderCard(idx) {
  renderCardContent(idx);
  window.mediaWorkspace?.render();
}

function renderCardContent(idx) {
  const c = cardData[idx];
  if (!c) return;
  let el = document.getElementById(`card-${idx}`);
  const advancedOpen = Boolean(el?.querySelector('.advanced-options')?.open);
  const formatsOpen = Boolean(el?.querySelector('.format-details')?.open);
  const qualityScroll = el?.querySelector('.quality-row')?.scrollTop || 0;
  const focused = el?.contains(document.activeElement) ? { ...document.activeElement.dataset } : null;
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

  const thumbUrl = safeMediaUrl(c.thumbnail);
  const thumbHtml = thumbUrl && !isAudio
    ? `<img src="${attr(thumbUrl)}" alt="">`
    : `<div class="no-thumb">${uiIcon(isAudio ? 'audio' : isImage ? 'image' : 'video')}</div>`;

  // Format pills
  const fmtPills = `<div class="fmt-pills">
    <button class="fmt-pill${cardFmt === 'video' ? ' active' : ''}" data-click-action="set-card-format" data-index="${idx}" data-format="video" aria-pressed="${cardFmt === 'video'}">视频</button>
    <button class="fmt-pill${cardFmt === 'audio' ? ' active' : ''}" data-click-action="set-card-format" data-index="${idx}" data-format="audio" aria-pressed="${cardFmt === 'audio'}">音频</button>
    <button class="fmt-pill${cardFmt === 'image' ? ' active' : ''}" data-click-action="set-card-format" data-index="${idx}" data-format="image" aria-pressed="${cardFmt === 'image'}">封面</button>
  </div>`;

  const rangeMode = c.videoRangeMode || 'auto';
  const rangePills = `<label class="inspector-option"><span>动态范围</span><select data-change-action="set-video-range" data-index="${idx}"><option value="auto"${rangeMode === 'auto' ? ' selected' : ''}>跟随源视频</option><option value="hdr"${rangeMode === 'hdr' ? ' selected' : ''}${c.hasHdr ? '' : ' disabled'}>仅 HDR</option><option value="sdr"${rangeMode === 'sdr' ? ' selected' : ''}>仅 SDR</option></select></label>`;
  const preset = c.preset || 'recommended';
  const webm = c.options?.container === 'webm';
  const presets = [['highest', '最高画质'], ['recommended', '均衡 · 1080p'], ['smallest', '节省空间'], ['compatible', '兼容优先']];
  const presetPills = `<label class="inspector-option"><span>画质</span><select data-change-action="set-preset" data-index="${idx}">${c.selectedFormatId ? '<option selected disabled>指定格式</option>' : ''}${presets.map(([value, label]) => `<option value="${value}"${preset === value && !c.selectedFormatId ? ' selected' : ''}${webm && value === 'compatible' ? ' disabled' : ''}>${label}</option>`).join('')}</select></label>`;

  // Quality chips (only for video)
  let qualityChips = '';
  if (!isAudio && !isImage && c.formats && c.formats.length > 0) {
    const visibleFormats = c.formats.filter(f =>
      (!webm || isWebmFormat(f)) && (rangeMode === 'hdr' ? f.hdr : rangeMode === 'sdr' ? !f.hdr : true)
    );
    qualityChips = visibleFormats.map(f => {
      const size = f.filesize ? `${f.filesize_is_estimate ? '约 ' : ''}${fmtSize(f.filesize)}` : '大小未知';
      const codec = /av01|av1/i.test(f.vcodec || '') ? 'AV1' : /vp0?9/i.test(f.vcodec || '') ? 'VP9' : /avc|h264/i.test(f.vcodec || '') ? 'H.264' : /hev|hvc|h265/i.test(f.vcodec || '') ? 'HEVC' : f.vcodec || '';
      const subtitle = [codec, f.fps ? `${Math.round(f.fps)} fps` : '', size].filter(Boolean).join(' · ');
      const selected = String(f.id) === String(c.selectedFormatId);
      return `<button class="q-chip format-choice${selected ? ' active' : ''}" aria-pressed="${selected}" data-click-action="pick-format" data-index="${idx}" data-format-id="${attr(String(f.id))}"><span class="format-copy"><strong>${esc(f.label)}</strong><small>${esc(subtitle)}</small></span><span class="format-check" aria-hidden="true">${uiIcon('check')}</span></button>`;
    }
    ).join('');
  }

  const options = c.options || {};
  const disclosureHeader = (title, detail) => `<summary><span class="disclosure-copy"><strong>${title}</strong><small>${esc(detail)}</small></span><svg class="disclosure-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 3 5 5-5 5"/></svg></summary>`;
  const chosenFormat = c.formats?.find(f => String(f.id) === String(c.selectedFormatId));
  const formatOptions = !isAudio && !isImage && qualityChips ? `<details class="format-details option-section">${disclosureHeader('分辨率与编码', chosenFormat?.label || '自动匹配当前画质偏好')}<div class="option-content"><p class="option-hint">选择源视频提供的格式</p><div class="quality-row" aria-label="可用视频格式">${qualityChips}</div></div></details>` : '';
  const optionSwitch = (key, title, hint, disabled = false) => `<label class="option-toggle${disabled ? ' unavailable' : ''}"><span class="option-copy"><span>${title}</span><small>${hint}</small></span><input type="checkbox" role="switch" aria-label="${title}" data-change-action="set-option" data-index="${idx}" data-option="${key}"${options[key] && !disabled ? ' checked' : ''}${disabled ? ' disabled' : ''}></label>`;
  const enabledExtras = ['metadata', 'embed_thumbnail', ...(!isAudio ? ['subtitles', 'chapters'] : [])].filter(key => options[key]).length;
  const advancedOptions = isImage ? '' : `<details class="advanced-options option-section">
    ${disclosureHeader('高级选项', `${isAudio ? (options.audio_quality || '192') + ' kbps' : (options.container || 'mp4').toUpperCase()} · ${enabledExtras ? enabledExtras + ' 项附加内容' : '未添加附加内容'}`)}
    <div class="option-content advanced-grid">
      <div class="option-group">
      ${!isAudio ? `<label class="option-select"><span class="option-copy"><span>文件格式</span><small>选择视频封装</small></span><select aria-label="文件格式" data-change-action="set-option" data-index="${idx}" data-option="container"><option value="mp4"${(options.container || 'mp4') === 'mp4' ? ' selected' : ''}>MP4</option><option value="mkv"${options.container === 'mkv' ? ' selected' : ''}>MKV</option><option value="webm"${options.container === 'webm' ? ' selected' : ''}>WebM</option></select></label>` : `<label class="option-select"><span class="option-copy"><span>音频质量</span><small>更高音质占用更多空间</small></span><select aria-label="音频质量" data-change-action="set-option" data-index="${idx}" data-option="audio_quality">${['128','192','256','320'].map(value => `<option value="${value}"${(options.audio_quality || '192') === value ? ' selected' : ''}>${value} kbps</option>`).join('')}</select></label>`}
      </div>
      <div class="option-group">
        ${optionSwitch('metadata', '写入元数据', '保存标题、作者等媒体信息')}
        ${optionSwitch('embed_thumbnail', '嵌入封面', webm && !isAudio ? 'WebM 不支持嵌入封面' : '在播放器中显示视频封面', webm && !isAudio)}
        ${!isAudio ? optionSwitch('chapters', '保留章节', '按源视频章节浏览') : ''}
      </div>
      ${!isAudio ? `<div class="option-group">${optionSwitch('subtitles', '嵌入字幕', '保存源视频可用的字幕')}<label class="option-language"><span>字幕语言</span><input aria-label="字幕语言" type="text" spellcheck="false" value="${attr(options.subtitle_languages || 'zh.*,en.*')}" data-change-action="set-option" data-index="${idx}" data-option="subtitle_languages"><small>默认中文与英文；多个语言用逗号分隔。</small></label></div>` : ''}
    </div>
  </details>`;

  let actionHtml = '';
  let progressHtml = '';

  if (c.status === 'ready') {
    actionHtml = `${fmtPills}
      ${!isAudio && !isImage ? rangePills : ''}
      ${!isAudio && !isImage ? presetPills : ''}
      ${formatOptions}
      ${advancedOptions}
      <button class="card-dl-btn" data-click-action="download-card" data-index="${idx}">下载</button>`;
  } else if (c.status === 'queued') {
    progressHtml = `
      <div class="dl-controls">
        <span class="card-status downloading">等待下载</span>
        <button class="card-dl-btn small cancel" data-click-action="cancel-card" data-index="${idx}">取消</button>
      </div>`;
  } else if (c.status === 'downloading' || c.status === 'paused' || c.status === 'cancelling') {
    const p = c.progress || {};
    const numericPct = Number.isFinite(p.percent) ? Math.max(0, Math.min(100, p.percent)) : null;
    const pct = numericPct != null ? numericPct.toFixed(1) : '--';
    const speed = fmtSpeed(p.speed_bps, p.speed);
    const downloaded = fmtSize(p.downloaded || 0);
    const total = p.total ? fmtSize(p.total) : '';
    const isPaused = c.status === 'paused';
    const isCancelling = c.status === 'cancelling';
    const statusIcon = uiIcon(isPaused ? 'pause' : 'download');
    const phaseLabel = progressPhaseLabel(p.phase);
    const etaText = fmtEtaEstimate(p, isPaused);
    const progressDetail = phaseLabel ? etaText : [speed, etaText].filter(Boolean).join(' · ');
    const totalText = p.total_is_estimate ? `约 ${total}` : total;
    const statusText = isCancelling ? '正在取消' : isPaused
      ? '已暂停'
      : phaseLabel || (total ? `${downloaded} / ${totalText}` : downloaded !== '0 B' ? `已下载 ${downloaded}` : '正在下载');
    progressHtml = `
      <div class="dl-progress">
        <progress class="progress-bar" max="100" value="${numericPct || 0}" aria-label="下载进度"></progress>
        <div class="progress-info"><span>${pct}%</span><span>${progressDetail}</span></div>
      </div>
      <div class="dl-controls">
        <span class="card-status downloading">${statusIcon} ${statusText}</span>
        <button class="card-dl-btn small" data-click-action="toggle-pause" data-index="${idx}"${isCancelling ? ' disabled' : ''}>${isPaused ? '继续' : '暂停'}</button>
        <button class="card-dl-btn small cancel" data-click-action="cancel-card" data-index="${idx}"${isCancelling ? ' disabled' : ''}>取消</button>
      </div>`;
  } else if (c.status === 'done') {
    const completed = completedDownloadFor(c, cardFmt);
    const completionStatus = completed
      ? `<span class="card-status done">已保存：${esc(completed.filename || '')}${completed.mediaInfo?.dynamic_range ? ' · ' + esc(completed.mediaInfo.dynamic_range) : ''}</span>`
      : '';
    actionHtml = `${completionStatus}
      ${fmtPills}
      ${!isAudio && !isImage ? rangePills : ''}
      ${!isAudio && !isImage ? presetPills : ''}
      ${formatOptions}
      ${advancedOptions}
      <div class="done-btns">
        <button class="card-dl-btn" data-click-action="download-card" data-index="${idx}">${completed ? '重新下载' : '下载'}</button>
      </div>`;
  } else if (c.status === 'error') {
    actionHtml = `${fmtPills}
      ${!isAudio && !isImage ? rangePills : ''}
      ${!isAudio && !isImage ? presetPills : ''}
      ${formatOptions}
      ${advancedOptions}
      <button class="card-dl-btn" data-click-action="download-card" data-index="${idx}">重试</button>
      <span class="card-status error">${esc(friendlyError(c.error || 'Download failed'))}</span>`;
  } else if (c.status === 'cancelled') {
    actionHtml = `<span class="card-status cancelled">已取消</span>
      ${fmtPills}
      ${!isAudio && !isImage ? rangePills : ''}
      ${!isAudio && !isImage ? presetPills : ''}
      ${formatOptions}
      ${advancedOptions}
      <button class="card-dl-btn" data-click-action="download-card" data-index="${idx}">下载</button>`;
  }

  el.innerHTML = `
    <div class="card-thumb${thumbUrl && !isAudio ? '' : ' placeholder-thumb'}">${thumbHtml}</div>
    <div class="card-body">
      <div class="card-title">${esc(c.title || '未命名媒体')}</div>
      <div class="card-meta">${esc(c.uploader)}${c.duration ? ' · ' + fmtDur(c.duration) : ''}</div>
      ${c.cookieWarning ? `<div class="cookie-warning">${esc(c.cookieWarning)}</div>` : ''}
      <div class="card-actions">${actionHtml}</div>
      ${progressHtml}
      ${c.actionError ? `<div class="card-error-msg">${esc(c.actionError)}</div>` : ''}
    </div>
  `;
  if (advancedOpen && el.querySelector('.advanced-options')) el.querySelector('.advanced-options').open = true;
  if (formatsOpen && el.querySelector('.format-details')) el.querySelector('.format-details').open = true;
  const qualityList = el.querySelector('.quality-row');
  if (qualityList) qualityList.scrollTop = qualityScroll;
  if (focused && (focused.clickAction || focused.changeAction)) {
    const replacement = [...el.querySelectorAll('[data-click-action],[data-change-action]')].find(control => Object.entries(focused).every(([key, value]) => control.dataset[key] === value));
    replacement?.focus({ preventScroll:true });
  }
}


async function togglePause(idx) {
  const c = cardData[idx];
  if (!c?.jobId || c.cancelRequested) return;
  const attempt = c._downloadAttempt;
  const jobId = c.jobId;
  try {
    const res = await fetch(`/api/pause/${encodeURIComponent(jobId)}`, { method: 'POST' });
    const data = await res.json();
    if (!isCurrentCardAttempt(idx, c, attempt) || c.jobId !== jobId || c.cancelRequested) return;
    if (!res.ok || data.error) throw new Error(data.error || '任务操作失败');
    c.actionError = null;
    if (data.status === 'paused') {
      c.status = 'paused';
    } else if (data.status === 'resumed') {
      c.status = 'downloading';
    }
    renderCard(idx);
  } catch (err) {
    if (!isCurrentCardAttempt(idx, c, attempt) || c.jobId !== jobId || c.cancelRequested) return;
    c.actionError = friendlyError(err.message);
    renderCard(idx);
  }
}

async function cancelDl(idx) {
  const c = cardData[idx];
  if (!c || c.cancelRequested || !c._downloadAttempt) return;
  c._downloadAttempt.cancelled = true;
  c.cancelRequested = true;
  c.status = 'cancelling';
  c.actionError = null;
  stopCardPolling(c);
  renderCard(idx);
  if (c.jobId) await finishCardCancellation(idx, c, c._downloadAttempt, c.jobId);
}

async function finishCardCancellation(idx, card, attempt, jobId) {
  try {
    const response = await fetch(`/api/cancel/${encodeURIComponent(jobId)}`, { method: 'POST' });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '取消失败');
    if (!isCurrentCardAttempt(idx, card, attempt)) return;
    card.status = 'cancelled';
    card.progress = {};
    card.activeRequest = null;
  } catch (error) {
    if (!isCurrentCardAttempt(idx, card, attempt)) return;
    attempt.cancelled = false;
    card.cancelRequested = false;
    card.status = 'downloading';
    card.actionError = friendlyError(error.message);
    pollCard(idx, card, jobId);
  }
  if (isCurrentCardAttempt(idx, card, attempt)) renderCard(idx);
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
  if (preset === 'compatible' && cardData[idx].options?.container === 'webm') return;
  cardData[idx].preset = preset;
  cardData[idx].selectedFormatId = null;
  renderCard(idx);
}

function setOption(idx, key, value) {
  const card = cardData[idx];
  card.options = { ...(card.options || {}), [key]: value };
  if (key === 'container' && value === 'webm') {
    card.options.embed_thumbnail = false;
    if (card.preset === 'compatible') card.preset = 'recommended';
    const selected = card.formats?.find(format => format.id === card.selectedFormatId);
    if (selected && !isWebmFormat(selected)) {
      card.selectedFormatId = null;
      card.preset = 'recommended';
    }
  }
  renderCard(idx);
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
  if (!c || ['downloading', 'queued', 'paused', 'cancelling'].includes(c.status)) return;
  const requestedFormat = currentCardFormat(idx);
  const requested = buildDownloadRequest(c, requestedFormat);
  const resumeJobId = c.status === 'error' && c.resumable && c.jobId
    && c.activeRequest && downloadRequestKey(c.activeRequest) === downloadRequestKey(requested)
    ? c.jobId : null;
  stopCardPolling(c);
  const attempt = { cancelled: false };
  c._downloadAttempt = attempt;
  c.jobId = null;
  c.activeRequest = requested;
  c.status = 'downloading';
  c.error = null;
  c.actionError = null;
  c.progress = {};
  c.cancelRequested = false;
  renderCard(idx);

  try {
    const res = await fetch(resumeJobId ? `/api/jobs/${encodeURIComponent(resumeJobId)}/retry` : '/api/download', {
      method: 'POST',
      headers: resumeJobId ? undefined : { 'Content-Type': 'application/json' },
      body: resumeJobId ? undefined : JSON.stringify({
        url: c.url,
        format: requested.format,
        format_id: requested.formatId,
        video_range_mode: requested.videoRangeMode || 'auto',
        preset: requested.preset,
        options: requested.options,
        thumbnail: c.thumbnail,
        youtube_engine: c.youtubeEngine,
        skip_browser_cookies: Boolean(c.skipBrowserCookies),
        title: c.title || '',
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error || !data.job_id) throw new Error(data.error || '无法创建下载任务');
    if (isCurrentCardAttempt(idx, c, attempt)) c.jobId = data.job_id;
    if (attempt.cancelled) {
      await finishCardCancellation(idx, c, attempt, data.job_id);
    } else if (isCurrentCardAttempt(idx, c, attempt)) {
      pollCard(idx, c, data.job_id);
    }
  } catch (err) {
    if (!isCurrentCardAttempt(idx, c, attempt)) return;
    c.status = 'error';
    c.error = err.message;
    renderCard(idx);
  }
}

function pollCard(idx, c = cardData[idx], jobId = c?.jobId) {
  if (!c || !jobId || cardData[idx] !== c) return;
  stopCardPolling(c);
  const generation = c._pollGeneration;
  const attempt = c._downloadAttempt;
  const current = () => isCurrentCardAttempt(idx, c, attempt)
    && c.jobId === jobId && c._pollGeneration === generation && !c.cancelRequested;
  const poll = async () => {
    if (!current()) return;
    c._pollInterval = null;
    let keepPolling = true;
    const controller = new AbortController();
    c._pollController = controller;
    try {
      const res = await fetch(`/api/status/${encodeURIComponent(jobId)}`, { signal: controller.signal });
      const data = await res.json();
      if (!current()) return;
      if (!res.ok) throw new Error(data.error || 'Lost connection to server');
      if (data.progress) {
        c.progress = data.progress;
      }
      if (data.status === 'done') {
        keepPolling = false;
        const completedRequest = c.activeRequest || buildDownloadRequest(c);
        c.completedDownloads = c.completedDownloads || {};
        c.completedDownloads[downloadRequestKey(completedRequest)] = {
          filename: data.filename,
          mediaInfo: data.media_info,
          request: completedRequest,
        };
        c.status = 'done';
        c.actionError = null;
        c.resumable = false;
        c.filename = data.filename;
        c.activeRequest = null;
        if (!notifiedJobs.has(jobId)) {
          notifiedJobs.add(jobId);
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
        c.status = 'paused';
        c.progress = data.progress;
        renderCard(idx);
      } else if (data.status === 'queued' || data.status === 'starting') {
        c.status = data.status === 'queued' ? 'queued' : 'downloading';
        renderCard(idx);
      } else {
        c.status = 'downloading';
        renderCard(idx);
      }
    } catch (error) {
      if (!current() || error.name === 'AbortError') return;
      keepPolling = false;
      c.status = 'error';
      c.error = error.message || 'Lost connection to server';
      renderCard(idx);
    } finally {
      if (c._pollController === controller) c._pollController = null;
    }
    if (keepPolling && current()) {
      c._pollInterval = setTimeout(poll, 1000);
    }
  };
  c._pollInterval = setTimeout(poll, 1000);
}

async function dlAll() {
  const btn = document.querySelector('.dl-all-btn');
  if (btn) { btn.disabled = true; btn.textContent = '正在下载...'; }

  const batch = cardData.slice();
  for (let i = 0; i < batch.length; i++) {
    if (cardData[i] !== batch[i]) break;
    if (batch[i].status === 'ready') {
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
    window.mediaWorkspace?.message(friendlyError(error.message));
    setSettingsMsg('err', friendlyError(error.message), true);
  }
}

function taskButtons(task) {
  const id = attr(task.id);
  const button = (action, label, icon, extra = '') => `<button title="${label}" aria-label="${label}" data-click-action="task-action" data-job-id="${id}" data-task-action="${action}" ${extra}>${uiIcon(icon)}</button>`;
  if (task.status === 'starting' || task.status === 'interrupted') return button('cancel', '取消下载', 'close');
  if (task.status === 'queued') return button('reorder', '上移', 'up', 'data-direction="up"') + button('reorder', '下移', 'down', 'data-direction="down"') + button('cancel', '取消下载', 'close');
  if (task.status === 'downloading' || task.status === 'paused') return button('pause', task.status === 'paused' ? '继续下载' : '暂停下载', task.status === 'paused' ? 'play' : 'pause') + button('cancel', '取消下载', 'close');
  if (task.status === 'done') return `<button title="在 Finder 中显示" aria-label="在 Finder 中显示" data-click-action="reveal-task-file" data-file-path="${attr(task.file || '')}">${uiIcon('folder')}</button>` + button('retry', '重新下载', 'retry') + button('delete', '移除记录', 'close');
  return button('retry', '重试下载', 'retry') + button('delete', '移除记录', 'close');
}

async function revealTaskFile(filePath) {
  if (!filePath || !window.electronAPI?.showItemInFolder) return;
  await window.electronAPI.showItemInFolder(filePath);
}

async function refreshTaskCenter() {
  clearTimeout(taskRefreshTimer);
  let nextRefresh = 10000;
  try {
    const response = await fetch('/api/jobs?limit=200&include_active=1');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '无法读取任务');
    const tasks = data.jobs || [];
    window.mediaWorkspace?.update(tasks);
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
      const detail = task.error ? ` · ${esc(friendlyError(task.error))}` : task.media_info?.dynamic_range ? ` · ${esc(task.media_info.dynamic_range)}` : '';
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
  } catch { window.mediaWorkspace?.connectionLost(); } finally {
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
    case 'set-preset': setPreset(Number(target.dataset.index), target.value); break;
    case 'set-video-range': setVideoRangeMode(Number(target.dataset.index), target.value); break;
    case 'save-concurrency': void saveConcurrency(); break;
    case 'change-auth-mode': void changeAuthMode(); break;
    case 'save-youtube-mode': void postConfig({ youtube_enhanced: target.value === 'enhanced' }, '已更新 YouTube 模式，请重新解析链接').then(loadEngineStatus); break;
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
