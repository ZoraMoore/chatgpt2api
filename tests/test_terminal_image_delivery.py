import copy
import unittest

from services.protocol.terminal_image_delivery import select_terminal_image_delivery


def message(message_id, role, content, **fields):
    return {"id": message_id, "author": {"role": role}, "content": content,
            "status": "finished_successfully", "recipient": "all", "metadata": {}, **fields}


def user(message_id="user-1"):
    return message(message_id, "user", {"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file-input"}, "edit"]})


def image_result(message_id="image-1", pointer="sediment://file-output"):
    return message(message_id, "tool", {"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": pointer}]},
        author={"role": "tool", "name": "image_alias.generate"}, metadata={"image_gen_title": "Synthetic image"})


def python_pair(output="/mnt/data/final.png\n", stdout=None):
    code = "result.save(output_path)\nprint(output_path)"
    call = message("python-call", "assistant", {"content_type": "code", "text": code}, recipient="python")
    result = message("python-result", "tool", {"content_type": "execution_output", "text": output},
                     author={"role": "tool", "name": "python"}, metadata={"is_complete": True,
                     "aggregate_result": {"status": "success", "code": code,
                         "in_kernel_exception": None, "system_exception": None, "timeout_triggered": None,
                         "final_expression_output": None,
                         "messages": [{"message_type": "stream", "stream_name": "stdout",
                                       "text": stdout if stdout is not None else output}],
                         "jupyter_messages": [{"msg_type": "status", "content": {"execution_state": "idle"}}]}})
    return call, result


def native_document():
    return {"conversation_id": "conversation-1", "messages": [user(), image_result(),
        message("thought", "assistant", {"content_type": "thoughts", "thoughts": []}),
        message("recap", "assistant", {"content_type": "reasoning_recap", "content": ""}),
        message("final", "assistant", {"content_type": "text", "parts": [""]}, channel="final", end_turn=True)]}


def python_document(output="/mnt/data/final.png\n", stdout=None):
    doc = native_document()
    doc["messages"][2:2] = python_pair(output, stdout)
    return doc


def select(document, **kwargs):
    return select_terminal_image_delivery(document, conversation_id="conversation-1", **kwargs)


