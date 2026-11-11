import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch
from services.account_service import config
from services.image_quota import ImageQuotaEvidence, ImageQuotaExceeded
from services.image_submission import guard_image_submission
from services.protocol import conversation
from test_sub2api_full_import import make_service
from image_quota_fixtures import seed_known_local_balance

class ImagePolicyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.service,_=make_service([
            {'access_token':'fixture-edu','type':'edu_plus','quota':3},
            {'access_token':'fixture-prolite','type':'self_serve_business_prolite','quota':1}])
        seed_known_local_balance(self.service, 'fixture-prolite', 1)
        for row in self.service._accounts.values():self.service._image_refresh_times[row['management_id']]=time.monotonic()
        self.enterContext(patch('services.account_service.browser_sentinel_enabled',return_value=False))
        self.enterContext(patch.object(type(config),'image_account_concurrency',new_callable=PropertyMock,return_value=3))
        self.enterContext(patch.object(conversation,'account_service',self.service))
        self.backends=[]
        def backend(access_token):
            obj=SimpleNamespace(access_token=access_token,close=Mock(),image_conversation_id='')
            self.backends.append(obj);return obj
        self.enterContext(patch.object(conversation,'OpenAIBackendAPI',side_effect=backend))
        self.request=conversation.ConversationRequest(model='gpt-image-2.6-sunburst',prompt='fixture',image_transport='conversation')
    def result(self,backend):
        guard_image_submission(backend)
        backend.image_conversation_id='fixture-conversation'
        yield conversation.ImageOutput(kind='result',model=self.request.model,index=1,total=1,data=[{'url':'https://example.test/result.png'}],conversation_id='fixture-conversation')
    def test_last_reserved_unit_reaches_actual_submission_and_settles(self):
        with patch.object(conversation,'stream_image_outputs',side_effect=lambda b,*_:self.result(b)):
            result=conversation._generate_single_image_impl(self.request,1,1)
        self.assertEqual(1,len(result));self.assertEqual('fixture-prolite',self.backends[0].access_token)
        self.assertEqual((0,1),(self.service.get_account('fixture-prolite')['quota'],self.service.get_account('fixture-prolite')['success']))
    def test_limit_after_selection_switches_before_submission_only(self):
        generated=[]
        def stream(backend,*_):
            if backend.access_token=='fixture-prolite':
                now=time.time();self.service.mark_image_quota_limit(backend.access_token,ImageQuotaEvidence('额度限制','fixture','','','event',now,now,now+3600))
            guard_image_submission(backend)
            generated.append(backend.access_token)
            yield from self.result(backend)
        with patch.object(conversation,'stream_image_outputs',side_effect=stream):
            conversation._generate_single_image_impl(self.request,1,1)
        self.assertEqual(['fixture-edu'],generated)
        self.assertEqual(0,self.service.get_account('fixture-prolite')['image_quota_uncertain'])
    def test_unknown_failure_after_submit_keeps_original_account_and_budget(self):
        def stream(backend,*_):
            guard_image_submission(backend);backend.image_conversation_id='original-conversation'
            raise RuntimeError('network disconnected')
        with patch.object(conversation,'stream_image_outputs',side_effect=stream):
            with self.assertRaises(conversation.ImageGenerationError):conversation._generate_single_image_impl(self.request,1,1)
        self.assertEqual(1,len(self.backends))
        self.assertEqual(1,self.service.get_account('fixture-prolite')['image_quota_uncertain'])
        self.assertTrue(self.service.complete_image_generation_from_resume('fixture-prolite','original-conversation'))
        self.assertFalse(self.service.complete_image_generation_from_resume('fixture-prolite','original-conversation'))
        self.assertEqual((0,1),(self.service.get_account('fixture-prolite')['quota'],self.service.get_account('fixture-prolite')['success']))
    def test_remote_precheck_of_last_unit_does_not_discard_own_reservation(self):
        self.service._image_refresh_times.clear()
        with patch.object(self.service,'fetch_remote_info',side_effect=lambda t,*a,**kw:self.service.update_account(t,{'quota':1},quiet=True,remote=True)):
            with patch.object(conversation,'stream_image_outputs',side_effect=lambda b,*_:self.result(b)):
                conversation._generate_single_image_impl(self.request,1,1)
        self.assertEqual('fixture-prolite',self.backends[0].access_token)

if __name__=='__main__':unittest.main()
