"""网页根消息补丁与完成标记兼容性：仅操作本地tracker，不访问上游。"""
from __future__ import annotations

import copy
import unittest

from services.protocol.final_image import FinalImageTracker


CID = "synthetic-tracker-conversation"


def message(message_id="synthetic-final", *, complete=False, parts=None):
    return {
        "id": message_id, "author": {"role": "assistant"}, "recipient": "all", "channel": "final",
        "content": {"content_type": "text", "parts": [""] if parts is None else parts},
        "status": "finished_successfully" if complete else "in_progress", "end_turn": complete,
        "metadata": {},
    }


class DeliveryTrackerCompatibilityTests(unittest.TestCase):
    def test_root_replace_establishes_complete_message_and_final_artifact(self):
        final = message(complete=True, parts=["[PNG](sandbox:/mnt/data/final.png)"])
        for initial in (None, message("earlier-final")):
            with self.subTest(initial=initial is not None):
                tracker = FinalImageTracker()
                if initial is not None:
                    tracker.update({"conversation_id": CID, "message": initial})
                tracker.update({"conversation_id": CID, "p": "/message", "o": "replace", "v": final})
                self.assertEqual(final, tracker.current)
                self.assertEqual("synthetic-final", tracker.final["id"])
                self.assertEqual(["/mnt/data/final.png"], tracker.paths())
                self.assertFalse(tracker.is_complete())
                tracker.update({"type": "message_stream_complete", "conversation_id": CID})
                self.assertTrue(tracker.is_complete())

    def test_root_append_updates_completion_fields_without_losing_message(self):
        tracker = FinalImageTracker()
        pending = message(parts=["[PNG](sandbox:/mnt/data/final.png)"])
        tracker.update({"conversation_id": CID, "message": pending})
        tracker.update({"p": "/message", "o": "append", "v": {
            "status": "finished_successfully", "end_turn": True,
        }})
        self.assertEqual("synthetic-final", tracker.current["id"])
        self.assertEqual(pending["content"], tracker.current["content"])
        self.assertEqual("finished_successfully", tracker.current["status"])
        self.assertIs(True, tracker.current["end_turn"])
        tracker.update({"type": "message_stream_complete", "conversation_id": CID})
        self.assertTrue(tracker.is_complete())
        self.assertEqual(["/mnt/data/final.png"], tracker.paths())

    def test_complete_marker_with_message_in_same_frame_preserves_completion(self):
        final = message(complete=True, parts=["[PNG](sandbox:/mnt/data/final.png)"])
        envelopes = (
            {"message": final},
            {"v": {"conversation_id": CID, "message": final}},
        )
        for envelope in envelopes:
            with self.subTest(nested="v" in envelope):
                tracker = FinalImageTracker()
                tracker.update({"type": "message_stream_complete", "conversation_id": CID, **copy.deepcopy(envelope)})
                self.assertTrue(tracker.stream_complete)
                self.assertTrue(tracker.is_complete())
                self.assertEqual(final, tracker.current)
                self.assertEqual(["/mnt/data/final.png"], tracker.paths())

    def test_root_replace_switching_id_clears_inherited_scalar_and_batch_context(self):
        for old_batch in (False, True):
            for root_batch in (False, True):
                with self.subTest(old_batch=old_batch, root_batch=root_batch):
                    tracker = FinalImageTracker()
                    tracker.update({"conversation_id": CID, "message": message("old-message")})
                    operation = {"p": "/message/content/parts/0", "o": "append", "v": "old content"}
                    tracker.update({"o": "patch", "v": [operation]} if old_batch else operation)
                    root = {"p": "/message", "o": "replace", "v": message("new-message")}
                    tracker.update({"o": "patch", "v": [root]} if root_batch else root)
                    tracker.update({"v": "unexpected inherited scalar"})
                    tracker.update({"o": "patch", "v": [{"v": "unexpected inherited batch"}]})
                    self.assertEqual("new-message", tracker.current["id"])
                    self.assertEqual([""], tracker.current["content"]["parts"])
                    self.assertFalse(tracker.stream_complete)
                    tracker.update({"p": "/message/content/parts/0", "o": "append", "v": "new content"})
                    self.assertEqual(["new content"], tracker.current["content"]["parts"])


if __name__ == "__main__":
    unittest.main()
