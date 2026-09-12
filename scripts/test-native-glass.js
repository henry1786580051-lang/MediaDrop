const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
function load(platform = 'darwin', release = '25.6.0') {
  const calls = [], handlers = {}, frame = {};
  const sender = { mainFrame: frame, getURL: () => 'http://127.0.0.1:1234/' };
  const win = { isDestroyed: () => false, getNativeWindowHandle: () => Buffer.alloc(8) };
  const electron = { app: {isPackaged: true}, nativeTheme: {}, BrowserWindow: { fromWebContents: () => win }, ipcMain: {handle: (name, fn) => handlers[name] = fn} };
  const context = { module: {exports: {}}, process: {platform, resourcesPath:'/resources'}, __dirname: __dirname, URL, console,
    require: name => name === 'node:path' ? path : name === 'node:os' ? {release: () => release} : {apply: (...args) => {calls.push(args); return args[1];}} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../native-glass.js'),'utf8'), context);
  const api = context.module.exports; api.install(electron);
  return {api, electron, calls, handler: handlers['native-glass'], event: {sender, senderFrame: frame}};
}
const t = load();
assert.equal(t.api.windowOptions(t.electron.app).transparent, true);
assert.equal(t.handler(t.event, 'dark'), true);
assert.equal(t.calls[0][2], 'dark');
t.electron.nativeTheme.prefersReducedTransparency = true;
assert.equal(t.handler(t.event, 'light'), false);
assert.equal(t.calls[1][1], false);
assert.equal(t.handler({...t.event, senderFrame:{}}, 'dark'), false);
assert.equal(t.calls.length, 2);
for (const [platform, release] of [['win32','25.0.0'],['darwin','24.0.0']]) {
 const old = load(platform, release); assert.equal(Object.keys(old.api.windowOptions(old.electron.app)).length,0); assert.equal(old.handler(old.event,'system'),false);
}
console.log('Native glass: main-frame ownership, reduced-transparency fallback, and unsupported systems passed');
