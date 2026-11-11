import unittest
from unittest.mock import Mock
from services.openai_backend_api import OpenAIBackendAPI

TARGET='11111111-1111-4111-8111-111111111111'
PERSONAL='22222222-2222-4222-8222-222222222222'

def entry(account_id,plan):
    return {'account':{'account_id':account_id,'plan_type':plan}}

class AccountWorkspacePlanTests(unittest.TestCase):
    def backend(self,accounts,target=TARGET):
        backend=object.__new__(OpenAIBackendAPI)
        backend.base_url='https://example.test';backend.access_token=''
        backend.session=Mock();backend.session.headers={'ChatGPT-Account-Id':target} if target else {}
        backend.session.get.return_value.status_code=200
        backend.session.get.return_value.json.return_value={'accounts':accounts}
        backend._timezone_fields=Mock(return_value={'timezone_offset_min':0})
        return backend
    def test_current_workspace_wins_over_unrelated_default_personal_account(self):
        backend=self.backend({TARGET:entry(TARGET,'self_serve_business_prolite'),'default':entry(PERSONAL,'free')})
        self.assertEqual('self_serve_business_prolite',backend._get_default_account()['plan_type'])
        self.assertEqual(TARGET,backend.session.get.call_args.kwargs['headers']['ChatGPT-Account-Id'])
    def test_default_with_matching_identity_is_valid(self):
        backend=self.backend({'default':entry(TARGET,'edu_plus')})
        self.assertEqual('edu_plus',backend._get_default_account()['plan_type'])
    def test_duplicate_aliases_with_same_plan_are_one_workspace(self):
        backend=self.backend({TARGET:entry(TARGET,'edu_plus'),'default':entry(TARGET,'edu_plus')})
        self.assertEqual('edu_plus',backend._get_default_account()['plan_type'])
    def test_missing_requested_workspace_does_not_silently_downgrade(self):
        backend=self.backend({'default':entry(PERSONAL,'free')})
        with self.assertRaises(RuntimeError):backend._get_default_account()
    def test_conflicting_workspace_identity_is_rejected(self):
        backend=self.backend({TARGET:entry(PERSONAL,'self_serve_business_prolite'),'default':entry(PERSONAL,'free')})
        with self.assertRaises(RuntimeError):backend._get_default_account()
    def test_conflicting_alias_plans_are_rejected(self):
        backend=self.backend({TARGET:entry(TARGET,'self_serve_business_prolite'),'default':entry(TARGET,'free')})
        with self.assertRaises(RuntimeError):backend._get_default_account()
    def test_missing_plan_is_not_treated_as_free(self):
        backend=self.backend({TARGET:{'account':{'account_id':TARGET}},'default':entry(PERSONAL,'free')})
        with self.assertRaises(RuntimeError):backend._get_default_account()
    def test_malformed_alias_plan_is_rejected(self):
        backend=self.backend({TARGET:entry(TARGET,'self_serve_business_prolite'),'default':entry(TARGET,[])})
        with self.assertRaises(RuntimeError):backend._get_default_account()
    def test_without_workspace_hint_only_explicit_default_is_used(self):
        backend=self.backend({TARGET:entry(TARGET,'self_serve_business_prolite'),'default':entry(PERSONAL,'free')},target='')
        self.assertEqual('free',backend._get_default_account()['plan_type'])
    def test_get_user_info_rejects_missing_plan_instead_of_overwriting_type(self):
        backend=self.backend({})
        backend.access_token='fixture'
        backend._get_me=Mock(return_value={'id':'fixture-user'})
        backend._get_conversation_init=Mock(return_value={'limits_progress':[]})
        backend._get_default_account=Mock(return_value={})
        with self.assertRaises(RuntimeError):backend.get_user_info()

if __name__=='__main__':unittest.main()
