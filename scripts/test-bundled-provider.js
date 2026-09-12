// Exercise the packaged helper, including native canvas, without fetching user media.
const path = require('node:path');
const fs = require('node:fs');
const { spawn, execFileSync } = require('node:child_process');
const root = path.resolve(process.argv[2]);
const runtime = process.platform === 'win32' ? path.join(root, 'node.exe') : process.execPath;
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json')));
const binary = path.join(root, process.platform === 'win32' ? 'yt-dlp-sabr.exe' : 'yt-dlp-sabr');
if (require('node:crypto').createHash('sha256').update(fs.readFileSync(binary)).digest('hex') !== manifest.engine_sha256) throw new Error('Engine checksum mismatch');
execFileSync(runtime, ['-e', "const c=require('canvas').createCanvas(8,8);if(!c.toBuffer().length)process.exit(1)"], {cwd:path.join(root,'server'),stdio:'inherit'});
const child = spawn(runtime,[path.join(root,'server/build/main.js'),'--host','127.0.0.1','--port','0'],{cwd:path.join(root,'server'),stdio:['ignore','pipe','inherit']});
let ready=false, output='';
const timer=setTimeout(()=>{console.error('Provider startup timed out');child.kill();process.exitCode=1},20000);
child.stdout.on('data',data=>{output+=data; if(!ready && /MEDIADROP_POT_READY \d+/.test(output)){ready=true;clearTimeout(timer);console.log('Bundled engine checksum, native canvas and provider startup passed');child.kill()}});
child.on('error',error=>{clearTimeout(timer);console.error(error);process.exitCode=1});
child.on('exit',()=>{clearTimeout(timer);if(!ready)process.exitCode=1});