class TerminalImageDeliveryTests(unittest.TestCase):
    def test_native_after_python_precheck_selects_last_image(self):
        doc = native_document()
        doc["messages"][1:1] = python_pair("(1200, 1200)")
        result = select(doc, submitted_message_id="user-1")
        self.assertIsNotNone(result)
        self.assertEqual(("native_image", "image-1", ""), (result.kind, result.message_id, result.path))
        self.assertEqual("sediment://file-output", result.image_message["content"]["parts"][0]["asset_pointer"])

    def test_native_returns_only_exact_pointer_message(self):
        doc = native_document()
        doc["messages"][1]["metadata"]["references"] = [{"file_id": "file-other"}]
        doc["messages"][1]["content"]["parts"].append("file-unrelated")
        original = copy.deepcopy(doc)
        result = select(doc)
        self.assertIsNotNone(result)
        self.assertNotIn("metadata", result.image_message)
        self.assertEqual(1, len(result.image_message["content"]["parts"]))
        result.image_message["content"]["parts"][0]["asset_pointer"] = "changed"
        self.assertEqual(original, doc)
        with self.assertRaises(AttributeError):
            result.kind = "python_output"

    def test_native_selects_latest_generated_image(self):
        doc = native_document()
        doc["messages"].insert(2, image_result("image-2", "file-service://file-second"))
        result = select(doc)
        self.assertIsNotNone(result)
        self.assertEqual("image-2", result.message_id)

    def test_python_accepts_observed_output_shapes(self):
        cases = [("\n<<ImageDisplayed>>/mnt/data/final.png\n", "/mnt/data/final.png\n"),
                 ("/mnt/data/final.png\n", "/mnt/data/final.png\n"),
                 ("/mnt/data/final.png (1024, 1024)\n\n<<ImageDisplayed>>", "/mnt/data/final.png (1024, 1024)\n")]
        for output, stdout in cases:
            with self.subTest(output=output):
                result = select(python_document(output, stdout))
                self.assertIsNotNone(result)
                self.assertEqual(("python_output", "python-result", "/mnt/data/final.png", None),
                                 (result.kind, result.message_id, result.path, result.image_message))

    def test_python_accepts_matching_jupyter_stdout(self):
        doc = python_document()
        aggregate = doc["messages"][3]["metadata"]["aggregate_result"]
        aggregate["jupyter_messages"].insert(0, {"msg_type": "stream", "content": {
            "name": "stdout", "text": "/mnt/data/final.png\n"}})
        self.assertIsNotNone(select(doc))

    def test_rejects_wrong_or_missing_conversation(self):
        for value in (None, "other", [], 1):
            doc = native_document()
            doc["conversation_id"] = value
            self.assertIsNone(select(doc))
        doc = native_document()
        doc["id"] = "other"
        self.assertIsNone(select(doc))

    def test_rejects_nonterminal_or_ambiguous_final(self):
        mutations = [{"status": "in_progress"}, {"end_turn": False}, {"channel": "analysis"},
                     {"recipient": "python"}, {"content": {"content_type": "text", "parts": ["请选择第一张或第二张"]}},
                     {"metadata": {"error": "failed"}}, {"metadata": {"interrupted": True}},
                     {"metadata": {"is_complete": False}}, {"metadata": {"finish_details": {"type": "error"}}}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                doc = native_document()
                doc["messages"][-1].update(mutation)
                self.assertIsNone(select(doc))

    def test_empty_multimodal_final_uses_current_turn_generated_artifact(self):
        doc = native_document()
        doc["messages"][-1]["content"] = {"content_type": "multimodal_text", "parts": [""]}
        selected = select(doc)
        self.assertIsNotNone(selected)
        self.assertEqual(("native_image", "image-1"), (selected.kind, selected.message_id))

    def test_completed_explanation_or_system_message_before_final_is_not_new_tool_work(self):
        for later in (message("new-comment", "assistant", {"content_type": "text", "parts": ["working"]}),
                      message("new-system", "system", {"content_type": "text", "parts": [""]})):
            with self.subTest(later=later["id"]):
                doc = native_document()
                doc["messages"].insert(-1, later)
                selected = select(doc)
                self.assertIsNotNone(selected)
                self.assertEqual(("native_image", "image-1"), (selected.kind, selected.message_id))

    def test_rejects_new_work_after_candidate(self):
        work = [message("new-call", "assistant", {"content_type": "code", "text": "work()"}, recipient="python"),
                message("unknown-tool", "tool", {"content_type": "text", "parts": ["ok"]},
                        author={"role": "tool", "name": "other"}),
                message("incomplete-thought", "assistant", {"content_type": "thoughts"}, status="in_progress")]
        for later in work:
            with self.subTest(later=later["id"]):
                doc = native_document()
                doc["messages"].insert(-1, later)
                self.assertIsNone(select(doc))

    def test_rejects_missing_ambiguous_or_superseded_user(self):
        doc = native_document()
        self.assertIsNone(select(doc, submitted_message_id="missing"))
        doc["messages"].insert(0, user("older-user"))
        self.assertIsNone(select(doc))
        self.assertIsNotNone(select(doc, submitted_message_id="user-1"))
        self.assertIsNone(select(doc, submitted_message_id="older-user"))
        doc = native_document()
        doc["messages"].insert(2, user("new-user"))
        self.assertIsNone(select(doc, submitted_message_id="user-1"))
        self.assertIsNone(select(doc, submitted_message_id="new-user"))
        doc["messages"] = doc["messages"][1:]
        self.assertIsNone(select(doc))

    def test_rejects_input_pointer_reuse_even_with_other_scheme(self):
        doc = native_document()
        doc["messages"][1] = image_result(pointer="file-service://file-input")
        self.assertIsNone(select(doc))
        for key in ("id", "library_file_id"):
            doc = native_document()
            doc["messages"][0]["metadata"] = {"attachments": [{key: "file-output"}]}
            self.assertIsNone(select(doc))

    def test_rejects_nonimage_tool_and_ambiguous_pointers(self):
        for tool in ("python", "container.exec", "container.open_image", "open_image", "container"):
            doc = native_document()
            doc["messages"][1]["author"]["name"] = tool
            self.assertIsNone(select(doc))
        for pointer in ("https://example.test/image.png", "sediment://file-output/evil", "", None):
            doc = native_document()
            doc["messages"][1]["content"]["parts"][0]["asset_pointer"] = pointer
            self.assertIsNone(select(doc))
        doc = native_document()
        doc["messages"][1]["content"]["parts"].append({"content_type": "image_asset_pointer", "asset_pointer": "sediment://file-second"})
        self.assertIsNone(select(doc))

    def test_rejects_failed_image_result(self):
        for mutation in ({"status": "in_progress"}, {"metadata": {"error": "failed"}},
                         {"content": {"content_type": "execution_output", "parts": [
                             {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file-output"}]}}):
            doc = native_document()
            doc["messages"][1].update(mutation)
            self.assertIsNone(select(doc))

    def test_rejects_unknown_image_preview_without_generation_marker(self):
        doc = native_document()
        doc["messages"][1]["metadata"] = {}
        self.assertIsNone(select(doc))
        for title in ([], {}, "", " " * 10, "x" * 2049):
            doc["messages"][1]["metadata"] = {"image_gen_title": title}
            self.assertIsNone(select(doc))
        doc["messages"][1]["metadata"] = {"async_task_type": "image_gen"}
        self.assertIsNotNone(select(doc))

    def test_rejects_active_or_unknown_async_status(self):
        for status in ("pending", "in_progress", "unknown", {}, []):
            doc = native_document()
            doc["async_status"] = status
            self.assertIsNone(select(doc))
        doc = native_document()
        doc["async_status"] = None
        self.assertIsNotNone(select(doc))

    def test_python_requires_preceding_generated_image(self):
        doc = python_document()
        del doc["messages"][1]
        self.assertIsNone(select(doc))

    def test_python_requires_successful_matching_call(self):
        for mutation in ({"recipient": "container.exec"}, {"status": "in_progress"},
                         {"content": {"content_type": "text", "parts": ["code"]}},
                         {"content": {"content_type": "code", "text": "different code"}}):
            doc = python_document()
            doc["messages"][2].update(mutation)
            self.assertIsNone(select(doc))
        doc = python_document()
        del doc["messages"][2]
        self.assertIsNone(select(doc))

    def test_python_rejects_failures_timeouts_and_unknown_results(self):
        for key, value in (("status", "error"), ("in_kernel_exception", {}), ("system_exception", "boom"),
                           ("timeout_triggered", True), ("messages", [{"message_type": "error", "text": "/mnt/data/final.png"}]),
                           ("messages", [{"message_type": "stream", "stream_name": "stderr", "text": "/mnt/data/final.png"}]),
                           ("messages", []), ("jupyter_messages", [{"msg_type": "error", "content": {}}]),
                           ("jupyter_messages", [{"msg_type": "unknown", "content": {}}])):
            with self.subTest(key=key, value=value):
                doc = python_document()
                doc["messages"][3]["metadata"]["aggregate_result"][key] = value
                self.assertIsNone(select(doc))
        doc = python_document()
        del doc["messages"][3]["metadata"]["aggregate_result"]
        self.assertIsNone(select(doc))

    def test_python_rejects_free_text_multiple_paths_and_path_traversal(self):
        for output in ("Saved image to /mnt/data/final.png", "/mnt/data/a.png\n/mnt/data/b.png",
                       "Traceback: /mnt/data/final.png", "/mnt/data/../secret.png", "/mnt/data/%2e%2e/secret.png",
                       "/mnt/data/%252e%252e/secret.png", "/mnt/data/a\\b.png", "/mnt/data/final.txt",
                       "print('/mnt/data/final.png')", "/mnt/data/final.png?query=1", "/mnt/data/final.png\x00"):
            with self.subTest(output=output):
                self.assertIsNone(select(python_document(output)))

    def test_python_rejects_different_structured_path(self):
        self.assertIsNone(select(python_document("/mnt/data/final.png", "/mnt/data/other.png")))
        doc = python_document()
        doc["messages"][3]["metadata"]["aggregate_result"]["jupyter_messages"].insert(0, {
            "msg_type": "stream", "content": {"name": "stdout", "text": "/mnt/data/other.png"}})
        self.assertIsNone(select(doc))

    def test_python_does_not_scan_code_for_path(self):
        doc = python_document("\n<<ImageDisplayed>>", "")
        code = "result.save('/mnt/data/final.png')"
        doc["messages"][2]["content"]["text"] = code
        doc["messages"][3]["metadata"]["aggregate_result"]["code"] = code
        self.assertIsNone(select(doc))

    def test_rejects_stale_snapshot_against_known_stream_ids(self):
        doc = python_document()
        self.assertIsNotNone(select(doc, expected_final_message_id="final", expected_image_message_id="image-1",
                                    required_tool_message_ids=("image-1", "python-result")))
        for kwargs in ({"expected_final_message_id": "missing"}, {"expected_image_message_id": "missing"},
                       {"required_tool_message_ids": ("missing",)}, {"required_tool_message_ids": ("python-call",)},
                       {"required_tool_message_ids": "image-1"}):
            self.assertIsNone(select(doc, **kwargs))

    def test_rejects_stale_snapshot_missing_live_tool_call(self):
        doc = python_document()
        self.assertIsNotNone(select(doc, required_tool_calls={"python-call": "python"}))
        for calls in ({"later-call": "python"}, {"python-call": "container.exec"},
                      {"python-result": "python"}, {"final": "all"}, [], {"python-call": []}):
            self.assertIsNone(select(doc, required_tool_calls=calls))

    def test_flat_document_rejects_incomplete_page_or_wrong_current_node(self):
        for fields in ({"current_node": "other"}, {"page_info": {"has_previous_page": True}},
                       {"page_info": {"has_next_page": True}}, {"page_info": []}):
            doc = native_document()
            doc.update(fields)
            self.assertIsNone(select(doc))
        doc = native_document()
        doc.update(current_node="final", page_info={"has_previous_page": False, "has_next_page": False})
        self.assertIsNotNone(select(doc))

    def test_mapping_only_follows_current_branch(self):
        doc = native_document()
        messages = doc.pop("messages")
        mapping = {"root": {"message": None, "parent": None}}
        parent = "root"
        for item in messages:
            mapping[item["id"]] = {"message": item, "parent": parent}
            parent = item["id"]
        mapping["inactive"] = {"message": user("unrelated"), "parent": "root"}
        doc.update(mapping=mapping, current_node="final")
        self.assertIsNotNone(select(doc))
        doc["current_node"] = "inactive"
        self.assertIsNone(select(doc))
        doc["current_node"] = "final"
        mapping["root"]["parent"] = "final"
        self.assertIsNone(select(doc))
        mapping["root"]["parent"] = "missing"
        self.assertIsNone(select(doc))

    def test_rejects_bad_types_duplicate_ids_and_bounded_inputs(self):
        for doc in (None, [], {}, {"conversation_id": "conversation-1", "messages": [None]}):
            self.assertIsNone(select(doc))
        doc = native_document()
        doc["messages"][1]["id"] = "user-1"
        self.assertIsNone(select(doc))
        for path, value in (("author", []), ("metadata", []), ("content", []), ("id", "x" * 129)):
            doc = native_document()
            doc["messages"][1][path] = value
            self.assertIsNone(select(doc))
        doc = native_document()
        doc["messages"] *= 5000
        self.assertIsNone(select(doc))
        self.assertIsNone(select(python_document("x" * 70000)))

    def test_rejects_voice_final_and_missing_text_evidence(self):
        doc = native_document()
        doc["messages"][-1]["metadata"]["bidi_voice_fem_message"] = True
        self.assertIsNone(select(doc))
        doc = native_document()
        del doc["messages"][-1]["content"]["parts"]
        self.assertIsNone(select(doc))

    def test_rejects_content_filter_or_refusal_despite_success_status(self):
        for index in (1, -1):
            for reason in ("content_filter", "refusal"):
                with self.subTest(index=index, reason=reason):
                    doc = native_document()
                    doc["messages"][index]["metadata"]["finish_details"] = {"type": reason}
                    self.assertIsNone(select(doc))

    def test_malformed_nested_types_fail_closed(self):
        cases = [(1, ("author", "role")), (-1, ("metadata", "finish_details", "type")),
                 (4, ("content", "content_type")),
                 (3, ("metadata", "aggregate_result", "jupyter_messages", 0, "msg_type")),
                 (3, ("metadata", "aggregate_result", "jupyter_messages", 0, "content", "execution_state"))]
        for index, keys in cases:
            for invalid in ([], {}, 42):
                with self.subTest(index=index, keys=keys, invalid=invalid):
                    doc = python_document()
                    obj = doc["messages"][index]
                    for key in keys[:-1]:
                        if key not in obj and isinstance(obj, dict):
                            obj[key] = {}
                        obj = obj[key]
                    obj[keys[-1]] = invalid
                    self.assertIsNone(select(doc))


if __name__ == "__main__":
    unittest.main()
