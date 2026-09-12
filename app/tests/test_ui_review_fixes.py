import atexit
import json
import pathlib
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock
from app import app as server

atexit.unregister(server.cleanup_all_jobs)

class UIReviewFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(server, 'get_base_dir', return_value=self.tmp.name))
        self.stack.enter_context(mock.patch.object(server, 'API_TOKEN', ''))
        self.client = server.app.test_client()

    def test_preferences_persist_across_origins_and_validate_before_save(self):
        preferences = {'ui_appearance':'dark', 'ui_density':'compact', 'ui_quality':'compatible'}
        self.assertEqual(self.client.post('/api/config', json=preferences, base_url='http://localhost:41001').status_code, 200)
        reloaded = server.app.test_client().get('/api/config', base_url='http://localhost:41002').get_json()
        for key, value in preferences.items():
            self.assertEqual(reloaded[key], value)
            self.assertEqual(server.load_config()[key], value)
        self.assertEqual(self.client.post('/api/config', json={'ui_appearance':'invalid', 'ui_density':'comfortable'}).status_code, 400)
        self.assertEqual(server.load_config()['ui_density'], 'compact')

    def test_preserve_codec_and_framerate_variants(self):
        formats = [dict(format_id=id, height=1080, vcodec=codec, fps=fps, dynamic_range='SDR', protocol='https', tbr=rate) for id,codec,fps,rate in [('avc30','avc1.640028',30,5000),('vp930','vp9',30,3000),('av130','av01.0.08M',30,2000),('av160','av01.0.08M',60,3500),('avc30duplicate','avc1.640028',30,4000)]]
        with mock.patch.object(server,'get_extractor_command',return_value=(['yt-dlp'],'standard')), mock.patch.object(server,'get_proxy_url',return_value=''), mock.patch.object(server,'get_cookie_args',return_value=[]), mock.patch.object(server,'get_ffmpeg_dir',return_value=None), mock.patch.object(server,'get_ytdlp_javascript_args',return_value=[]), mock.patch.object(server.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps({'formats':formats}),'')):
            result = self.client.post('/api/info',json={'url':'https://example.com/video'}).get_json()
        self.assertEqual({f['id'] for f in result['formats']}, {'avc30','vp930','av130','av160'})

    def test_enhanced_engine_reports_its_own_version_and_update_channel(self):
        root = pathlib.Path(self.tmp.name)
        (root/('yt-dlp-sabr.exe' if server.sys.platform == 'win32' else 'yt-dlp-sabr')).write_text('fixture')
        server.save_config({'youtube_enhanced': True})
        with mock.patch.object(server.youtube_compat,'asset_root',return_value=root), mock.patch.object(server,'cached_ytdlp_version',return_value='sabr-fixture') as version:
            result=self.client.get('/api/engines').get_json()['youtube']
            version.assert_called_once_with(str(root/('yt-dlp-sabr.exe' if server.sys.platform == 'win32' else 'yt-dlp-sabr')))
        self.assertEqual(result, {'mode':'sabr','version':'sabr-fixture','available':True,'update_method':'application'})
