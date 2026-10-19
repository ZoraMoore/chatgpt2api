from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from io import BytesIO
import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from scripts.test_image_protocol import (
    ProbeError, StreamAudit, analyze_har, exit_code, parse_sse, read_limited, run_live, run_worker, select_probe_accounts, write_report,
)


def assistant(metadata=None):
    return {"v": {"message": {"id": "synthetic-message", "author": {"role": "assistant"},
                              "metadata": metadata or {}, "content": {"content_type": "text", "parts": [""]}}}}


def stream(*events):
    return "\n\n".join("data: " + (event if isinstance(event, str) else json.dumps(event)) for event in events) + "\n\n"


class ImageProtocolFieldTests(unittest.TestCase):
    def test_resolved_field_is_distinct_from_model_label(self):
        audit = StreamAudit()
        parse_sse(stream(assistant({"model_slug": "gpt-6-thinking"}), "[DONE]"), audit)
        report = audit.report(mode="live", generation={"successful": True})
        self.assertEqual("absent", report["sse"]["resolved_model_slug"]["presence"])
        self.assertEqual("gpt-6-thinking", report["model_fields"][0]["value"])
        self.assertEqual(1, exit_code(report))

    def test_resolved_field_in_snapshot_and_stream_are_reported_separately(self):
        audit = StreamAudit()
        parse_sse(stream(assistant({"model_slug": "gpt-6-thinking"}), "[DONE]"), audit)
        audit.snapshot({"default_model_slug": "gpt-6-thinking", "mapping": {
            "synthetic-node": {"message": assistant({"resolved_model_slug": "gpt-6-thinking"})["v"]["message"]},
        }})
        report = audit.report(mode="capture")
        self.assertEqual("absent", report["sse"]["resolved_model_slug"]["presence"])
        self.assertEqual("present", report["conversation_response"]["resolved_model_slug"]["presence"])
        self.assertEqual("conversation.default_model_slug", report["model_fields"][1]["location"])

    def test_scalar_patch_inherits_path_and_operation(self):
        audit = StreamAudit()
        events = [assistant({"resolved_model_slug": "gpt-"}),
                  {"p": "/message/metadata/resolved_model_slug", "o": "append", "v": "6-"},
                  {"v": "thinking"}, "[DONE]"]
        parse_sse(stream(*events), audit)
        report = audit.report(mode="capture")
        self.assertEqual("present", report["sse"]["resolved_model_slug"]["presence"])
        self.assertIn("gpt-6-thinking", report["sse"]["resolved_model_slug"]["values"])

    def test_patch_batch_and_version_record_are_supported(self):
        audit = StreamAudit()
        parse_sse(stream('"v1"', assistant(), {"o": "patch", "v": [
            {"p": "/message/metadata/resolved_model_slug", "o": "add", "v": "gpt-6-thinking"},
        ]}, "[DONE]"), audit)
        self.assertEqual(0, audit.parse_errors)
        self.assertEqual(["gpt-6-thinking"], audit.report(mode="capture")["sse"]["resolved_model_slug"]["values"])

    def test_server_metadata_is_observed_and_does_not_fake_resolved_field(self):
        audit = StreamAudit()
        audit.feed(json.dumps({"type": "server_ste_metadata", "metadata": {"model_slug": "gpt-6-thinking"}}))
        audit.feed("[DONE]")
        report = audit.report(mode="capture")
        self.assertEqual("absent", report["sse"]["resolved_model_slug"]["presence"])
        self.assertEqual("server_ste_metadata.model_slug", report["model_fields"][0]["location"])

    def test_empty_and_null_fields_still_count_as_present(self):
        for value, state in ((None, "null"), ("", "empty")):
            with self.subTest(value=value):
                audit = StreamAudit()
                parse_sse(stream(assistant({"resolved_model_slug": value}), "[DONE]"), audit)
                check = audit.report(mode="capture")["sse"]["resolved_model_slug"]
                self.assertEqual("present", check["presence"])
                self.assertEqual([], check["values"])
                self.assertEqual([state], check["value_states"])

    def test_failure_and_incomplete_stream_cannot_prove_absence(self):
        for raw, successful in ((stream(assistant(), "[DONE]"), False), (stream(assistant()), True),
                                (stream(assistant(), "{", "[DONE]"), True), ("", True)):
            with self.subTest(raw=raw, successful=successful):
                audit = StreamAudit()
                parse_sse(raw, audit)
                report = audit.report(mode="live", generation={"successful": successful})
                self.assertEqual("inconclusive", report["sse"]["resolved_model_slug"]["presence"])
                self.assertEqual(2, exit_code(report))

    def test_field_observed_before_failure_is_not_lost(self):
        audit = StreamAudit()
        parse_sse(stream(assistant({"resolved_model_slug": "gpt-6-thinking"})), audit)
        report = audit.report(mode="live", generation={"successful": False})
        self.assertEqual("present", report["sse"]["resolved_model_slug"]["presence"])
        self.assertEqual(2, exit_code(report))
        # 图片已保存也不能掩盖 worker 异常退出；字段证据仍保留。
        report["generation"]["successful"] = True
        report["worker_exit_code"] = 137
        self.assertEqual(2, exit_code(report))

    def test_prompt_thought_code_and_unknown_values_are_not_written(self):
        audit = StreamAudit()
        event = assistant({"resolved_model_slug": "secret-cookie-marker", "token": "sensitive-token-marker"})
        event["v"]["message"]["content"]["parts"] = ['{"resolved_model_slug":"gpt-6-pro"}', "private-thought-marker"]
        parse_sse(stream(event, "[DONE]"), audit)
        report = audit.report(mode="capture")
        encoded = json.dumps(report)
        for private in ("secret-cookie-marker", "sensitive-token-marker", "private-thought-marker", "gpt-6-pro"):
            self.assertNotIn(private, encoded)
        self.assertEqual(["invalid_redacted"], report["sse"]["resolved_model_slug"]["value_states"])

    def test_multiline_sse_comments_and_crlf(self):
        raw = ': heartbeat\r\nevent: delta\r\ndata: {"type":"server_ste_metadata",\r\ndata: "metadata":{"resolved_model_slug":"gpt-6-thinking"}}\r\n\r\ndata: [DONE]\r\n\r\n'
        audit = StreamAudit()
        parse_sse(raw, audit)
        self.assertEqual(0, audit.parse_errors)
        self.assertEqual("present", audit.report(mode="capture")["sse"]["resolved_model_slug"]["presence"])

    def test_multiple_streams_are_not_merged(self):
        audit = StreamAudit()
        parse_sse(stream(assistant(), "[DONE]", assistant({"resolved_model_slug": "gpt-6-pro"})), audit)
        report = audit.report(mode="capture")
        self.assertEqual("inconclusive", report["sse"]["resolved_model_slug"]["presence"])
        self.assertNotIn("gpt-6-pro", json.dumps(report))

    def test_analysis_limits_prevent_false_absence(self):
        audit = StreamAudit()
        with patch("scripts.test_image_protocol.MAX_EVENTS", 1):
            parse_sse(stream(assistant(), {}, "[DONE]"), audit)
        self.assertTrue(audit.limited)
        self.assertEqual("inconclusive", audit.report(mode="capture")["sse"]["resolved_model_slug"]["presence"])

    def test_unrecognized_snapshot_and_user_only_snapshot_are_inconclusive(self):
        for document in ({}, {"default_model_slug": "gpt-6-thinking"}, {"messages": [None]},
                         {"messages": [{"author": {"role": "user"}}]}):
            with self.subTest(document=document):
                audit = StreamAudit()
                audit.snapshot(document)
                self.assertEqual("inconclusive", audit.report(mode="capture")["conversation_response"]["resolved_model_slug"]["presence"])

    def test_har_event_source_fallback_and_prepare_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.har"
            generation = {"request": {"method": "POST", "url": "https://chatgpt.com/backend-api/f/conversation"},
                          "response": {"status": 200, "content": {"text": ""}}, "_eventSourceMessages": [
                              {"data": json.dumps(assistant({"resolved_model_slug": "gpt-6-thinking"}))}, {"data": "[DONE]"}]}
            prepare = {"request": {"method": "POST", "url": "https://chatgpt.com/backend-api/f/conversation/prepare"}}
            write_report(path, {"log": {"entries": [prepare, generation]}})
            report = analyze_har(path, None)
            self.assertEqual(1, report["capture"]["entry_index"])
            self.assertEqual("present", report["sse"]["resolved_model_slug"]["presence"])

    def test_har_multiple_generations_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.har"
            entry = {"request": {"method": "POST", "url": "https://chatgpt.com/backend-api/f/conversation"},
                     "response": {"status": 404, "content": {"text": ""}}}
            write_report(path, {"log": {"entries": [entry, entry]}})
            with self.assertRaises(ProbeError):
                analyze_har(path, None)
            report = analyze_har(path, 0)
            self.assertEqual("inconclusive", report["sse"]["resolved_model_slug"]["presence"])
            self.assertEqual(2, exit_code(report))

    def test_har_http_failure_is_not_field_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.har"
            entry = {"request": {"method": "POST", "url": "https://chatgpt.com/backend-api/f/conversation"},
                     "response": {"status": 404, "content": {"text": stream(assistant(), "[DONE]")}}}
            write_report(path, {"log": {"entries": [entry]}})
            self.assertEqual("inconclusive", analyze_har(path, 0)["sse"]["resolved_model_slug"]["presence"])

    def test_har_event_metadata_without_payload_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.har"
            entry = {"request": {"method": "POST", "url": "https://chatgpt.com/backend-api/f/conversation"},
                     "response": {"status": 200, "content": {"text": ""}},
                     "_eventSourceMessages": [{"eventName": "delta", "time": 1.0}]}
            write_report(path, {"log": {"entries": [entry]}})
            report = analyze_har(path, None)
            self.assertEqual("inconclusive", report["sse"]["resolved_model_slug"]["presence"])
            self.assertEqual("unavailable", report["capture"]["stream_source"])

    def test_har_uses_body_when_event_records_have_no_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.har"
            entry = {"request": {"method": "POST", "url": "https://chatgpt.com/backend-api/f/conversation"},
                     "response": {"status": 200, "content": {"text": stream(assistant(), "[DONE]")}},
                     "_eventSourceMessages": [{"eventName": "delta", "time": 1.0}]}
            write_report(path, {"log": {"entries": [entry]}})
            report = analyze_har(path, None)
            self.assertEqual("absent", report["sse"]["resolved_model_slug"]["presence"])
            self.assertEqual("response_content", report["capture"]["stream_source"])

    def test_input_limit_and_private_report_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_report(path, {"synthetic": True})
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            with self.assertRaises(ProbeError):
                read_limited(path, 1)

    def test_worker_cannot_run_outside_isolated_container(self):
        with patch.dict(os.environ, {"IMAGE_PROTOCOL_PROBE_ISOLATED": "0"}):
            with self.assertRaises(ProbeError):
                run_worker()


