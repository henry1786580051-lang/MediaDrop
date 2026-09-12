import pathlib
import tempfile
import unittest
from unittest import mock
from app import app as server

class FileRelocationTests(unittest.TestCase):
    def test_relocation_restores_record_without_moving_file(self):
        with tempfile.TemporaryDirectory() as folder:
            file = pathlib.Path(folder, 'cover.jpg'); file.write_bytes(b'image')
            job = {'status':'missing','format':'image','file':'/missing/cover.jpg','completed_at':123}
            with mock.patch.object(server,'jobs',{'task':job}), mock.patch.object(server,'API_TOKEN',''), mock.patch.object(server,'persist_job') as persist:
                response = server.app.test_client().post('/api/jobs/task/locate',json={'file':str(file)})
                self.assertEqual(response.status_code,200)
                self.assertEqual(job['status'],'done')
                self.assertEqual(job['file'],str(file.resolve()))
                self.assertEqual(job['completed_at'],123)
                self.assertEqual(file.read_bytes(),b'image')
                self.assertEqual(server.job_payload('task',job)['file_size'],5)
                persist.assert_called_once_with('task',force=True)

    def test_relocation_rejects_running_jobs_and_wrong_file_type(self):
        with tempfile.TemporaryDirectory() as folder:
            file=pathlib.Path(folder,'note.txt');file.write_text('text')
            job={'status':'downloading','format':'video'}
            with mock.patch.object(server,'jobs',{'task':job}),mock.patch.object(server,'API_TOKEN',''):
                client=server.app.test_client()
                self.assertEqual(client.post('/api/jobs/task/locate',json={'file':str(file)}).status_code,409)
                job['status']='missing'
                self.assertEqual(client.post('/api/jobs/task/locate',json={'file':str(file)}).status_code,400)
                self.assertEqual(job['status'],'missing')

    def test_disappeared_completed_file_is_reported_for_attention(self):
        job={'status':'done','file':'/nonexistent/mediadrop-test.mov'}
        self.assertEqual(server.job_payload('task',job)['status'],'missing')
        self.assertEqual(job['status'],'done')
