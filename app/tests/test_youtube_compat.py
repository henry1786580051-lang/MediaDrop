import io
import pathlib
import unittest
from unittest import mock
from app import app as server
from app import youtube_compat


class YouTubeEngineTests(unittest.TestCase):
    def test_scope_and_explicit_engine_survive_setting_changes(self):
        with mock.patch.object(server, 'load_config', return_value={'youtube_enhanced': True}), mock.patch.object(server, 'get_ytdlp_path', return_value='stable'), mock.patch.object(youtube_compat, 'command', return_value=['sabr']):
            self.assertEqual(server.get_extractor_command('https://example.com/youtube.com')[0], ['stable'])
            self.assertEqual(server.get_extractor_command('https://youtu.be/video')[0], ['sabr'])
            self.assertEqual(server.get_extractor_command('https://youtube.com/watch?v=x', 'standard')[0], ['stable'])
        with mock.patch.object(server, 'load_config', return_value={'youtube_enhanced': False}), mock.patch.object(youtube_compat, 'command', return_value=['sabr']):
            self.assertEqual(server.get_extractor_command('https://youtube.com/watch?v=x', 'sabr')[0], ['sabr'])

    def test_download_uses_sabr_video_and_audio(self):
        with mock.patch.object(server, 'get_extractor_command', return_value=(['sabr'], 'sabr')), mock.patch.object(server, 'get_proxy_url', return_value=''), mock.patch.object(server, 'get_download_cookie_args', return_value=[]), mock.patch.object(server, 'get_ffmpeg_dir', return_value=None), mock.patch.object(server, 'get_ytdlp_javascript_args', return_value=[]):
            cmd, _, _ = server.build_download_command('test', '/tmp', 'https://youtu.be/x', 'video', None, preset='highest')
            self.assertIn('bestvideo[protocol=sabr]+bestaudio[protocol=sabr]', cmd[cmd.index('-f')+1])
            cmd, _, _ = server.build_download_command('test', '/tmp', 'https://youtu.be/x', 'video', '401')
            self.assertEqual(cmd[cmd.index('-f')+1], '401+bestaudio[protocol=sabr]/401')

    def test_provider_reuses_live_child_and_cleans_up(self):
        child = mock.Mock()
        child.poll.return_value = None
        child.stdout = io.StringIO('private data must not be forwarded\nMEDIADROP_POT_READY 54321\n')
        provider = youtube_compat.Provider()
        with mock.patch.object(youtube_compat.subprocess, 'Popen', return_value=child) as spawn:
            self.assertEqual(provider.start(pathlib.Path('/tmp'), '/runtime'), 54321)
            self.assertEqual(provider.start(pathlib.Path('/tmp'), '/runtime'), 54321)
            self.assertEqual(spawn.call_count, 1)
            provider.close()
            child.terminate.assert_called_once()
            self.assertIsNone(provider.process)

    def test_provider_early_exit_is_actionable(self):
        child = mock.Mock()
        child.stdout = io.StringIO('startup failed\n')
        child.poll.return_value = 1
        with mock.patch.object(youtube_compat.subprocess, 'Popen', return_value=child):
            with self.assertRaisesRegex(RuntimeError, '增强组件启动失败'):
                youtube_compat.Provider().start(pathlib.Path('/tmp'), '/runtime')

    def test_parse_filters_unusable_non_sabr_formats(self):
        info = {'title': 'test', 'formats': [
            {'format_id': '401', 'height': 2160, 'vcodec': 'av01', 'protocol': 'sabr'},
            {'format_id': 'bad', 'height': 4320, 'vcodec': 'av01', 'protocol': 'https'}]}
        import json
        with mock.patch.object(server, 'API_TOKEN', ''), mock.patch.object(server, 'get_extractor_command', return_value=(['sabr'], 'sabr')), mock.patch.object(server, 'get_cookie_args', return_value=[]), mock.patch.object(server, 'get_proxy_url', return_value=''), mock.patch.object(server, 'get_ffmpeg_dir', return_value=None), mock.patch.object(server.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(info))):
            result = server.app.test_client().post('/api/info', json={'url': 'https://youtu.be/x'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual([f['id'] for f in result.json['formats']], ['401'])
        self.assertEqual(result.json['youtube_engine'], 'sabr')
