import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.accounts import create_router
from test_sub2api_full_import import make_service

class ImageAccountAPITests(unittest.TestCase):
    def setUp(self):
        self.service,_=make_service([{'access_token':'fixture-api','type':'edu_plus','quota':3}])
        self.enterContext(patch('api.accounts.account_service',self.service))
        app=FastAPI();app.include_router(create_router())
        self.client=TestClient(app)
        self.headers={'Authorization':'Bearer isolated-test-key'}
    def update(self,**values):
        return self.client.post('/api/accounts/update',headers=self.headers,json={'access_token':'fixture-api',**values})
    def test_manual_zero_and_auto_roundtrip(self):
        response=self.update(image_priority=0,image_priority_mode='manual')
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual((0,'manual'),(response.json()['item']['image_priority'],response.json()['item']['image_priority_mode']))
        response=self.update(image_priority_mode='auto')
        self.assertEqual(1,response.json()['item']['image_priority'])
    def test_invalid_priorities_are_rejected_without_mutation(self):
        for value in [True,-1,1.5,'0',1000001,None]:
            with self.subTest(value=value):
                response=self.update(image_priority=value,status='禁用')
                self.assertEqual(422,response.status_code,response.text)
                self.assertEqual('正常',self.service.get_account('fixture-api')['status'])
    def test_other_update_preserves_manual_priority(self):
        self.update(image_priority=0)
        response=self.update(proxy='')
        self.assertEqual((0,'manual'),(response.json()['item']['image_priority'],response.json()['item']['image_priority_mode']))
    def test_update_requires_admin(self):
        response=self.client.post('/api/accounts/update',json={'access_token':'fixture-api','image_priority':0})
        self.assertEqual(401,response.status_code)

if __name__=='__main__':unittest.main()
