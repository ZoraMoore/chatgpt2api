"""容器终态测试共用的脱敏消息及流式交付夹具，不执行命令或访问上游。"""
from __future__ import annotations

import base64
import copy
from unittest.mock import Mock

from services.protocol.terminal_image_delivery import select_terminal_image_delivery
from tests import test_image_final_contract as pipeline_fixtures
from tests.test_terminal_image_delivery import message, native_document


OUTPUT_PATH = "/mnt/data/final.png"
CONVERSATION_ID = pipeline_fixtures.CID
BINDINGS = {
    "request_id": "synthetic-request",
    "turn_exchange_id": "synthetic-exchange",
    "working_turn_id": "synthetic-working-turn",
}


def container_document(output=OUTPUT_PATH + "\n"):
    document = native_document()
    document["conversation_id"] = CONVERSATION_ID
    document["messages"][0] = pipeline_fixtures.user_message()
    document["messages"][2:2] = [
        message(
            "container-call", "assistant",
            {"content_type": "code", "text": "synthetic_container_postprocess()"},
            recipient="container.exec", channel="analysis", end_turn=False,
            metadata=dict(BINDINGS),
        ),
        message(
            "container-result", "tool", {"content_type": "execution_output", "text": output},
            author={"role": "tool", "name": "container.exec"},
            metadata={**BINDINGS, "is_complete": True, "aggregate_result": {
                "status": "success", "messages": [], "final_expression_output": output,
            }},
        ),
    ]
    return document


def select(document, **kwargs):
    return select_terminal_image_delivery(
        document, conversation_id=CONVERSATION_ID,
        submitted_message_id=pipeline_fixtures.SUBMITTED, **kwargs,
    )


class ContainerTerminalPipelineTests:
    """复用已有隔离后端与配置，只让真实选择器和 SSE 交付链路参与验证。"""

    def backend(self, document):
        expected_png = pipeline_fixtures.png("blue")
        backend = pipeline_fixtures.ImageFinalContractTests().backend(
            document["messages"], sandbox_bytes={OUTPUT_PATH: expected_png},
        )
        backend._get_conversation.return_value = copy.deepcopy(document)
        backend._resolve_image_urls = Mock()
        backend.resolve_conversation_image_urls = Mock()
        backend.download_image_bytes = Mock()
        return backend, expected_png

    def run_pipeline(self, backend):
        fixture = pipeline_fixtures.ImageFinalContractTests()
        fixture.setUp()
        try:
            request = pipeline_fixtures.ConversationRequest(
                model="gpt-image-2.6-sunburst", prompt="synthetic image edit request",
                images=[base64.b64encode(pipeline_fixtures.INPUT_BYTES).decode("ascii")],
                n=1, image_transport="conversation", response_format="b64_json",
            )
            return list(pipeline_fixtures.stream_image_outputs(backend, request))
        finally:
            fixture.doCleanups()
