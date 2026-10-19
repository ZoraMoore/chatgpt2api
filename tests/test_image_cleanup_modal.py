import os
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('CHATGPT2API_AUTH_KEY','test-admin-key')
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.image_space import create_router
from services.image_space_service import ImageSpaceService, account_ref


class CleanupSelectionTests(unittest.TestCase):
    def service(self):
        records=[{'access_token':f'fixture-{i}', 'email':f'fixture{i}@example.test', 'source_type':'web'} for i in range(25)]
        records.append({'access_token':'excluded-adobe','source_type':'adobe'})
        accounts=Mock();accounts.list_accounts.return_value=records
        accounts.get_account.side_effect=lambda token: next((a for a in records if a['access_token']==token),None)
        client=Mock();client.scan.return_value={'image':{'library_file_id':'image','file_id':'file-test','file_name':'test.png','parent_directory_id':''}}
        return ImageSpaceService(accounts,Mock(return_value=client)), records, client

    def wait(self,service):
        deadline=time.monotonic()+3
        while service.active and time.monotonic()<deadline:time.sleep(.001)
        self.assertFalse(service.active)

    def test_all_accounts_preview_and_execution_keep_original_snapshot(self):
        service,records,client=self.service()
        job_id=service.preview([],all_accounts=True);self.wait(service)
        self.assertEqual(25,len(service.get(job_id)['accounts']))
        self.assertEqual('ready',service.get(job_id)['status'])
        client.post.assert_not_called()
        records.append({'access_token':'added-after-preview','email':'new@example.test','source_type':'web'})
        with patch('services.image_space_service.clear_snapshot') as clear:
            service.execute(job_id);self.wait(service)
        self.assertEqual(25,clear.call_count)
        self.assertNotIn('added-after-preview',str(service.accounts.get_account.call_args_list))

    def test_selection_larger_than_twenty_is_accepted(self):
        service,records,_=self.service()
        job_id=service.preview([account_ref(a) for a in records[:25]]);self.wait(service)
        self.assertEqual(25,len(service.get(job_id)['accounts']))


class CleanupModalApiTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.include_router(create_router());self.client=TestClient(app)
        self.headers={'Authorization':'Bearer test-admin-key'}

    def test_selection_and_all_are_mutually_exclusive_and_admin_only(self):
        for body in ({}, {'account_ids':[]}, {'all_accounts':True,'account_ids':['a'*64]}, {'all_accounts':'true'}, {'account_ids':['a'*64]*1001}):
            with self.subTest(body_keys=list(body)):
                self.assertEqual(422,self.client.post('/api/accounts/image-space/preview',json=body,headers=self.headers).status_code)
        self.assertEqual(401,self.client.post('/api/accounts/image-space/preview',json={'all_accounts':True}).status_code)
        with patch('api.image_space.image_space_service.preview',return_value='b'*32) as preview:
            result=self.client.post('/api/accounts/image-space/preview',json={'all_accounts':True},headers=self.headers)
            self.assertEqual(202,result.status_code)
            preview.assert_called_once_with([],all_accounts=True)
        with patch('api.image_space.image_space_service.preview',return_value='b'*32):
            result=self.client.post('/api/accounts/image-space/preview',json={'account_ids':[f'{i:064x}' for i in range(25)]},headers=self.headers)
            self.assertEqual(202,result.status_code)

    def test_legacy_page_redirects_to_account_modal(self):
        response=self.client.get('/accounts/image-space?accounts='+'a'*64,follow_redirects=False)
        self.assertEqual(307,response.status_code)
        self.assertEqual('/accounts?image_space_accounts='+'a'*64,response.headers['location'])
        response=self.client.get('/accounts/image-space?accounts=invalid',follow_redirects=False)
        self.assertEqual('/accounts',response.headers['location'])
