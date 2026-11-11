"""辅助读取被限流时保留主生成流，账号冷却与禁止重放仍然有效。"""
import time
import unittest
from unittest.mock import patch

from services import image_quota
from services.protocol import conversation
from utils.helper import UpstreamHTTPError
from tests import test_image_generation_stream as transport
from tests import test_delivery_timeout_recovery as delivery
from tests import test_image_final_contract as images
from test_readback_throttle_fix import account_service, TOKEN


class StreamReadbackThrottleSurvivalTests(unittest.TestCase):
    def setUp(self):
        self.accounts = account_service()
        self.addCleanup(self.accounts._image_ledger.close)
        self.enterContext(patch('services.account_service.account_service', self.accounts))
        self.h = transport.ImageGenerationStreamTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def backend(self, later_events, *, delay=.15):
        server = self.h.server([{'conversation_id': transport.CONVERSATION_ID}],
            later_events=later_events, delay=delay,
            statuses={'/backend-api/conversations/' + transport.CONVERSATION_ID: 429})
        backend = self.h.backend(server)
        backend.access_token = TOKEN
        return server, backend

    def test_auxiliary_429_keeps_live_stream_until_final_image(self):
        server, backend = self.backend(transport.final_events())
        try:
            values = list(self.h.stream(backend, timeout=.7))
        except image_quota.ImageQuotaExceeded:
            self.fail('辅助GET被限流不能中断仍能交付最终图片的主生成流')
        self.h.assert_delivered(values)
        self.assertEqual(1, len(server.get_paths), '本次请求收到429后停止额外查询')
        self.h.assert_single_submission(server)
        self.assertEqual('cooldown', self.accounts.get_account(TOKEN)['image_limit']['state'])
        self.assertNotIn(TOKEN, self.accounts._list_ready_candidate_tokens())

    def test_auxiliary_429_still_bounds_silent_main_stream(self):
        server, backend = self.backend([])
        started = time.monotonic()
        with self.assertRaises(image_quota.ImageQuotaExceeded):
            list(self.h.stream(backend, timeout=.22))
        elapsed = time.monotonic()-started
        self.assertGreaterEqual(elapsed, .18, '必须先给原主流完整等待预算')
        self.assertLess(elapsed, .7)
        self.assertEqual(1, len(server.get_paths))
        self.assertEqual(1, server.post_count)

    def test_primary_stream_progress_does_not_restart_limited_reads(self):
        frames = [{'conversation_id':transport.CONVERSATION_ID,'message':{
            'id':'progress','author':{'role':'assistant'},'status':'in_progress',
            'content':{'content_type':'text','parts':['synthetic progress']}}}]
        server, backend = self.backend(frames, delay=.12)
        with self.assertRaises(image_quota.ImageQuotaExceeded):
            list(self.h.stream(backend, timeout=.3))
        self.assertEqual(1, len(server.get_paths))
        self.assertEqual(1, server.post_count)

    def test_actual_quota_failure_is_not_treated_as_auxiliary_throttle(self):
        server, backend = self.backend(transport.final_events())
        error = image_quota.ImageQuotaExceeded({'source':'assistant_final','reason':'synthetic quota',
            'conversation_id':transport.CONVERSATION_ID,'restore_at':time.time()+3600})
        async def quota(*args, **kwargs):raise error
        with patch.object(self.h.module,'_read_completed_turn',side_effect=quota):
            with self.assertRaises(image_quota.ImageQuotaExceeded) as caught:
                list(self.h.stream(backend, timeout=.7))
        self.assertIs(error,caught.exception)
        self.assertEqual(1,server.post_count)


class DeliveryAfterReadbackThrottleTests(unittest.TestCase):
    def setUp(self):
        self.h = delivery.DeliveryTimeoutRecoveryTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def throttle(self):
        return image_quota.ImageQuotaExceeded({'source':'upstream_http_conversation_throttle',
            'reason':'synthetic read throttle','conversation_id':images.CID,'restore_at':time.time()+30})

    def test_final_from_main_stream_is_delivered_while_account_cools(self):
        backend=self.h.backend(images.revisions(1))
        backend._image_readback_limit_error=self.throttle()
        result=self.h.run_result(backend)
        self.h.fixture.assert_only_image(result,self.h.expected)
        backend._get_conversation.assert_not_called()
        self.h.assert_no_generation(backend)

    def test_missing_final_does_not_query_again_after_auxiliary_429(self):
        backend=self.h.backend(images.revisions(1)[:-1])
        backend._image_delivery_deadline=self.h.clock.now+8
        backend._image_readback_limit_error=self.throttle()
        with self.assertRaises(image_quota.ImageQuotaExceeded):self.h.run_result(backend)
        backend._get_conversation.assert_not_called()
        self.h.assert_no_generation(backend)

    def test_non429_read_failure_does_not_destroy_delivery_evidence(self):
        backend=self.h.backend(images.revisions(1)[:-1])
        backend._image_delivery_deadline=self.h.clock.now+8
        backend._get_conversation.side_effect=[UpstreamHTTPError('/backend-api/conversations/'+images.CID,503,{}),
            {'conversation_id':images.CID,'messages':images.revisions(1),'async_status':4}]
        result=self.h.run_result(backend)
        self.h.fixture.assert_only_image(result,self.h.expected)
        self.assertEqual(2,backend._get_conversation.call_count)
        self.h.assert_no_generation(backend)


class ReadbackThrottleBusinessFlowTests(unittest.TestCase):
    def test_429_keeps_final_delivery_and_settles_once_without_clearing_cooldown(self):
        import base64
        import io
        from unittest.mock import Mock
        from PIL import Image
        from services.image_submission import guard_image_submission
        service=account_service()
        self.addCleanup(service._image_ledger.close)
        self.enterContext(patch('services.account_service.account_service',service))
        self.enterContext(patch.object(conversation,'account_service',service))
        service.get_available_access_token=Mock(side_effect=lambda **kwargs:service._acquire_next_candidate_token(
            excluded_tokens={'healthy-fixture'},generation_id=kwargs['generation_id']))
        h=transport.ImageGenerationStreamTests();h.setUp();self.addCleanup(h.doCleanups)
        server=h.server([{'conversation_id':transport.CONVERSATION_ID}],later_events=transport.final_events(),
            delay=.15,statuses={'/backend-api/conversations/'+transport.CONVERSATION_ID:429})
        backend=h.backend(server);backend.access_token=TOKEN;backend._voice_image_session=None
        out=io.BytesIO();Image.new('RGB',(8,6),'blue').save(out,format='PNG');png=out.getvalue()
        backend.download_final_sandbox_images=Mock(return_value=[png]);backend.close=Mock()
        def stream(**kwargs):
            guard_image_submission(backend)
            yield from h.stream(backend,timeout=.7)
        backend.stream_conversation=stream
        with patch.object(conversation,'OpenAIBackendAPI',return_value=backend):
            results=conversation._generate_single_image_impl(conversation.ConversationRequest(
                model='gpt-image-2.6-sunburst',prompt='synthetic',image_transport='conversation',response_format='b64_json'),1,1)
        result=next(x for x in results if x.kind=='result')
        self.assertEqual(png,base64.b64decode(result.data[0]['b64_json']))
        self.assertEqual('cooldown',service.get_account(TOKEN)['image_limit']['state'])
        self.assertEqual(1,service.get_account(TOKEN)['success'])
        self.assertEqual(0,service.get_account(TOKEN)['image_quota_uncertain'])
        self.assertEqual(1,server.post_count)
        self.assertEqual(1,len(server.get_paths))
        self.assertNotIn(TOKEN,service._list_ready_candidate_tokens())
