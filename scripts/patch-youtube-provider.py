"""Make upstream provider usable with Electron's bundled Node and port zero."""
import hashlib
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
p = root / 'server/build/main.js'
s = p.read_text()
for old, new in [('.parse();', '.parse(process.argv, { from: "node" });'),
                 ('server.removeListener("error", reject);', 'server.removeListener("error", reject);\n        console.log(`MEDIADROP_POT_READY ${server.address().port}`);')]:
    if new not in s:
        if old not in s:
            raise SystemExit('Provider source changed; review the integration patch')
        s = s.replace(old, new, 1)
p.write_text(s)
(root / 'manifest.json').write_text(json.dumps({
    'engine': 'sabr', 'engine_sha256': hashlib.sha256((root / ('yt-dlp-sabr.exe' if (root / 'yt-dlp-sabr.exe').exists() else 'yt-dlp-sabr')).read_bytes()).hexdigest(),
    'provider': '2.0.0', 'sources': ['https://github.com/bashonly/yt-dlp/releases/tag/sabr',
                                 'https://github.com/Brainicism/bgutil-ytdlp-pot-provider/tree/2.0.0'],
    'modifications': 'Electron Node argument parsing; ephemeral loopback port readiness marker',
}, indent=2))
