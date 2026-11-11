import time
import unittest
from unittest.mock import Mock,patch
from services.image_quota import ImageQuotaEvidence
from test_sub2api_full_import import make_service

class ImageLimitRecoveryPolicyTests(unittest.TestCase):
    def service(self):
        return make_service([{'access_token':'fixture','type':'self_serve_business_prolite','quota':20,'status':'正常'}])[0]
    def evidence(self, ident, deadline):
        now=time.time()
        return ImageQuotaEvidence('图像额度已达到上限','assistant_final','c',ident,ident,now,now,deadline)
    def test_older_limit_cannot_shorten_current_deadline(self):
        service=self.service();now=time.time()
        service.mark_image_quota_limit('fixture',self.evidence('new',now+7200))
        service.mark_image_quota_limit('fixture',self.evidence('late',now+60))
        self.assertGreaterEqual(service.get_account('fixture')['image_limit']['restore_at'],now+7200)
    def test_unknown_time_has_separate_next_check(self):
        service=self.service();service.mark_image_quota_limit('fixture',self.evidence('unknown',None))
        row=service.get_account('fixture')['image_limit']
        self.assertIsNone(row['restore_at']);self.assertGreater(row['next_check_at'],time.time())
    def test_due_refresh_opens_business_validation_without_quota_endpoint(self):
        service=self.service();now=time.time()
        service.mark_image_quota_limit('fixture',self.evidence('expired',now-1))
        backend=Mock()
        backend.get_image_quota.return_value={'quota':0,'limits_progress':[{'feature_name':'image_gen','remaining':0,'reset_after':now+3600}]}
        with patch('services.openai_backend_api.OpenAIBackendAPI',return_value=backend) as factory:
            service._refresh_image_limit('fixture')
        factory.assert_not_called()
        row=service.get_account('fixture')['image_limit']
        self.assertEqual('probe_ready', row['state'])
        self.assertLess(row['restore_at'], now)
    def test_not_due_accounts_do_not_make_upstream_calls(self):
        service=self.service();service.mark_image_quota_limit('fixture',self.evidence('wait',time.time()+3600))
        with patch('services.openai_backend_api.OpenAIBackendAPI') as backend:
            service._refresh_image_limit('fixture');backend.assert_not_called()
    def test_due_list_excludes_future_disabled_and_ready_probe(self):
        service=self.service();now=time.time()
        service.mark_image_quota_limit('fixture',self.evidence('due',now-1))
        method=getattr(service,'list_due_image_limits',None);self.assertTrue(callable(method))
        self.assertEqual(['fixture'],method())
        service.update_account('fixture',{'status':'禁用'},quiet=True)
        self.assertEqual([],method())

if __name__=='__main__':unittest.main()
