import atexit
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from app import app as server
from app import hdr_metadata as hdr
from app.job_store import JobStore

atexit.unregister(server.cleanup_all_jobs)


class HdrMetadataTests(unittest.TestCase):
    def test_youtube_itag_does_not_claim_hdr10_or_dynamic_metadata(self):
        for label in ('HDR10', 'HDR10+', 'HLG', 'DV'):
            self.assertEqual(hdr.describe_format({'dynamic_range': label}, youtube=True), (True, 'HDR'))
        self.assertEqual(hdr.describe_format({'dynamic_range': 'SDR'}, youtube=True), (False, 'SDR'))

    def test_transfer_overrides_misleading_itag(self):
        self.assertEqual(hdr.describe_format({'dynamic_range': 'HDR10', 'color_transfer': 'arib-std-b67'}, youtube=True), (True, 'HLG'))

    def test_actual_stream_and_frame_metadata(self):
        base = {'streams': [{'color_transfer': 'smpte2084', 'width': 3840, 'height': 2160}], 'frames': []}
        self.assertEqual(hdr.classify_probe(base)['dynamic_range'], 'HDR (PQ)')
        base['frames'] = [{'side_data_list': [{'side_data_type': 'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)'}]}]
        self.assertEqual(hdr.classify_probe(base)['dynamic_range'], 'HDR10+')
        base['streams'][0]['side_data_list'] = [{'side_data_type': 'DOVI configuration record'}]
        self.assertEqual(hdr.classify_probe(base)['dynamic_range'], 'Dolby Vision')
        self.assertIsNone(hdr.classify_probe({'streams': [{'pix_fmt': 'yuv420p10le'}]}))
        self.assertEqual(hdr.classify_probe({'streams': [{'color_transfer': 'arib-std-b67'}]})['dynamic_range'], 'HLG')

    def test_limited_av1_decoder_uses_second_probe(self):
        pq = {'streams': [{'color_transfer': 'smpte2084'}]}
        plus = dict(pq, frames=[{'side_data_list': [{'side_data_type': 'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)'}]}])
        with tempfile.NamedTemporaryFile() as f, mock.patch.object(hdr.subprocess, 'run', side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(pq), 'Failed to get pixel format'),
            subprocess.CompletedProcess([], 0, json.dumps(plus), ''),
        ]) as run:
            self.assertEqual(hdr.inspect_file(f.name, ['bundled', 'system'])['dynamic_range'], 'HDR10+')
            self.assertEqual(run.call_count, 2)
            self.assertIn('%+#48', run.call_args.args[0])

    def test_missing_or_timed_out_inspector_is_nonfatal(self):
        self.assertIsNone(hdr.inspect_file('/no/such/file', ['ffprobe']))
        with tempfile.NamedTemporaryFile() as f, mock.patch.object(hdr.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffprobe', 8)):
            self.assertIsNone(hdr.inspect_file(f.name, ['ffprobe']))

    def test_api_uses_generic_hdr_without_changing_format_ids(self):
        payload = {'extractor_key': 'Youtube', 'formats': [
            {'format_id': '337', 'height': 2160, 'vcodec': 'vp9.2', 'dynamic_range': 'HDR10'},
            {'format_id': '315', 'height': 2160, 'vcodec': 'vp9', 'dynamic_range': 'SDR'},
        ]}
        with mock.patch.object(server, 'get_ytdlp_path', return_value='yt-dlp'), mock.patch.object(server, 'get_proxy_url', return_value=''), mock.patch.object(server, 'get_cookie_args', return_value=[]), mock.patch.object(server, 'get_ffmpeg_dir', return_value=None), mock.patch.object(server, 'get_ytdlp_env', return_value={}), mock.patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), '')):
            response = server.app.test_client().post('/api/info', json={'url': 'https://www.youtube.com/watch?v=sample'})
        formats = {f['id']: f for f in response.get_json()['formats']}
        self.assertEqual(formats['337']['label'], '2160p HDR')
        self.assertTrue(formats['337']['hdr'])
        self.assertFalse(formats['315']['hdr'])

    def test_verified_type_survives_history_and_status(self):
        info = {'dynamic_range': 'HLG', 'hdr': True}
        with tempfile.TemporaryDirectory() as d:
            store = JobStore(str(pathlib.Path(d, 'jobs.sqlite3')))
            store.save('hdr-test', {'status': 'done', 'media_info': info})
            self.assertEqual(store.load_recent()[0]['media_info'], info)
        job = {'status': 'done', 'media_info': info}
        self.assertEqual(server.job_payload('hdr-test', job)['media_info'], info)
        with mock.patch.dict(server.jobs, {'hdr-test': job}):
            self.assertEqual(server.app.test_client().get('/api/status/hdr-test').get_json()['media_info'], info)
