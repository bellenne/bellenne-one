import unittest
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from tests.test_storefront import StorefrontJobsTests
from app.models import IngestToken, Snapshot, Job, Product, ActiveJob
from app.db import migrate


class ExternalApiTests(unittest.TestCase):
    setUp = StorefrontJobsTests.setUp
    tearDown = StorefrontJobsTests.tearDown

    def test_ui_json_import_without_token_and_shared_dedup(self):
        import json
        body = self.payload()
        raw = ('\ufeff' + json.dumps(body)).encode('utf-8')
        def upload(content=raw, csrf='csrf', headers=None):
            return self.client.post('/prices/import', headers=headers or self.headers,
                data={'csrf_token':csrf}, files={'file':('prices.json',content,'application/json')}, follow_redirects=False)
        self.assertEqual(upload(csrf='wrong').status_code,403)
        self.assertEqual(upload(b'not json').status_code,422)
        self.assertEqual(upload(b'x'*65537).status_code,413)
        result = upload()
        self.assertEqual(result.status_code,303,result.text)
        self.assertEqual(upload().headers['location'],result.headers['location'])
        auth = self.configure()
        self.assertTrue(self.client.post('/api/v1/prices',headers=auth,json=body).json()['duplicate'])
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),2)
        self.assertIn('Импортировать JSON',self.client.get('/integrations',headers=self.headers).text)

    def configure(self, token='a'*48):
        response = self.client.post('/api-token', headers=self.headers,
            data={'csrf_token':'csrf', 'token':token}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        return {'Authorization':'Bearer '+token, 'Accept':'application/json'}

    def payload(self):
        return {'version':1, 'event_id':str(uuid4()), 'prices':[
            {'marketplace':mp,'sku':sku,'observed_at':datetime.now(timezone.utc).isoformat(),
             'loyalty_price':price,'discount_price':'4000.00','base_price':None}
            for mp,sku,price in [('wb','123','2917.00'),('ozon','789','3043.00')]]}

    def test_auth_catalog_pagination_isolation_rotation(self):
        self.assertEqual(self.client.get('/api/v1/products', headers=self.headers).status_code,401)
        auth = self.configure()
        first = self.client.get('/api/v1/products?limit=1',headers=auth).json()
        self.assertEqual(len(first['items']),1)
        self.assertEqual(first['items'][0]['offer_id'],'TEST')
        second = self.client.get('/api/v1/products?after_id='+str(first['next_after_id']),headers=auth).json()
        self.assertEqual(len(second['items']),1)
        self.assertIsNone(second['next_after_id'])
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        page = self.client.get('/integrations',headers=self.headers)
        self.assertIn('Токен настроен',page.text)
        self.assertNotIn('a'*48,page.text)
        with self.factory() as session:
            self.assertNotEqual(session.get(IngestToken,1).digest,'a'*48)
        foreign = {**self.headers,'X-Bellenne-User-Id':'2'}
        self.client.get('/', headers=foreign)
        self.client.post('/api-token',headers=foreign,data={'csrf_token':'csrf','token':'b'*48})
        self.assertEqual(self.client.get('/api/v1/products',headers={'Authorization':'Bearer '+'b'*48}).json()['items'],[])
        self.configure('c'*48)
        self.assertEqual(self.client.get('/api/v1/products',headers=auth).status_code,401)
        self.client.post('/api-token',headers=self.headers,data={'csrf_token':'csrf','action':'revoke'})
        self.assertEqual(self.client.get('/api/v1/products',headers={'Authorization':'Bearer '+'c'*48}).status_code,401)

    def test_atomic_idempotent_prices_restart_and_conflict(self):
        auth, body = self.configure(), self.payload()
        result = self.client.post('/api/v1/prices',headers=auth,json=body)
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()['accepted'])
        migrate(self.app.state.factory.kw['bind'])
        repeat = self.client.post('/api/v1/prices',headers=auth,json=body)
        self.assertTrue(repeat.json()['duplicate'])
        self.assertEqual(repeat.json()['job_id'],result.json()['job_id'])
        body['prices'][0]['loyalty_price']='3000'
        self.assertEqual(self.client.post('/api/v1/prices',headers=auth,json=body).status_code,409)
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),2)
            self.assertEqual(session.get(Job,result.json()['job_id']).status,'success')
        stale = self.payload()
        stale['prices'][0]['observed_at'] = body['prices'][0]['observed_at']
        self.assertEqual(self.client.post('/api/v1/prices',headers=auth,json=stale).status_code,409)
        unknown = self.payload()
        unknown['prices'][1]['sku']='999999'
        self.assertEqual(self.client.post('/api/v1/prices',headers=auth,json=unknown).status_code,404)
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),2)

    def test_validation_csrf_active_and_stale(self):
        self.assertEqual(self.client.post('/api-token',headers=self.headers,data={'csrf_token':'wrong','token':'a'*48}).status_code,403)
        auth = self.configure()
        for value in ('0','-1','NaN','1.001',3.14,'abc'):
            body=self.payload(); body['prices'][0]['loyalty_price']=value
            self.assertEqual(self.client.post('/api/v1/prices',headers=auth,json=body).status_code,422)
        body=self.payload(); body['prices'][0]['observed_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        self.assertEqual(self.client.post('/api/v1/prices',headers=auth,json=body).status_code,422)
        self.assertEqual(self.client.post('/api/v1/prices',headers=auth,content='x'*65537).status_code,413)
        from app.service import enqueue
        with self.factory() as session:
            enqueue(session,1); session.commit()
        self.assertEqual(self.client.post('/api/v1/prices',headers=auth,json=self.payload()).status_code,409)

    def test_parallel_repeat_creates_one_job(self):
        from concurrent.futures import ThreadPoolExecutor
        auth, body = self.configure(), self.payload()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.client.post('/api/v1/prices',headers=auth,json=body),range(2)))
        self.assertEqual([r.status_code for r in responses],[200,200])
        self.assertEqual(sorted(r.json()['duplicate'] for r in responses),[False,True])
        self.assertEqual(responses[0].json()['job_id'],responses[1].json()['job_id'])
