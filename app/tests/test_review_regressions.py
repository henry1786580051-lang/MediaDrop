import atexit
import errno
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest import mock

from app import app as server
from app.data_lock import DataDirectoryLock


atexit.unregister(server.cleanup_all_jobs)


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name, "data")
        self.destination = pathlib.Path(self.temp.name, "downloads")
        self.base.mkdir()
        self.destination.mkdir()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in (
            ("get_base_dir", str(self.base)), ("get_download_dir", str(self.destination)),
            ("get_ytdlp_path", "yt-dlp"), ("get_proxy_url", ""), ("get_cookie_args", []),
            ("get_ffmpeg_dir", None), ("get_ytdlp_env", {}), ("get_ytdlp_javascript_args", []),
            ("load_config", {"max_concurrent": 2, "proxy_url": ""}),
        ):
            self.patches.enter_context(mock.patch.object(server, name, return_value=value))
        self.patches.enter_context(mock.patch.object(server, "API_TOKEN", ""))
        server.jobs.clear()
        server.active_downloads.clear()
        server.download_queue.clear()
        server.shutdown_event.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server.jobs.clear()
        server.active_downloads.clear()
        server.download_queue.clear()
        server.shutdown_event.clear()

    def task(self, job_id="task", status="starting"):
        job = {"status": status, "url": "https://example.com/video", "format": "video", "progress": server.empty_progress()}
        server.jobs[job_id] = job
        return job

    def partial(self, job_id):
        path = pathlib.Path(server.get_job_cache_dir(job_id), job_id + ".mp4.part")
        path.write_bytes(b"partial-data")
        return path

    def run_task(self, job_id="task", **kwargs):
        server.run_download(job_id, "https://example.com/video", "video", None, "Example", **kwargs)

    def test_restore_keeps_paused_task_older_than_history_limit(self):
        self.task("paused", "paused")
        partial = self.partial("paused")
        server.persist_job("paused", force=True)
        store = server.get_job_store()
        for index in range(200):
            store.save(f"history-{index}", {"status": "cancelled"})
        server.jobs.clear()
        server.restore_jobs()
        self.assertEqual(server.jobs["paused"]["status"], "queued")
        self.assertIn("paused", server.download_queue)
        self.assertTrue(partial.exists())
        self.assertIn("paused", {job["id"] for job in store.load_recent(1000)})

    def test_trim_only_limits_nonrecoverable_history(self):
        store = server.get_job_store()
        store.save("active", {"status": "queued"})
        store.save("retryable", {"status": "error", "resumable": True})
        for index in range(30):
            store.save(f"history-{index}", {"status": "done"})
        store.trim(keep=20)
        retained = store.load_recent(1000)
        self.assertEqual(len(retained), 22)
        self.assertTrue({"active", "retryable"}.issubset({job["id"] for job in retained}))

    def test_runtime_history_cleanup_preserves_resumable_cache(self):
        self.task("paused", "paused")
        self.task("retryable", "error")["resumable"] = True
        partials = [self.partial(job_id) for job_id in ("paused", "retryable")]
        for index in range(251):
            self.task(f"history-{index}", "cancelled")
        server.cleanup_old_jobs()
        self.assertEqual(len(server.jobs), 202)
        self.assertTrue(all(path.exists() for path in partials))

    def test_history_cleanup_waits_for_cancelled_worker_to_finish(self):
        self.task("finishing", "cancelled")
        partial = self.partial("finishing")
        server.active_downloads.add("finishing")
        for index in range(251):
            self.task(f"history-{index}", "cancelled")
        server.cleanup_old_jobs()
        self.assertIn("finishing", server.jobs)
        self.assertTrue(partial.exists())
        self.assertEqual(len(server.jobs), 201)

    def test_shutdown_preserves_partial_before_cleanup_marks_job_interrupted(self):
        job = self.task()
        partial = self.partial("task")
        server.active_downloads.add("task")

        def stop_during_download(*args):
            server.shutdown_event.set()
            return 1, ["interrupted"]

        with mock.patch.object(server, "run_ytdlp_download_process", side_effect=stop_during_download), \
             mock.patch.object(server, "process_download_queue") as advance:
            self.run_task()
        self.assertEqual(job["status"], "interrupted")
        self.assertTrue(partial.exists())
        self.assertEqual(server.get_job_store().load_recent()[0]["status"], "interrupted")
        self.assertNotIn("task", server.active_downloads)
        advance.assert_not_called()

    def test_shutdown_stops_all_processes_even_when_persistence_fails(self):
        for job_id in ("first", "second"):
            self.task(job_id)["proc"] = mock.Mock()
            server.jobs[job_id]["proc"].poll.return_value = None
        self.task("finished", "done")
        with mock.patch.object(server, "persist_job", side_effect=OSError("disk full")), \
             mock.patch.object(server, "terminate_process_tree") as terminate:
            server.cleanup_all_jobs()
        self.assertEqual(terminate.call_count, 2)
        self.assertEqual(server.jobs["finished"]["status"], "done")
        for job_id in ("first", "second"):
            self.assertEqual(server.jobs[job_id]["status"], "interrupted")
            self.assertTrue(server.jobs[job_id]["_stop_event"].is_set())

    def test_signal_handler_unwinds_before_trying_to_reacquire_locks(self):
        with server.queue_lock, mock.patch.object(server, "cleanup_all_jobs") as cleanup:
            with self.assertRaises(SystemExit):
                server.signal_handler(server.signal.SIGTERM, None)
        cleanup.assert_not_called()

    def test_interrupted_startup_cleans_up_before_releasing_profile_lock(self):
        events = []
        with mock.patch.object(server, "DataDirectoryLock") as lock, \
             mock.patch.object(server, "initialize_download_state", side_effect=SystemExit), \
             mock.patch.object(server, "cleanup_all_jobs", side_effect=lambda: events.append("cleanup")):
            lock.return_value.__exit__.side_effect = lambda *args: events.append("unlock")
            with self.assertRaises(SystemExit):
                server.run_server()
        self.assertEqual(events, ["cleanup", "unlock"])

    def test_late_progress_cannot_overwrite_persisted_cancellation(self):
        job = self.task(status="downloading")
        store = server.get_job_store()
        save = store.save
        started, release, second_done = threading.Event(), threading.Event(), threading.Event()

        def delayed_save(job_id, snapshot):
            if snapshot["status"] == "downloading":
                started.set()
                release.wait(timeout=3)
            save(job_id, snapshot)

        def save_cancelled():
            server.persist_job("task", force=True)
            second_done.set()

        first = threading.Thread(target=server.persist_job, args=("task", True))
        second = threading.Thread(target=save_cancelled)
        with mock.patch.object(store, "save", side_effect=delayed_save):
            try:
                first.start()
                self.assertTrue(started.wait(timeout=2))
                with server.jobs_lock:
                    job["status"] = "cancelled"
                second.start()
                self.assertFalse(second_done.wait(timeout=0.05))
            finally:
                release.set()
                first.join(timeout=3)
                if second.ident is not None:
                    second.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_done.is_set())
        self.assertEqual(store.load_recent()[0]["status"], "cancelled")

    def test_pause_cannot_revive_a_cancelled_task_with_a_live_process(self):
        job = self.task(status="cancelled")
        job["proc"] = mock.Mock()
        job["proc"].poll.return_value = None
        with mock.patch.object(server, "_suspend_process") as suspend:
            response = self.client.post("/api/pause/task")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(job["status"], "cancelled")
        suspend.assert_not_called()

    def test_invalid_output_directory_releases_download_slot(self):
        job = self.task()
        server.active_downloads.add("task")
        with mock.patch.object(server, "get_download_dir", side_effect=PermissionError("offline drive")), \
             mock.patch.object(server, "process_download_queue") as advance, \
             mock.patch.object(server.subprocess, "Popen") as popen:
            self.run_task()
        self.assertEqual(job["status"], "error")
        self.assertIn("Download directory", job["error"])
        self.assertNotIn("task", server.active_downloads)
        advance.assert_called_once()
        popen.assert_not_called()

    def test_invalid_output_directory_does_not_block_startup(self):
        with mock.patch.object(server, "restore_jobs") as restore, \
             mock.patch.object(server, "get_download_dir", side_effect=PermissionError("offline drive")):
            server.initialize_download_state()
        restore.assert_called_once()

    def test_persistence_failure_cannot_leak_worker_slot(self):
        self.task()
        server.active_downloads.add("task")
        with mock.patch.object(server, "get_download_dir", side_effect=PermissionError("offline")), \
             mock.patch.object(server, "persist_job", side_effect=OSError("disk full")), \
             mock.patch.object(server, "process_download_queue"):
            self.run_task()
        self.assertNotIn("task", server.active_downloads)

    def test_thread_start_failure_releases_queue_slot(self):
        self.task("task", "queued")
        server.download_queue.append("task")
        with mock.patch.object(server.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("cannot start worker")
            server.process_download_queue()
        self.assertEqual(server.jobs["task"]["status"], "error")
        self.assertEqual(server.active_downloads, set())

    def test_cancellation_during_retry_does_not_start_another_process(self):
        job = self.task()
        server.active_downloads.add("task")
        process = mock.Mock(returncode=1)
        process.stdout = iter(["ERROR: HTTP Error 403: Forbidden"])
        process.poll.return_value = 1

        def cancel_during_wait(job, delay):
            self.assertEqual(self.client.post("/api/cancel/task").status_code, 200)
            return False

        with mock.patch.object(server.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(server, "wait_for_download_retry", side_effect=cancel_during_wait), \
             mock.patch.object(server, "process_download_queue"):
            self.run_task()
        popen.assert_called_once()
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertNotIn("task", server.active_downloads)

    def test_cancel_before_process_registration_still_terminates_it(self):
        job = self.task()
        server.active_downloads.add("task")

        class Process:
            returncode = None
            stdout = iter([server.PROGRESS_PREFIX + json.dumps({"downloaded_bytes": 1})])

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        process = Process()

        def spawn(*args, **kwargs):
            self.assertEqual(self.client.post("/api/cancel/task").status_code, 200)
            return process

        with mock.patch.object(server.subprocess, "Popen", side_effect=spawn), \
             mock.patch.object(server, "process_download_queue"):
            self.run_task()
        self.assertEqual(process.returncode, -9)
        self.assertEqual(job["status"], "cancelled")

    def test_cancelled_copy_never_publishes_a_file(self):
        job = self.task()
        source = pathlib.Path(server.get_job_cache_dir("task"), "task.mp4")
        source.write_bytes(b"x" * (2 * 1024 * 1024))
        original_check = server.ensure_download_active
        checks = 0

        def stop_mid_copy(current_job):
            nonlocal checks
            checks += 1
            if checks == 3:
                job["status"] = "cancelled"
                server.get_download_stop_event(job).set()
            original_check(current_job)

        with mock.patch.object(server.os, "link", side_effect=OSError(errno.EXDEV, "different drive")), \
             mock.patch.object(server, "ensure_download_active", side_effect=stop_mid_copy):
            with self.assertRaises(server.DownloadStopped):
                server.finalize_cached_file(str(source), str(self.destination), "Example", ".mp4", "task", job=job)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertTrue(source.exists())

    def test_commit_cannot_be_overwritten_by_a_late_cancel(self):
        job = self.task()
        source = pathlib.Path(server.get_job_cache_dir("task"), "task.mp4")
        source.write_bytes(b"complete")
        server.finalize_cached_file(str(source), str(self.destination), "Example", ".mp4", "task", job=job)
        response = self.client.post("/api/cancel/task")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(job["status"], "done")
        self.assertTrue(pathlib.Path(job["file"]).exists())

    def test_retry_and_delete_wait_for_worker_cleanup(self):
        self.task("task", "error")["resumable"] = True
        server.active_downloads.add("task")
        self.assertEqual(self.client.post("/api/jobs/task/retry").status_code, 409)
        self.assertEqual(self.client.delete("/api/jobs/task").status_code, 400)

    def test_profile_lock_blocks_a_second_process_and_releases_on_close(self):
        code = "from app.data_lock import DataDirectoryLock\nimport sys\nwith DataDirectoryLock(sys.argv[1]):\n print('acquired')"
        with DataDirectoryLock(str(self.base)):
            result = subprocess.run([sys.executable, "-B", "-c", code, str(self.base)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already using this data directory", result.stderr)
        with DataDirectoryLock(str(self.base)):
            self.assertTrue((self.base / "server.lock").exists())

    def test_video_container_is_explicitly_remuxed_for_custom_and_auto_formats(self):
        for container in ("mp4", "mkv", "webm"):
            for format_id in (None, "315"):
                with self.subTest(container=container, format_id=format_id):
                    command, extension, _ = server.build_download_command(
                        "task", str(self.base), "https://example.com/video", "video", format_id,
                        options={"container": container},
                    )
                    self.assertEqual(command[command.index("--remux-video") + 1], container)
                    self.assertEqual(extension, "." + container)

    def test_webm_cover_and_compatibility_mode_are_rejected_before_queueing(self):
        for options, preset in (({"container": "webm", "embed_thumbnail": True}, "highest"),
                                ({"container": "webm"}, "compatible")):
            with self.subTest(options=options, preset=preset):
                response = self.client.post("/api/download", json={
                    "url": "https://example.com/video", "format": "video", "preset": preset, "options": options,
                })
                self.assertEqual(response.status_code, 400)
                self.assertIn("WebM", response.get_json()["error"])
        self.assertEqual(server.jobs, {})

    def test_missing_bundled_probe_fails_before_download(self):
        self.task()
        with mock.patch.object(server, "get_ffmpeg_dir", return_value=str(self.base)), \
             mock.patch.object(server.subprocess, "Popen") as popen:
            self.run_task(options={"container": "mkv", "embed_thumbnail": True})
        popen.assert_not_called()
        self.assertIn("FFprobe", server.jobs["task"]["error"])

    def test_audio_and_image_do_not_receive_video_remux_options(self):
        for format_choice in ("audio", "image"):
            command, _, _ = server.build_download_command(
                "task", str(self.base), "https://example.com/video", format_choice, None,
                options={"container": "webm", "embed_thumbnail": True},
            )
            self.assertNotIn("--remux-video", command)

    def test_webm_selector_chooses_supported_video_and_audio(self):
        from yt_dlp import YoutubeDL

        formats = [
            {"format_id": "vp9", "vcodec": "vp9", "acodec": "none", "ext": "webm", "height": 1080},
            {"format_id": "avc", "vcodec": "avc1.640028", "acodec": "none", "ext": "mp4", "height": 2160},
            {"format_id": "opus", "vcodec": "none", "acodec": "opus", "ext": "webm"},
            {"format_id": "aac", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a"},
        ]
        for item in formats:
            item.update({"url": "https://example.com/" + item["format_id"], "protocol": "https"})
        for preset in ("recommended", "highest", "smallest"):
            command, _, _ = server.build_download_command(
                "task", str(self.base), "https://example.com/video", "video", None,
                preset=preset, options={"container": "webm"},
            )
            with YoutubeDL({"quiet": True, "no_warnings": True, "cachedir": False, "merge_output_format": "webm"}) as ydl:
                select = ydl.build_format_selector(command[command.index("-f") + 1])
                selected = list(select({"formats": formats, "incomplete_formats": False, "has_merged_format": False}))[0]
            self.assertEqual({item["format_id"] for item in selected["requested_formats"]}, {"vp9", "opus"})

    def test_diagnostics_redact_urls_paths_headers_and_credentials(self):
        job = self.task("private", "error")
        secrets = ["URLSECRET", "PASSSECRET", "BEARERSECRET", "COOKIESECRET", "TOKENSECRET", "ALICESECRET", "EMAILSECRET"]
        job["error"] = "ERROR: HTTP Error 403 at https://alice:PASSSECRET@example.com/private/URLSECRET?signature=URLSECRET#URLSECRET"
        job["error_detail"] = "\n".join([
            "Authorization: Bearer BEARERSECRET", "Cookie: SID=COOKIESECRET", "access_token=TOKENSECRET",
            "C:\\Users\\ALICESECRET\\My Downloads\\video.mp4",
            "'/Users/ALICESECRET/My Downloads/video.mp4'", "EMAILSECRET@example.com",
        ])
        with mock.patch.object(server, "load_config", return_value={
            "download_dir": "/Users/ALICESECRET/My Downloads", "proxy_url": "http://alice:PASSSECRET@example.com:8080",
        }), mock.patch.object(server, "run_ytdlp_version", return_value="2026.08.19"), \
             mock.patch.object(server, "_command_version", return_value="ffmpeg 8.1.2"):
            response = self.client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertIn("HTTP Error 403", text)
        self.assertIn("https://example.com/", text)
        self.assertIn("URLSECRET", job["error"])

    def test_diagnostic_redaction_handles_quoted_and_unc_paths(self):
        samples = [
            '"C:\\Users\\PRIVATEUSER\\file with spaces.txt"',
            r"\\server\PRIVATEUSER\cookies.txt", '/secret/PRIVATEUSER/file.txt',
            '"token": "PRIVATEUSER"', "https://example.com:invalid/PRIVATEUSER",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertNotIn("PRIVATEUSER", server.redact_diagnostic_text(sample))
        self.assertIsNone(server.redact_diagnostic_text(None))


class YtdlpSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundled = pathlib.Path(self.temp.name, "bundled")
        self.updated = pathlib.Path(self.temp.name, "updated")
        self.bundled.write_bytes(b"bundled")
        self.updated.write_bytes(b"updated")
        server._ytdlp_version_cache.clear()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(server, "get_bundled_ytdlp_path", return_value=str(self.bundled)))
        self.patches.enter_context(mock.patch.object(server, "get_updatable_ytdlp_path", return_value=str(self.updated)))

    def versions(self, bundled, updated):
        return mock.patch.object(server, "run_ytdlp_version", side_effect=lambda path: bundled if path == str(self.bundled) else updated)

    def test_new_bundled_engine_wins_over_old_user_copy(self):
        with self.versions("2026.08.19", "2026.07.04"):
            self.assertEqual(server.get_ytdlp_path(), str(self.bundled))

    def test_newer_user_nightly_is_not_downgraded(self):
        with self.versions("2026.08.19", "2026.08.20.122307"):
            self.assertEqual(server.get_ytdlp_path(), str(self.updated))

    def test_broken_user_copy_falls_back_to_bundled_engine(self):
        with self.versions("2026.08.19", None):
            self.assertEqual(server.get_ytdlp_path(), str(self.bundled))

    def test_identical_versions_prefer_bundled_engine(self):
        with self.versions("2026.08.19", "2026.08.19"):
            self.assertEqual(server.get_ytdlp_path(), str(self.bundled))

    def test_version_probes_are_cached_and_invalidated_after_replacement(self):
        with self.versions("2026.08.19", "2026.07.04") as probe:
            server.get_ytdlp_path()
            server.get_ytdlp_path()
            self.assertEqual(probe.call_count, 2)
            self.updated.write_bytes(b"a new binary")
            server.get_ytdlp_path()
            self.assertEqual(probe.call_count, 3)

    def test_version_status_reports_the_selected_engine(self):
        with self.versions("2026.08.19", "2026.07.04"), \
             mock.patch.object(server, "get_latest_ytdlp_release", return_value={"version": "2026.08.19"}):
            result = server.get_ytdlp_versions()
        self.assertEqual(result["current"], "2026.08.19")
        self.assertFalse(result["using_updated"])
        self.assertFalse(result["update_available"])


if __name__ == "__main__":
    unittest.main()
