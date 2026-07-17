import atexit
import concurrent.futures
import errno
import importlib.util
import io
import json
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


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ProgressEstimatorTests(unittest.TestCase):
    MIB = 1024 * 1024

    def setUp(self):
        self.clock = FakeClock()
        self.estimator = server.DownloadProgressEstimator(clock=self.clock)

    def update(self, downloaded, total=None, estimate=None):
        progress = {"downloaded_bytes": downloaded}
        if total is not None:
            progress["total_bytes"] = total
        if estimate is not None:
            progress["total_bytes_estimate"] = estimate
        return self.estimator.update(progress)

    def test_stable_connection_produces_accurate_high_confidence_eta(self):
        total = 500 * self.MIB
        self.update(0, total=total)
        result = None
        for second in range(1, 21):
            self.clock.advance(1)
            result = self.update(second * 10 * self.MIB, total=total)

        self.assertEqual(result["state"], "downloading")
        self.assertEqual(result["eta_confidence"], "high")
        self.assertAlmostEqual(result["speed_bps"], 10 * self.MIB, delta=0.5 * self.MIB)
        self.assertAlmostEqual(result["eta_seconds"], 30, delta=3)

    def test_bursty_connection_is_smoothed(self):
        total = 500 * self.MIB
        downloaded = 0
        self.update(downloaded, total=total)
        eta_values = []
        for second in range(1, 25):
            self.clock.advance(1)
            downloaded += (2 if second % 2 else 18) * self.MIB
            result = self.update(downloaded, total=total)
            if second >= 15 and result["eta_seconds"] is not None:
                eta_values.append(result["eta_seconds"])

        self.assertAlmostEqual(result["speed_bps"], 10 * self.MIB, delta=2 * self.MIB)
        self.assertLess(max(eta_values) - min(eta_values), 16)

    def test_slowdown_changes_eta_gradually_but_does_not_hide_it(self):
        total = 400 * self.MIB
        downloaded = 0
        self.update(downloaded, total=total)
        for _ in range(15):
            self.clock.advance(1)
            downloaded += 10 * self.MIB
            before = self.update(downloaded, total=total)

        self.clock.advance(1)
        downloaded += 2 * self.MIB
        first_slow = self.update(downloaded, total=total)
        for _ in range(14):
            self.clock.advance(1)
            downloaded += 2 * self.MIB
            after = self.update(downloaded, total=total)

        self.assertGreater(first_slow["speed_bps"], 5 * self.MIB)
        self.assertLess(after["speed_bps"], first_slow["speed_bps"])
        self.assertGreater(after["eta_seconds"], before["eta_seconds"])

    def test_stall_suppresses_misleading_eta(self):
        total = 200 * self.MIB
        downloaded = 0
        self.update(downloaded, total=total)
        for _ in range(10):
            self.clock.advance(1)
            downloaded += 5 * self.MIB
            self.update(downloaded, total=total)
        self.clock.advance(8)
        result = self.estimator.snapshot()

        self.assertEqual(result["state"], "stalled")
        self.assertIsNone(result["eta_seconds"])

    def test_pause_time_is_not_counted_as_zero_speed(self):
        total = 200 * self.MIB
        downloaded = 0
        self.update(downloaded, total=total)
        for _ in range(10):
            self.clock.advance(1)
            downloaded += 5 * self.MIB
            self.update(downloaded, total=total)

        paused = self.estimator.pause()
        self.clock.advance(60)
        resumed = self.estimator.resume()
        self.clock.advance(1)
        after = self.update(downloaded + 5 * self.MIB, total=total)

        self.assertEqual(paused["state"], "paused")
        self.assertEqual(resumed["state"], "estimating")
        self.assertNotEqual(after["state"], "stalled")
        self.assertGreater(after["speed_bps"], self.MIB)

    def test_new_stream_is_accumulated_instead_of_resetting_to_zero(self):
        self.update(0, total=100 * self.MIB)
        self.clock.advance(10)
        self.update(100 * self.MIB, total=100 * self.MIB)
        self.clock.advance(1)
        result = self.update(self.MIB, total=10 * self.MIB)

        self.assertEqual(result["phase_index"], 2)
        self.assertEqual(result["downloaded"], 101 * self.MIB)
        self.assertEqual(result["total"], 110 * self.MIB)
        self.assertAlmostEqual(result["percent"], 10100 / 110, delta=0.1)
        self.assertEqual(result["state"], "estimating")

    def test_unknown_total_does_not_invent_an_eta(self):
        self.update(0)
        for second in range(1, 10):
            self.clock.advance(1)
            result = self.update(second * self.MIB)

        self.assertEqual(result["total"], 0)
        self.assertIsNone(result["percent"])
        self.assertIsNone(result["eta_seconds"])
        self.assertEqual(result["state"], "estimating")

    def test_estimated_total_uses_a_median_instead_of_locking_to_a_spike(self):
        self.update(0, estimate=200 * self.MIB)
        for second in range(1, 10):
            self.clock.advance(1)
            result = self.update(second * 5 * self.MIB, estimate=100 * self.MIB)

        self.assertEqual(result["total"], 100 * self.MIB)
        self.assertTrue(result["total_is_estimate"])
        self.assertEqual(result["eta_confidence"], "medium")

    def test_exact_total_replaces_an_earlier_estimate(self):
        self.update(0, estimate=200 * self.MIB)
        self.clock.advance(1)
        result = self.update(10 * self.MIB, total=120 * self.MIB)

        self.assertEqual(result["total"], 120 * self.MIB)
        self.assertFalse(result["total_is_estimate"])

    def test_postprocessing_phase_hides_download_eta(self):
        total = 100 * self.MIB
        self.update(0, total=total)
        for second in range(1, 10):
            self.clock.advance(1)
            self.update(second * 5 * self.MIB, total=total)

        result = self.estimator.set_phase("merging")

        self.assertEqual(result["state"], "merging")
        self.assertEqual(result["phase"], "merging")
        self.assertIsNone(result["eta_seconds"])
        self.assertIsNone(result["speed_bps"])

    def test_structured_progress_parser_ignores_noise(self):
        line = 'prefix ' + server.PROGRESS_PREFIX + '{"downloaded_bytes": 42, "total_bytes": 100}'
        self.assertEqual(server.parse_structured_progress(line)["downloaded_bytes"], 42)
        self.assertIsNone(server.parse_structured_progress("ordinary yt-dlp output"))
        self.assertIsNone(server.parse_structured_progress(server.PROGRESS_PREFIX + "not-json"))


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

    def test_terminate_process_tree_targets_the_unix_process_group(self):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None

        with mock.patch.object(server.sys, "platform", "darwin"), \
             mock.patch.object(server.os, "getpgid", return_value=4321), \
             mock.patch.object(server.os, "killpg") as kill_group:
            server.terminate_process_tree(proc)

        kill_group.assert_called_once_with(4321, server.signal.SIGTERM)
        proc.kill.assert_not_called()

    def test_descendant_process_ids_include_nested_ffmpeg_children(self):
        pairs = [(20, 10), (30, 20), (40, 999), (31, 20)]
        self.assertCountEqual(server.descendant_process_ids(10, pairs), [20, 30, 31])

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

    def test_hdr_preset_and_advanced_options_reach_ytdlp(self):
        captured = []

        class SuccessfulProcess:
            stdout = iter([])
            returncode = 0
            def wait(self, timeout=None): return 0
            def poll(self): return 0

        def create_output(command, **kwargs):
            captured.extend(command)
            template = command[command.index("-o") + 1]
            pathlib.Path(template.replace("%(ext)s", "mp4")).write_bytes(b"finished")
            return SuccessfulProcess()

        server.jobs["hdr"] = {"status": "starting", "progress": server.empty_progress()}
        server.active_downloads.add("hdr")
        patches = [
            mock.patch.object(server, "get_download_dir", return_value=self.download_dir),
            mock.patch.object(server, "get_ytdlp_path", return_value="yt-dlp"),
            mock.patch.object(server, "get_proxy_url", return_value=""),
            mock.patch.object(server, "get_cookie_args", return_value=[]),
            mock.patch.object(server, "get_ffmpeg_dir", return_value=None),
            mock.patch.object(server, "get_ytdlp_env", return_value={}),
            mock.patch.object(server.subprocess, "Popen", side_effect=create_output),
        ]
        for patcher in patches: patcher.start()
        try:
            server.run_download(
                "hdr", "https://example.com/video", "video", None, "HDR Example",
                "hdr", "highest", {"container": "mp4", "subtitles": True,
                "subtitle_languages": "zh.*,en.*", "metadata": True, "chapters": True},
            )
        finally:
            for patcher in reversed(patches): patcher.stop()

        selector = captured[captured.index("-f") + 1]
        self.assertIn("[dynamic_range!=SDR]", selector)
        self.assertNotIn("/best", selector)
        self.assertIn("--embed-subs", captured)
        self.assertIn("--embed-metadata", captured)
        self.assertIn("--embed-chapters", captured)

    def test_custom_format_never_falls_back_to_an_unrelated_stream(self):
        captured = []

        class SuccessfulProcess:
            stdout = iter([])
            returncode = 0
            def wait(self, timeout=None): return 0
            def poll(self): return 0

        def create_output(command, **kwargs):
            captured.extend(command)
            template = command[command.index("-o") + 1]
            pathlib.Path(template.replace("%(ext)s", "mp4")).write_bytes(b"finished")
            return SuccessfulProcess()

        server.jobs["custom"] = {"status": "starting", "progress": server.empty_progress()}
        server.active_downloads.add("custom")
        patches = [
            mock.patch.object(server, "get_download_dir", return_value=self.download_dir),
            mock.patch.object(server, "get_ytdlp_path", return_value="yt-dlp"),
            mock.patch.object(server, "get_proxy_url", return_value=""),
            mock.patch.object(server, "get_cookie_args", return_value=[]),
            mock.patch.object(server, "get_ffmpeg_dir", return_value=None),
            mock.patch.object(server, "get_ytdlp_env", return_value={}),
            mock.patch.object(server.subprocess, "Popen", side_effect=create_output),
        ]
        for patcher in patches: patcher.start()
        try:
            server.run_download(
                "custom", "https://example.com/video", "video", "337", "HDR Example"
            )
        finally:
            for patcher in reversed(patches): patcher.stop()

        selector = captured[captured.index("-f") + 1]
        self.assertEqual(selector, "337+bestaudio/337")
        self.assertNotIn("/best", selector)


class PersistenceAndApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = os.path.join(self.temp_dir.name, "data")
        os.makedirs(self.base_dir)
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

    def test_job_store_round_trip_and_trim(self):
        store = server.JobStore(os.path.join(self.base_dir, "history.sqlite3"))
        for index in range(25):
            store.save(str(index), {"status": "done", "title": f"Task {index}", "created_at": index})
        self.assertEqual(len(store.load_recent(limit=100)), 25)
        store.trim(keep=20)
        recent = store.load_recent(limit=100)
        self.assertEqual(len(recent), 20)
        self.assertEqual(recent[0]["title"], "Task 24")

    def test_interrupted_job_is_restored_and_requeued(self):
        server.get_job_store().save("resume-me", {
            "status": "downloading",
            "url": "https://example.com/video",
            "format": "video",
            "progress": {"percent": 42},
            "created_at": 1,
        })
        cache = pathlib.Path(server.get_job_cache_dir("resume-me"))
        (cache / "resume-me.mp4.part").write_bytes(b"partial")

        restored = server.restore_jobs()

        self.assertIn("resume-me", restored)
        self.assertEqual(server.jobs["resume-me"]["status"], "queued")
        self.assertIn("resume-me", server.download_queue)
        self.assertTrue(cache.exists())

    def test_api_token_blocks_unauthorized_local_requests(self):
        with mock.patch.object(server, "API_TOKEN", "secret"):
            client = server.app.test_client()
            self.assertEqual(client.get("/api/config").status_code, 403)
            response = client.get("/api/config", headers={"X-MediaDrop-Token": "secret"})
            self.assertEqual(response.status_code, 200)

    def test_config_validates_and_saves_concurrency(self):
        client = server.app.test_client()
        self.assertEqual(client.post("/api/config", json={"max_concurrent": 0}).status_code, 400)
        with mock.patch.object(server, "process_download_queue") as process_queue:
            response = client.post("/api/config", json={"max_concurrent": 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["max_concurrent"], 3)
        process_queue.assert_called_once()

    def test_reorder_queue_returns_without_reacquiring_the_queue_lock(self):
        server.download_queue.extend(["first", "second"])
        client = server.app.test_client()

        response = client.post(
            "/api/queue/reorder", json={"job_id": "second", "direction": "up"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["queued"], ["second", "first"])
        self.assertTrue(server.queue_lock.acquire(timeout=0.1))
        server.queue_lock.release()

    def test_retry_and_delete_task_history(self):
        server.jobs["old"] = {
            "status": "error", "url": "https://example.com/video", "format": "audio",
            "title": "Example", "progress": server.empty_progress(), "created_at": 1,
        }
        server.persist_job("old", force=True)
        client = server.app.test_client()
        with mock.patch.object(server, "process_download_queue"):
            retried = client.post("/api/jobs/old/retry")
        self.assertEqual(retried.status_code, 200)
        new_id = retried.get_json()["job_id"]
        self.assertEqual(server.jobs[new_id]["format"], "audio")
        self.assertEqual(client.delete("/api/jobs/old").status_code, 200)
        self.assertNotIn("old", server.jobs)

    def test_ytdlp_checksum_failure_preserves_current_binary(self):
        tools = pathlib.Path(self.base_dir) / "tools"
        tools.mkdir()
        current = tools / "yt-dlp"
        current.write_bytes(b"working version")
        release = {
            "version": "2026.07.17", "url": "https://example.com/release",
            "assets": {"yt-dlp_macos": "https://example.com/bin", "SHA2-256SUMS": "https://example.com/sums"},
        }

        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): return False

        def urlopen(url, timeout=None):
            if url.endswith("sums"):
                return Response(("0" * 64 + "  yt-dlp_macos\n").encode())
            return Response(b"corrupt")

        with mock.patch.object(server, "get_latest_ytdlp_release", return_value=release), \
             mock.patch.object(server, "get_ytdlp_asset_name", return_value="yt-dlp_macos"), \
             mock.patch.object(server.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                server.download_latest_ytdlp()
        self.assertEqual(current.read_bytes(), b"working version")


if __name__ == "__main__":
    unittest.main()
