import asyncio
import unittest
from unittest.mock import Mock, patch
from fastapi.responses import JSONResponse
from api.app import create_app
from services.image_delivery_trace import ImageDeliveryTraceMiddleware, get_image_delivery_trace
from services.log_service import LoggedCall


class ImageDeliveryIntegrationTests(unittest.TestCase):
    def test_image_api_installs_transport_trace(self):
        self.assertIn(ImageDeliveryTraceMiddleware, [m.cls for m in create_app().user_middleware])

    def test_generation_and_transport_share_identity_without_claiming_receipt(self):
        logger = Mock()
        seen = []
        async def application(scope, receive, send):
            def handler():
                seen.append(get_image_delivery_trace())
                return {'data': [{'url': 'https://example.invalid/image.png'}]}
            call = LoggedCall({'id': 'test'}, '/v1/images/generations', 'gpt-image-2', '文生图')
            result = await call.run(handler)
            await JSONResponse(result)(scope, receive, send)
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            pass
        scope = {'type': 'http', 'path': '/v1/images/generations', 'headers': [(b'x-request-id', b'downstream-test-1')]}
        with patch('services.log_service.log_service', logger):
            asyncio.run(ImageDeliveryTraceMiddleware(application)(scope, receive, send))
        calls = [call.args for call in logger.add.call_args_list]
        generation = next(call for call in calls if call[0] == 'call')
        transport = next(call for call in calls if call[0] == 'delivery')
        self.assertIn('生成完成', generation[1])
        self.assertEqual('generation', generation[2]['result_scope'])
        self.assertEqual(transport[2]['call_id'], generation[2]['call_id'])
        self.assertEqual('downstream-test-1', generation[2]['client_request_id'])
        self.assertEqual(seen[0]['call_id'], generation[2]['call_id'])
        self.assertEqual('response_send_completed', transport[2]['send_state'])
        self.assertEqual({}, get_image_delivery_trace())