class ImageProtocolIsolationTests(unittest.TestCase):
    def test_same_email_multiple_sessions_require_token_fingerprint(self):
        old = {"email": "synthetic@example.com", "access_token": "synthetic-old-session", "source_type": "web"}
        new = {"email": "synthetic@example.com", "access_token": "synthetic-new-session", "source_type": "web"}
        with self.assertRaises(ProbeError):
            select_probe_accounts([old, new], old["email"], None)
        fingerprint = hashlib.sha256(new["access_token"].encode()).hexdigest()[:12]
        self.assertEqual([new], select_probe_accounts([old, new], old["email"], fingerprint))

    def test_unknown_or_invalid_token_fingerprint_never_falls_back(self):
        account = {"email": "synthetic@example.com", "access_token": "synthetic-session", "source_type": "web"}
        for fingerprint in ("0" * 64, "too-short", "invalid\nInjected", "a" * 65):
            with self.subTest(fingerprint=fingerprint):
                with self.assertRaises(ProbeError):
                    select_probe_accounts([account], account["email"], fingerprint)

    def test_default_pool_selection_still_preserves_native_accounts(self):
        accounts = [{"access_token": "synthetic-a", "source_type": "web"},
                    {"access_token": "synthetic-b", "source_type": "web"},
                    {"access_token": "synthetic-c", "source_type": "codex"}]
        self.assertEqual(accounts[:2], select_probe_accounts(accounts, None, None))

    @contextmanager
    def fake_runtime(self, *, timeout=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.txt"
            prompt.write_text("synthetic original prompt", encoding="utf-8")
            args = argparse.Namespace(container="chatgpt2api-image", prompt_file=prompt, image_file=None,
                                      account_email=None, output_dir=root / "result", timeout=30, model="gpt-image-2")
            commands = []
            copied_accounts = []
            private_paths = []

            def docker(arguments, **kwargs):
                commands.append(arguments)
                if arguments[0] == "inspect":
                    return subprocess.CompletedProcess(arguments, 0, json.dumps({"Image": "sha256:" + "a" * 64,
                        "State": {"Running": True}, "Config": {"Env": ["STORAGE_BACKEND=json"]},
                        "NetworkSettings": {"Networks": {"synthetic-network": {}}}}).encode())
                if arguments[0] == "cp":
                    source, destination = arguments[1], Path(arguments[2])
                    if source.endswith("/services"):
                        (destination / "protocol").mkdir(parents=True)
                        (destination / "openai_backend_api.py").write_text("# runtime code")
                        (destination / "protocol" / "conversation.py").write_text("# runtime code")
                        private_paths.append(destination.parent)
                    elif source.endswith("/utils"):
                        destination.mkdir()
                    elif source.endswith("/accounts.json"):
                        write_report(destination, [{"email": "synthetic@example.com", "access_token": "sensitive-marker", "source_type": "web"}])
                    else:
                        write_report(destination, {"image_storage": {"enabled": True, "webdav_password": "sensitive-marker"}})
                if arguments[0] == "run":
                    self.assertIn("--read-only", arguments)
                    self.assertIn("--user", arguments)
                    self.assertIn("STORAGE_BACKEND=json", arguments)
                    self.assertNotIn("--privileged", arguments)
                    self.assertNotIn("--volumes-from", arguments)
                    mounts = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--mount"]
                    self.assertTrue(any("dst=/app/services,readonly" in item for item in mounts))
                    self.assertTrue(any("dst=/app/config.json,readonly" in item for item in mounts))
                    private = private_paths[-1]
                    copied_accounts.extend(json.loads((private / "data" / "accounts.json").read_text()))
                    self.assertEqual("synthetic original prompt", (private / "prompt.txt").read_text())
                    self.assertFalse(json.loads((private / "config.json").read_text())["image_storage"]["enabled"])
                    audit = StreamAudit()
                    parse_sse(stream(assistant(), "[DONE]"), audit)
                    write_report(args.output_dir / "report.json", audit.report(mode="live", generation={"successful": not timeout}))
                    if timeout:
                        raise subprocess.TimeoutExpired(arguments, 30)
                return subprocess.CompletedProcess(arguments, 0, b"")

            with patch("scripts.test_image_protocol.run_docker", side_effect=docker):
                yield args, commands, copied_accounts, private_paths

    def test_live_clones_runtime_and_always_removes_temporary_credentials(self):
        with self.fake_runtime() as (args, commands, accounts, private_paths):
            report = run_live(args)
            self.assertTrue(report["runtime"]["isolated_data"])
            self.assertEqual("sensitive-marker", accounts[0]["access_token"])
            self.assertFalse(private_paths[0].exists())
            self.assertEqual(["rm", "--force"], commands[-1][:2])
            self.assertNotIn("sensitive-marker", json.dumps(report))
            self.assertNotIn("synthetic original prompt", json.dumps(report))
            self.assertFalse(report["runtime"]["http_wrapper_tested"])

    def test_timeout_does_not_turn_partial_report_into_field_absence(self):
        with self.fake_runtime(timeout=True) as (args, commands, _, private_paths):
            report = run_live(args)
            self.assertEqual("ProbeTimeout", report["generation"]["failure_type"])
            self.assertEqual("inconclusive", report["sse"]["resolved_model_slug"]["presence"])
            self.assertFalse(private_paths[0].exists())
            self.assertEqual("rm", commands[-1][0])


@unittest.skipUnless(os.getenv("IMAGE_PROTOCOL_TEST_RUNTIME") == "1", "仅在无网络的隔离运行镜像验证 worker")
class ImageProtocolWorkerTests(unittest.TestCase):
    def check_worker(self, *, extra_post=False, image_invalid=False, reference=False):
        from PIL import Image
        from services.openai_backend_api import OpenAIBackendAPI
        from services.protocol import openai_v1_image_edit, openai_v1_image_generations

        with tempfile.TemporaryDirectory() as directory:
            private, output = Path(directory) / "private", Path(directory) / "output"
            private.mkdir()
            output.mkdir()
            (private / "isolated-marker").touch()
            (private / "prompt.txt").write_text("synthetic original prompt", encoding="utf-8")
            buffer = BytesIO()
            Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
            image_bytes = buffer.getvalue()
            if reference:
                (private / "reference.bin").write_bytes(image_bytes)
            actual_posts = []
            observed_bodies = []

            def initialize(backend, *args, **kwargs):
                backend.account = {"email": "synthetic@example.com"}
                backend.access_token = "synthetic-current-session"

                def post(*args, **kwargs):
                    actual_posts.append((args, kwargs))
                    return SimpleNamespace(status_code=200)

                backend.session = SimpleNamespace(post=post)

            def original_stream(backend, *args, **kwargs):
                backend.session.post("https://chatgpt.com/backend-api/f/conversation", json={"model": "gpt-6-thinking", "thinking_effort": "extended"})
                event = assistant({"resolved_model_slug": "gpt-6-thinking"})
                event["v"]["conversation_id"] = "synthetic-conversation"
                yield json.dumps(event)
                yield "[DONE]"
                if extra_post:
                    backend.session.post("https://chatgpt.com/backend-api/f/conversation", json={"model": "gpt-6-thinking"})

            def handle(body):
                observed_bodies.append(body)
                backend = OpenAIBackendAPI()
                list(backend.stream_conversation(prompt=body["prompt"], model=body["model"]))
                encoded = base64.b64encode(b"not an image" if image_invalid else image_bytes).decode()
                return {"data": [{"b64_json": encoded}]}

            document = {"messages": [assistant({"resolved_model_slug": "gpt-6-thinking"})["v"]["message"]]}
            with (
                patch.dict(os.environ, {"IMAGE_PROTOCOL_PROBE_ISOLATED": "1", "IMAGE_PROTOCOL_PROBE_MODEL": "gpt-image-2"}),
                patch("scripts.test_image_protocol.PROBE_PRIVATE_DIR", private),
                patch("scripts.test_image_protocol.PROBE_OUTPUT_DIR", output),
                patch.object(OpenAIBackendAPI, "__init__", initialize),
                patch.object(OpenAIBackendAPI, "stream_conversation", original_stream),
                patch.object(OpenAIBackendAPI, "_get_conversation", return_value=document),
                patch.object(openai_v1_image_generations, "handle", side_effect=handle) as generation_handle,
                patch.object(openai_v1_image_edit, "handle", side_effect=handle) as edit_handle,
            ):
                result = run_worker()
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(1, len(actual_posts))
            self.assertEqual("synthetic original prompt", observed_bodies[0]["prompt"])
            self.assertEqual(1, observed_bodies[0]["n"])
            self.assertEqual("auto", observed_bodies[0]["quality"])
            self.assertEqual("present", report["sse"]["resolved_model_slug"]["presence"])
            self.assertEqual("present", report["conversation_response"]["resolved_model_slug"]["presence"])
            self.assertNotIn("synthetic original prompt", json.dumps(report))
            self.assertNotIn("synthetic@example.com", json.dumps(report))
            self.assertNotIn("synthetic-current-session", json.dumps(report))
            self.assertEqual([hashlib.sha256(b"synthetic-current-session").hexdigest()[:12]], report["token_fingerprints"])
            self.assertEqual(1 if reference else 0, edit_handle.call_count)
            self.assertEqual(0 if reference else 1, generation_handle.call_count)
            if extra_post or image_invalid:
                self.assertEqual(2, result)
                self.assertFalse(report["generation"]["successful"])
                self.assertFalse((output / "generated.png").exists())
                if extra_post:
                    self.assertEqual(1, report["blocked_additional_posts"])
            else:
                self.assertEqual(0, result)
                self.assertTrue(report["generation"]["successful"])
                self.assertEqual(image_bytes, (output / "generated.png").read_bytes())
                self.assertEqual([8, 8], report["generation"]["dimensions"])

    def test_worker_observes_stream_and_validates_image_without_network(self):
        self.check_worker()

    def test_worker_reference_uses_image_edit_handler(self):
        self.check_worker(reference=True)

    def test_worker_prevents_another_generation_post(self):
        self.check_worker(extra_post=True)

    def test_worker_rejects_invalid_image_even_with_resolved_field(self):
        self.check_worker(image_invalid=True)


if __name__ == "__main__":
    unittest.main()
