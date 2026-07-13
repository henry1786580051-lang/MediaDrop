import atexit
import concurrent.futures
import errno
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock


APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("mediadrop_server", APP_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)
atexit.unregister(server.cleanup_all_jobs)


class CacheLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = os.path.join(self.temp_dir.name, "data")
        self.download_dir = os.path.join(self.temp_dir.name, "downloads")
        os.makedirs(self.base_dir)
        os.makedirs(self.download_dir)
        self.base_patch = mock.patch.object(server, "get_base_dir", return_value=self.base_dir)
        self.base_patch.start()
        server.jobs.clear()
        server.download_queue.clear()
        server.active_downloads.clear()
        server.shutdown_event.clear()

    def tearDown(self):
        server.jobs.clear()
        server.download_queue.clear()
        server.active_downloads.clear()
        server.shutdown_event.clear()
        self.base_patch.stop()
        self.temp_dir.cleanup()

    def make_cached_file(self, job_id="job-1", content=b"complete-media"):
        job_dir = server.get_job_cache_dir(job_id)
        source = os.path.join(job_dir, f"{job_id}.mp4")
        with open(source, "wb") as handle:
            handle.write(content)
        return job_dir, source

    def test_completed_file_is_published_and_cache_can_be_removed(self):
        job_dir, source = self.make_cached_file()

        final_path, filename = server.finalize_cached_file(
            source, self.download_dir, "Example", ".mp4", "job-1"
        )

        self.assertEqual(filename, "Example.mp4")
        self.assertEqual(pathlib.Path(final_path).read_bytes(), b"complete-media")
        self.assertFalse(os.path.exists(source))
        self.assertTrue(server.cleanup_job_cache(job_dir))
        self.assertTrue(os.path.isfile(final_path))

    def test_existing_file_is_never_overwritten(self):
        existing = os.path.join(self.download_dir, "Example.mp4")
        pathlib.Path(existing).write_bytes(b"keep-me")
        _, source = self.make_cached_file(content=b"new-file")

        final_path, filename = server.finalize_cached_file(
            source, self.download_dir, "Example", ".mp4", "job-1"
        )

        self.assertEqual(pathlib.Path(existing).read_bytes(), b"keep-me")
        self.assertEqual(filename, "Example (1).mp4")
        self.assertEqual(pathlib.Path(final_path).read_bytes(), b"new-file")

    def test_concurrent_same_title_downloads_both_survive(self):
        _, first_source = self.make_cached_file("job-1", b"first")
        _, second_source = self.make_cached_file("job-2", b"second")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    server.finalize_cached_file,
                    source,
                    self.download_dir,
                    "Same title",
                    ".mp4",
                    job_id,
                )
                for source, job_id in ((first_source, "job-1"), (second_source, "job-2"))
            ]
            results = [future.result() for future in futures]

        filenames = {filename for _, filename in results}
        contents = {pathlib.Path(path).read_bytes() for path, _ in results}
        self.assertEqual(filenames, {"Same title.mp4", "Same title (1).mp4"})
        self.assertEqual(contents, {b"first", b"second"})

    def test_cross_filesystem_fallback_still_publishes_complete_file(self):
        _, source = self.make_cached_file(content=b"copied-file")
        real_link = os.link
        calls = 0

        def cross_filesystem_once(link_source, destination):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.EXDEV, "different filesystem")
            return real_link(link_source, destination)

        with mock.patch.object(server.os, "link", side_effect=cross_filesystem_once):
            final_path, filename = server.finalize_cached_file(
                source, self.download_dir, "External Drive", ".mp4", "job-1"
            )

        self.assertEqual(filename, "External Drive.mp4")
        self.assertEqual(pathlib.Path(final_path).read_bytes(), b"copied-file")
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.download_dir)))

    def test_cleanup_refuses_to_delete_outside_cache_root(self):
        outside = os.path.join(self.temp_dir.name, "important")
        os.makedirs(outside)
        pathlib.Path(outside, "keep.txt").write_text("keep", encoding="utf-8")

        self.assertFalse(server.cleanup_job_cache(outside))
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.txt")))

    def test_orphan_cleanup_does_not_follow_symlinks(self):
        outside = os.path.join(self.temp_dir.name, "important")
        os.makedirs(outside)
        pathlib.Path(outside, "keep.txt").write_text("keep", encoding="utf-8")
        cache_root = server.get_cache_root()
        link_path = os.path.join(cache_root, "outside-link")
        try:
            os.symlink(outside, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links are unavailable")

        server.cleanup_cache_root()

        self.assertFalse(os.path.lexists(link_path))
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.txt")))

    def test_cache_root_symlink_is_rejected_without_deleting_target(self):
        outside = os.path.join(self.temp_dir.name, "important-root")
        os.makedirs(outside)
        protected = pathlib.Path(outside, "keep.txt")
        protected.write_text("keep", encoding="utf-8")
        cache_path = os.path.join(self.base_dir, server.CACHE_DIR_NAME)
        try:
            os.symlink(outside, cache_path)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links are unavailable")

        server.cleanup_cache_root()

        self.assertTrue(protected.is_file())
        with self.assertRaises(RuntimeError):
            server.get_cache_root()

    def test_outside_file_cannot_be_published_as_cached_media(self):
        outside = os.path.join(self.temp_dir.name, "outside.mp4")
        pathlib.Path(outside).write_bytes(b"not-cache")

        with self.assertRaises(ValueError):
            server.finalize_cached_file(
                outside, self.download_dir, "Outside", ".mp4", "job-1"
            )

    def test_publish_temp_cleanup_is_narrowly_scoped(self):
        owned_temp = pathlib.Path(self.download_dir, ".mediadrop-job-abc.tmp")
        ordinary_temp = pathlib.Path(self.download_dir, "ordinary.tmp")
        similarly_named = pathlib.Path(self.download_dir, ".mediadrop-keep.mp4")
        owned_temp.write_bytes(b"partial")
        ordinary_temp.write_bytes(b"keep")
        similarly_named.write_bytes(b"keep")

        server.cleanup_destination_temp_files(self.download_dir)

        self.assertFalse(owned_temp.exists())
        self.assertTrue(ordinary_temp.exists())
        self.assertTrue(similarly_named.exists())

    def test_active_cancel_keeps_slot_until_worker_cleanup(self):
        class RunningProcess:
            def __init__(self):
                self.killed = False

            def poll(self):
                return None

            def kill(self):
                self.killed = True

        proc = RunningProcess()
        server.jobs["active"] = {"status": "downloading", "proc": proc}
        server.active_downloads.add("active")

        with mock.patch.object(server, "process_download_queue") as process_queue:
            response = server.app.test_client().post("/api/cancel/active")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(proc.killed)
        self.assertEqual(server.jobs["active"]["status"], "cancelled")
        self.assertIn("active", server.active_downloads)
        process_queue.assert_not_called()

    def test_failed_download_removes_task_cache(self):
        class FailedProcess:
            stdout = iter(["ERROR: simulated failure"])
            returncode = 1

            def wait(self, timeout=None):
                return self.returncode

            def poll(self):
                return self.returncode

        server.jobs["failed"] = {
            "status": "starting",
            "progress": {"percent": None, "speed": None, "eta": None},
        }
        server.active_downloads.add("failed")

        patches = [
            mock.patch.object(server, "get_download_dir", return_value=self.download_dir),
            mock.patch.object(server, "get_ytdlp_path", return_value="yt-dlp"),
            mock.patch.object(server, "get_proxy_url", return_value=""),
            mock.patch.object(server, "get_cookie_args", return_value=[]),
            mock.patch.object(server, "get_ffmpeg_dir", return_value=None),
            mock.patch.object(server, "get_ytdlp_env", return_value={}),
            mock.patch.object(server.subprocess, "Popen", return_value=FailedProcess()),
        ]
        for patcher in patches:
            patcher.start()
        try:
            server.run_download("failed", "https://example.com/video", "video", None, "Example")
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        self.assertEqual(server.jobs["failed"]["status"], "error")
        self.assertEqual(os.listdir(server.get_cache_root()), [])
        self.assertNotIn("failed", server.active_downloads)

    def test_successful_download_publishes_then_removes_cache(self):
        class SuccessfulProcess:
            stdout = iter([])
            returncode = 0

            def wait(self, timeout=None):
                return self.returncode

            def poll(self):
                return self.returncode

        def create_completed_output(command, **kwargs):
            template = command[command.index("-o") + 1]
            pathlib.Path(template.replace("%(ext)s", "mp4")).write_bytes(b"finished")
            return SuccessfulProcess()

        server.jobs["success"] = {
            "status": "starting",
            "progress": {"percent": None, "speed": None, "eta": None},
        }
        server.active_downloads.add("success")
        patches = [
            mock.patch.object(server, "get_download_dir", return_value=self.download_dir),
            mock.patch.object(server, "get_ytdlp_path", return_value="yt-dlp"),
            mock.patch.object(server, "get_proxy_url", return_value=""),
            mock.patch.object(server, "get_cookie_args", return_value=[]),
            mock.patch.object(server, "get_ffmpeg_dir", return_value=None),
            mock.patch.object(server, "get_ytdlp_env", return_value={}),
            mock.patch.object(server.subprocess, "Popen", side_effect=create_completed_output),
        ]
        for patcher in patches:
            patcher.start()
        try:
            server.run_download("success", "https://example.com/video", "video", None, "Example")
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        job = server.jobs["success"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["filename"], "Example.mp4")
        self.assertEqual(pathlib.Path(job["file"]).read_bytes(), b"finished")
        self.assertEqual(os.listdir(server.get_cache_root()), [])
        self.assertNotIn("success", server.active_downloads)


if __name__ == "__main__":
    unittest.main()
