"""异常结算必须结束验证所有权；慢预检必须给备用账号留出选择时间。"""
import time
import unittest
from unittest.mock import Mock,PropertyMock,patch
from services.account_service import config
from services.image_quota import ImageQuotaEvidence,ImageQuotaExceeded,CHECK_RETRY
from test_sub2api_full_import import make_service

class FakeClock:
    def __init__(self):self.now=100.0
    def monotonic(self):return self.now

class AccountRecoveryEdgeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('services.account_service.browser_sentinel_enabled',return_value=False))
        self.enterContext(patch.object(type(config),'image_account_concurrency',new_callable=PropertyMock,return_value=3))
        self.enterContext(patch.object(type(config),'auto_remove_rate_limited_accounts',new_callable=PropertyMock,return_value=False))

    def service(self,tokens):
        service,storage=make_service([{'access_token':t,'type':'edu_plus','quota':10,'status':'正常'} for t in tokens])
        self.addCleanup(service._image_ledger.close)
        service.list_accounts()
        return service

    def limit(self,service,token='probe',event='event-one',future=False):
        now=time.time()
        service.mark_image_quota_limit(token,ImageQuotaEvidence('图像额度已达到上限','assistant_final','fixture-cid',event,event,now-10,now,now+3600 if future else now-1))

    def test_abnormal_or_disabled_probe_settlement_can_be_revalidated_after_restoration(self):
        for status in ('异常','禁用'):
            for success in (False,True):
                with self.subTest(status=status,success=success):
                    service=self.service(['probe']);self.limit(service)
                    service._refresh_image_limit('probe')
                    service._acquire_next_candidate_token(generation_id='probe-generation',wait_timeout_secs=0)
                    service.assert_image_submission_allowed('probe','probe-generation')
                    service.update_account('probe',{'status':status},quiet=True)
                    service.mark_image_result('probe',success,generation_id='probe-generation',not_consumed=not success)
                    row=service.get_account('probe')
                    self.assertEqual(status,row['status'])
                    self.assertEqual('awaiting_check',row['image_limit']['state'])
                    self.assertEqual({},service._image_probe_leases)
                    self.assertEqual(0,service._image_inflight.get('probe',0))
                    self.assertEqual([],service._list_ready_candidate_tokens())
                    ready_at=row['image_limit']['next_check_at']+1
                    # 人工恢复资格不应跳过冷却，但到期必须允许再次业务验证。
                    service.update_account('probe',{'status':'正常'},quiet=True)
                    self.assertEqual([],service._list_ready_candidate_tokens())
                    with patch('services.account_service.time.time',return_value=ready_at):
                        service._refresh_image_limit('probe')
                    self.assertEqual(['probe'],service._list_ready_candidate_tokens())

    def test_old_probe_settlement_does_not_override_newer_cooldown_while_abnormal(self):
        service=self.service(['probe']);self.limit(service)
        service._refresh_image_limit('probe');service._acquire_next_candidate_token(generation_id='old',wait_timeout_secs=0)
        self.limit(service,event='new-event',future=True)
        service.update_account('probe',{'status':'异常'},quiet=True)
        before=service.get_account('probe')['image_limit'].copy()
        self.assertEqual('cooldown',before['state'])
        service.mark_image_result('probe',False,generation_id='old',not_consumed=True)
        self.assertEqual(before,service.get_account('probe')['image_limit'])
        self.assertEqual('异常',service.get_account('probe')['status'])

    def test_orphaned_probe_restoration_is_repaired_without_opening_an_active_probe(self):
        service=self.service(['probe']);self.limit(service);service._refresh_image_limit('probe')
        service._accounts['probe']['image_limit']['state']='probing'
        service._accounts['probe']['status']='异常'
        service.update_account('probe',{'status':'正常'},quiet=True,remote=True)
        row=service.get_account('probe')
        self.assertEqual('awaiting_check',row['image_limit']['state'])
        self.assertGreaterEqual(row['image_limit']['next_check_at'],time.time()+CHECK_RETRY-1)
        self.assertEqual([],service._list_ready_candidate_tokens())

    def test_profile_update_never_steals_a_live_probe_owner(self):
        service=self.service(['probe']);self.limit(service);service._refresh_image_limit('probe')
        service._acquire_next_candidate_token(generation_id='owned',wait_timeout_secs=0)
        owner=service._image_probe_leases['probe']
        service.update_account('probe',{'status':'正常'},quiet=True,remote=True)
        self.assertEqual('probing',service.get_account('probe')['image_limit']['state'])
        self.assertEqual(owner,service._image_probe_leases['probe'])
        service.assert_image_submission_allowed('probe','owned')

    def test_readback_resume_can_read_after_cooldown_without_a_new_generation_probe(self):
        service=self.service(['readback']);now=time.time()
        service.mark_image_quota_limit('readback',ImageQuotaEvidence('原会话读取暂时限流',
            'upstream_http_conversation_throttle','another-cid','readback-mid','readback-event',
            now,now,now+30))
        ref=service.image_account_ref('readback')
        with self.assertRaises(ImageQuotaExceeded):
            service.acquire_image_resume_token(ref,wait_timeout_secs=0,conversation_id='original-cid')
        with patch('services.account_service.time.time',return_value=now+31):
            selected=service.acquire_image_resume_token(ref,wait_timeout_secs=0,conversation_id='original-cid')
        self.assertEqual('readback',selected)
        self.assertEqual('cooldown',service.get_account('readback')['image_limit']['state'])
        self.assertEqual([],service._list_ready_candidate_tokens())
        self.assertEqual(0,service._image_ledger._db.execute('SELECT COUNT(*) FROM image_leases').fetchone()[0])
        service.release_image_slot(selected,resume=True)
        self.assertEqual({},service._image_inflight)

    def test_resume_does_not_read_early_or_ignore_actual_quota_refusal_or_disabled_status(self):
        service=self.service(['probe']);self.limit(service,future=True)
        ref=service.image_account_ref('probe')
        with self.assertRaises(ImageQuotaExceeded):
            service.acquire_image_resume_token(ref,wait_timeout_secs=0,conversation_id='fixture-cid')
        service.update_account('probe',{'status':'禁用'},quiet=True)
        with self.assertRaises(ValueError):
            service.acquire_image_resume_token(ref,wait_timeout_secs=0,conversation_id='fixture-cid')

    def test_slow_preflight_releases_reservation_and_tries_healthy_backup(self):
        service=self.service(['slow','healthy','backup']);clock=FakeClock();attempts=[];budgets=[]
        def fetch(token,*args,request_deadline=None,**kw):
            attempts.append(token);budgets.append(request_deadline-clock.now)
            if token=='slow':
                clock.now=request_deadline
                raise TimeoutError('fixture slow preflight')
            return service.get_account(token)
        with patch('services.account_service.time.monotonic',clock.monotonic),patch.object(service,'fetch_remote_info',side_effect=fetch):
            selected=service.get_available_access_token(generation_id='generation')
        self.assertEqual('healthy',selected)
        self.assertEqual(['slow','healthy'],attempts)
        self.assertLess(budgets[0],15)
        self.assertLessEqual(clock.now,115)
        for token in ['slow','healthy','backup']:
            row=service.get_account(token)
            self.assertEqual(int(token=='healthy'),row['image_quota_reserved'])
            self.assertEqual(int(token=='healthy'),service._image_inflight.get(token,0))
            self.assertEqual(0,row['image_quota_uncertain'])

    def test_single_account_can_use_full_remaining_preflight_budget(self):
        service=self.service(['only']);clock=FakeClock();budgets=[]
        def fetch(token,*args,request_deadline=None,**kw):
            budgets.append(request_deadline-clock.now);clock.now+=8
            self.assertLess(clock.now,request_deadline)
            return service.get_account(token)
        with patch('services.account_service.time.monotonic',clock.monotonic),patch.object(service,'fetch_remote_info',side_effect=fetch):
            self.assertEqual('only',service.get_available_access_token(generation_id='generation'))
        self.assertEqual([15],budgets)

    def test_slow_refresh_lock_cannot_block_all_backup_attempts(self):
        service=self.service(['slow','healthy','backup']);clock=FakeClock();lock=Mock()
        def acquire(timeout):clock.now+=timeout;return False
        lock.acquire.side_effect=acquire
        service._image_refresh_locks[service._accounts['slow']['management_id']]=lock
        with patch('services.account_service.time.monotonic',clock.monotonic),patch.object(service,'fetch_remote_info',side_effect=lambda t,*a,**kw:service.get_account(t)):
            selected=service.get_available_access_token(generation_id='generation')
        self.assertEqual('healthy',selected)
        self.assertLess(lock.acquire.call_args.kwargs['timeout'],15)
        lock.release.assert_not_called()

    def test_three_failed_preflights_stay_within_total_deadline_and_release_all_slots(self):
        service=self.service(['a','b','c']);clock=FakeClock();attempts=[]
        def fetch(token,*args,request_deadline=None,**kw):
            attempts.append(token);clock.now=request_deadline;raise TimeoutError('fixture timeout')
        with patch('services.account_service.time.monotonic',clock.monotonic),patch.object(service,'fetch_remote_info',side_effect=fetch):
            with self.assertRaises(RuntimeError):service.get_available_access_token(generation_id='generation')
        self.assertEqual(['a','b','c'],attempts)
        self.assertLessEqual(clock.now,115)
        self.assertEqual({},service._image_inflight)
        for row in service.list_accounts():self.assertEqual(0,row['image_quota_reserved'])

if __name__=='__main__':unittest.main()
