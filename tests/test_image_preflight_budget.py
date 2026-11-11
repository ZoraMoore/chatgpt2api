import time
import unittest
from unittest.mock import Mock, patch
from services.openai_backend_api import OpenAIBackendAPI
from test_sub2api_full_import import make_service

class ImagePreflightBudgetTests(unittest.TestCase):
    def backend(self):
        b=object.__new__(OpenAIBackendAPI)
        b.base_url='https://example.test';b.session=Mock();b.session.headers={};b.access_token='';b._headers=Mock(return_value={})
        b._timezone_fields=Mock(return_value={'timezone':'Asia/Shanghai','timezone_offset_min':-480})
        for method in [b.session.get,b.session.post]:
            method.return_value.status_code=200;method.return_value.json.return_value={}
        return b
    def test_profile_calls_share_remaining_deadline(self):
        b=self.backend();b._profile_deadline=102
        b.session.get.return_value.json.return_value={'accounts':{'default':{'account':{'plan_type':'edu_plus'}}}}
        with patch('services.openai_backend_api.time.monotonic',return_value=100):
            b._get_me();b._get_conversation_init();b._get_default_account()
        for call in [*b.session.get.call_args_list,*b.session.post.call_args_list]:
            self.assertEqual(2,call.kwargs['timeout'])
    def test_expired_preflight_does_not_start_another_network_request(self):
        b=self.backend();b._profile_deadline=100
        with patch('services.openai_backend_api.time.monotonic',return_value=101):
            with self.assertRaises(TimeoutError):b._get_me()
        b.session.get.assert_not_called()
    def test_healthy_account_cache_avoids_repeat_profile_queries(self):
        service,_=make_service([{'access_token':'cached-account','type':'self_serve_business_prolite','quota':3}])
        with patch('services.account_service.browser_sentinel_enabled',return_value=False):
            with patch.object(service,'fetch_remote_info',side_effect=lambda t,*a,**kw:service.get_account(t)) as fetch:
                first=service.get_available_access_token(generation_id='one')
                service.release_image_slot(first,generation_id='one')
                second=service.get_available_access_token(generation_id='two')
                service.release_image_slot(second,generation_id='two')
            self.assertEqual(1,fetch.call_count)

if __name__=='__main__':unittest.main()
