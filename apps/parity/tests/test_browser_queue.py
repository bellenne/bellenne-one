from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import unittest
from sqlalchemy import func, select
import tests.test_storefront as fixtures
from app.models import ActiveJob, BrowserRun, BrowserTask, CollectionAttempt, Comparison, Job, Policy, Product, Snapshot
from app.service import claim


class BrowserQueueTests(unittest.TestCase):
    setUp = fixtures.StorefrontJobsTests.setUp
    tearDown = fixtures.StorefrontJobsTests.tearDown

    def start(self, **extra):
        self.client_id = str(uuid4())
        response = self.client.post('/jobs', headers={**self.headers,'Accept':'application/json'},
                                    data={'csrf_token':'csrf','collector':'browser',**extra})
        self.assertEqual(response.status_code,202,response.text)
        return response.json()['job_id']

    def next(self, job_id, **extra):
        return self.client.post(f'/api/browser/{job_id}/claim',headers=self.headers,
                                json={'csrf_token':'csrf','client_id':self.client_id,**extra})

    def receipt(self, job_id, task, **extra):
        return self.client.post(f'/api/browser/{job_id}/receipt',headers=self.headers,json={
            'csrf_token':'csrf','client_id':self.client_id,'task_id':task['id'],'token':task['token'],
            'observed_at':datetime.now(timezone.utc).isoformat(),
            'observation':{'identity':task['identifier'],'city':'Москва','hasBox':True,'benefitConfirmed':True,
                           'benefit':'2917 ₽' if task['marketplace']=='wb' else '3043 ₽','regular':'4000 ₽','reference':'10000 ₽'},**extra})

    def control(self, job_id, action):
        return self.client.post(f'/browser/{job_id}/control',headers=self.headers,data={'csrf_token':'csrf','action':action})

    def test_whole_queue_and_idempotent_exact_prices(self):
        job = self.start()
        first = self.next(job).json()['task']
        response = self.receipt(job,first)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['accepted'])
        self.assertTrue(self.receipt(job,first).json()['duplicate'])
        second = self.next(job).json()['task']
        self.assertNotEqual(first['id'],second['id'])
        self.assertTrue(self.receipt(job,second).json()['accepted'])
        with self.factory() as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot)),2)
            self.assertEqual(s.get(Job,job).status,'success')
            self.assertIsNone(s.get(ActiveJob,1))
            self.assertEqual(s.scalar(select(Comparison).where(Comparison.price_type=='loyalty_price')).difference_absolute,Decimal('126'))
            self.assertEqual(s.scalar(select(Snapshot)).context['session'],'browser_profile')
        self.assertEqual(self.next(job).json()['state'],'finished')

    def test_popup_controls_require_owner_csrf_and_bound_profile(self):
        job = self.start()
        self.next(job)
        path = f'/api/browser/{job}/control'
        body = {'csrf_token':'csrf','client_id':self.client_id,'action':'pause'}
        self.assertEqual(self.client.post(path,headers=self.headers,json={**body,'csrf_token':'wrong'}).status_code,403)
        self.assertEqual(self.client.post(path,headers=self.headers,json={**body,'client_id':str(uuid4())}).status_code,409)
        self.assertEqual(self.client.post(path,headers={**self.headers,'X-Bellenne-User-Id':'2'},json=body).status_code,404)
        value = self.client.post(path,headers=self.headers,json=body)
        self.assertEqual(value.status_code,200,value.text)
        self.assertEqual(value.json()['state'],'paused')
        self.assertEqual(self.client.post(path,headers=self.headers,json={**body,'action':'resume'}).json()['state'],'running')
        self.assertEqual(self.client.post(path,headers=self.headers,json={**body,'action':'stop'}).status_code,422)

    def test_finished_job_can_release_stale_cursor_from_another_profile(self):
        old = self.start()
        task = self.next(old).json()['task']
        self.receipt(old,task)
        self.control(old,'stop')
        new = self.start(marketplace='wb',kind='probe')
        terminal = self.next(old)
        self.assertEqual(terminal.status_code,200,terminal.text)
        self.assertEqual(terminal.json()['state'],'finished')
        self.assertIsNone(terminal.json()['task'])
        self.assertEqual(terminal.json()['saved'],1)
        current = self.next(new)
        self.assertEqual(current.status_code,200,current.text)
        self.assertIsNotNone(current.json()['task'])
        self.assertEqual(self.next(new,client_id=str(uuid4())).status_code,409)
        with self.factory() as session:
            self.assertEqual(session.get(BrowserRun,old).client_id != self.client_id,True)
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),1)

    def test_worker_cannot_claim_or_expire_browser_queue(self):
        job = self.start()
        self.assertIsNone(claim(self.factory,self.settings))
        self.next(job)
        with self.factory() as s:
            s.get(Job,job).heartbeat_at=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(days=1)
            s.commit()
        self.assertIsNone(claim(self.factory,self.settings))
        self.assertEqual(self.next(job).json()['state'],'running')

    def test_pause_and_resume_retry_same_card(self):
        job=self.start()
        task=self.next(job).json()['task']
        self.control(job,'pause')
        self.assertIsNone(self.next(job).json()['task'])
        self.assertEqual(self.receipt(job,task).status_code,409)
        self.control(job,'resume')
        self.assertEqual(self.next(job).json()['task'],task)
        self.assertTrue(self.receipt(job,task).json()['accepted'])

    def test_pause_before_connection_stays_paused(self):
        job=self.start()
        self.control(job,'pause')
        self.assertEqual(self.next(job).json()['state'],'paused')
        self.control(job,'resume')
        self.assertIsNotNone(self.next(job).json()['task'])

    def test_all_card_errors_skip_without_manual_action(self):
        for code in ['blocked','benefit_missing','layout','navigation','identity','region','setup','unavailable','internal']:
            job=self.start()
            task=self.next(job).json()['task']
            response=self.receipt(job,task,observation=None,error_code=code)
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue(response.json()['accepted'])
            value = self.next(job).json()
            self.assertEqual(value['state'],'running')
            self.assertEqual((value['done'], value['skipped']), (1, 1))
            self.assertNotEqual(value['task']['id'], task['id'])
            self.assertTrue(self.receipt(job,task,observation=None,error_code=code).json()['duplicate'])
            with self.factory() as s:
                self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.job_id==job)),0)
            terminal = self.receipt(job, value['task']).json()
            self.assertEqual((terminal['state'], terminal['saved'], terminal['skipped']), ('finished', 1, 1))

    def test_cancel_retains_observations_and_releases_owner(self):
        job=self.start()
        task=self.next(job).json()['task']
        self.receipt(job,task)
        self.control(job,'stop')
        with self.factory() as s:
            self.assertEqual(s.get(Job,job).status,'cancelled')
            self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot)),1)
            self.assertIsNone(s.get(ActiveJob,1))
            self.assertEqual(s.scalar(select(BrowserTask).where(BrowserTask.state=='skipped')).error_code,'cancelled')
        self.assertEqual(self.next(job).json()['state'],'finished')

    def test_manual_skip_while_paused_counts_progress(self):
        job=self.start()
        task=self.next(job).json()['task']
        self.control(job,'pause')
        self.control(job,'skip')
        value=self.next(job).json()
        self.assertEqual(value['done'],1)
        self.assertNotEqual(value['task']['id'],task['id'])
        self.receipt(job,value['task'])
        with self.factory() as s:
            self.assertEqual(s.get(Job,job).status,'partial')

    def test_identity_mismatch_skips_once_and_collects_next_card(self):
        for reported in [True, False]:
            with self.subTest(reported_by_extension=reported):
                job = self.start()
                task = self.next(job).json()['task']
                extra = {'observation': None, 'error_code': 'identity'} if reported else {
                    'observation': {'identity': '999', 'city': 'Москва', 'hasBox': True,
                                    'benefitConfirmed': True, 'benefit': '100 ₽'}}
                response = self.receipt(job, task, **extra)
                self.assertEqual(response.status_code, 200, response.text)
                value = response.json()
                self.assertTrue(value['accepted'])
                self.assertEqual((value['state'], value['done'], value['saved'], value['skipped']), ('running', 1, 0, 1))
                self.assertIsNone(value['attention_code'])
                self.assertTrue(self.receipt(job, task, **extra).json()['duplicate'])
                with self.factory() as session:
                    skipped = session.get(BrowserTask, task['id'])
                    self.assertEqual((skipped.state, skipped.error_code), ('skipped', 'identity'))
                    product = session.get(Product, skipped.product_id)
                    self.assertEqual((product.collection_status, product.collection_error), ('failed', 'identity'))
                    attempt = session.scalar(select(CollectionAttempt).where(CollectionAttempt.job_id == job))
                    self.assertEqual((attempt.status, attempt.error_code, attempt.details['automatic']), ('skipped', 'identity', True))
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.job_id == job)), 0)
                following = self.next(job).json()['task']
                self.assertNotEqual(following['id'], task['id'])
                self.assertTrue(self.receipt(job, following).json()['accepted'])
                terminal = self.next(job).json()
                self.assertEqual((terminal['state'], terminal['done'], terminal['saved'], terminal['skipped']), ('finished', 2, 1, 1))
                with self.factory() as session:
                    self.assertEqual(session.get(Job, job).status, 'partial')

    def test_invalid_catalog_identifier_is_skipped_before_opening(self):
        with self.factory() as session:
            session.scalar(select(Product).where(Product.marketplace == 'ozon')).sku = None
            session.commit()
        job = self.start()
        value = self.next(job).json()
        self.assertEqual((value['done'], value['skipped'], value['task']['marketplace']), (1, 1, 'wb'))
        self.assertTrue(self.receipt(job, value['task']).json()['accepted'])
        with self.factory() as session:
            self.assertEqual(session.get(Job, job).status, 'partial')
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 1)

    def test_all_invalid_catalog_identifiers_finish_and_release_queue(self):
        with self.factory() as session:
            for product in session.scalars(select(Product)):
                product.external_id, product.sku = 'invalid', None
            session.commit()
        job = self.start()
        value = self.next(job).json()
        self.assertEqual((value['state'], value['done'], value['saved'], value['skipped']), ('finished', 2, 0, 2))
        self.assertIsNone(value['task'])
        self.assertEqual(self.next(job).json()['skipped'], 2)
        with self.factory() as session:
            self.assertEqual(session.get(Job, job).status, 'failed')
            self.assertIsNone(session.get(ActiveJob, 1))
            self.assertEqual(session.scalar(select(func.count()).select_from(CollectionAttempt)), 2)
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 0)

    def test_existing_identity_stop_recovers_only_for_bound_client_and_respects_pause(self):
        job = self.start()
        task = self.next(job).json()['task']
        with self.factory() as session:
            run = session.get(BrowserRun, job)
            run.state, run.attention_code = 'attention', 'identity'
            session.get(Job, job).phase = 'attention'
            session.commit()
        self.assertEqual(self.next(job, client_id=str(uuid4())).status_code, 409)
        self.control(job, 'pause')
        self.assertEqual(self.next(job).json()['state'], 'paused')
        self.assertEqual(self.receipt(job, task, observation=None, error_code='identity').status_code, 409)
        with self.factory() as session:
            self.assertEqual(session.get(BrowserTask, task['id']).state, 'pending')
            run = session.get(BrowserRun, job)
            run.state = 'attention'
            session.commit()
        recovered = self.next(job).json()
        self.assertEqual((recovered['state'], recovered['done'], recovered['skipped']), ('running', 1, 1))
        self.assertNotEqual(recovered['task']['id'], task['id'])
        self.assertEqual(self.next(job).json()['skipped'], 1)
        self.assertTrue(self.receipt(job, task, observation=None, error_code='identity').json()['duplicate'])

    def test_concurrent_identity_receipts_skip_only_current_card(self):
        job = self.start()
        task = self.next(job).json()['task']
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.receipt(job, task, observation=None, error_code='identity'), range(2)))
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(sum(response.json().get('duplicate', False) for response in responses), 1)
        value = self.next(job).json()
        self.assertEqual((value['done'], value['skipped']), (1, 1))
        self.assertNotEqual(value['task']['id'], task['id'])
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(CollectionAttempt)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 0)

    def test_identity_skip_on_last_card_finishes_without_saving_wrong_price(self):
        job = self.start()
        self.receipt(job, self.next(job).json()['task'])
        last = self.next(job).json()['task']
        result = self.receipt(job, last, observation=None, error_code='identity').json()
        self.assertEqual((result['state'], result['done'], result['saved'], result['skipped']), ('finished', 2, 1, 1))
        self.assertTrue(self.receipt(job, last, observation=None, error_code='identity').json()['duplicate'])
        with self.factory() as session:
            self.assertEqual(session.get(Job, job).status, 'partial')
            self.assertIsNone(session.get(ActiveJob, 1))
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 1)

    def test_identity_skip_keeps_history_but_invalidates_current_comparison(self):
        first_job = self.start()
        self.receipt(first_job, self.next(first_job).json()['task'])
        self.receipt(first_job, self.next(first_job).json()['task'])
        with self.factory() as session:
            old_snapshot = session.scalar(select(Product).where(Product.marketplace == 'ozon')).current_snapshot_id
        job = self.start()
        self.receipt(job, self.next(job).json()['task'], observation=None, error_code='identity')
        self.receipt(job, self.next(job).json()['task'])
        with self.factory() as session:
            product = session.scalar(select(Product).where(Product.marketplace == 'ozon'))
            self.assertEqual(product.current_snapshot_id, old_snapshot)
            self.assertIsNotNone(session.get(Snapshot, old_snapshot))
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 3)
            self.assertEqual(session.scalar(select(Comparison).where(Comparison.price_type == 'loyalty_price')).status, 'stale')

    def test_visible_city_is_optional_and_is_not_replaced_with_moscow(self):
        for city in [None, 'Казань']:
            with self.subTest(city=city):
                job = self.start(marketplace='ozon', kind='probe')
                task = self.next(job).json()['task']
                result = self.receipt(job, task, observation={
                    'identity': task['identifier'], 'city': city, 'hasBox': True,
                    'benefitConfirmed': True, 'benefit': '3043 ₽'}).json()
                self.assertEqual((result['state'], result['saved'], result['skipped']), ('finished', 1, 0))
                with self.factory() as session:
                    snapshot = session.scalar(select(Snapshot).where(Snapshot.job_id == job))
                    self.assertEqual(snapshot.context['city'], city)
                    self.assertFalse(snapshot.context['region_checked'])

    def test_sold_out_observation_skips_without_price_or_snapshot(self):
        job = self.start()
        task = self.next(job).json()['task']
        result = self.receipt(job, task, observation={'unavailable': True}).json()
        self.assertEqual((result['state'], result['done'], result['saved'], result['skipped']), ('running', 1, 0, 1))
        following = self.next(job).json()['task']
        self.assertNotEqual(task['id'], following['id'])
        self.receipt(job, following)
        with self.factory() as session:
            self.assertEqual(session.get(BrowserTask, task['id']).error_code, 'unavailable')
            self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 1)

    def test_legacy_setup_and_region_errors_and_unavailable_cards_do_not_stop_queue(self):
        for code in ['setup', 'region', 'unavailable']:
            with self.subTest(code=code):
                job = self.start()
                task = self.next(job).json()['task']
                result = self.receipt(job, task, observation=None, error_code=code).json()
                self.assertEqual((result['accepted'], result['state'], result['done'], result['skipped']), (True, 'running', 1, 1))
                self.assertEqual(result['message'], '')
                following = self.next(job).json()['task']
                self.assertNotEqual(task['id'], following['id'])
                self.receipt(job, following)
                with self.factory() as session:
                    self.assertEqual(session.get(BrowserTask, task['id']).error_code, 'unavailable')
                    self.assertEqual(session.get(Job, job).status, 'partial')

    def test_any_existing_error_stop_recovers_without_resume(self):
        for code in ['setup', 'region', 'layout', 'blocked', 'benefit_missing', 'navigation', 'internal', 'unexpected', None]:
            with self.subTest(code=code):
                job = self.start()
                task = self.next(job).json()['task']
                with self.factory() as session:
                    run = session.get(BrowserRun, job)
                    run.state, run.attention_code = 'attention', code
                    session.get(Job, job).phase = 'attention'
                    session.commit()
                self.assertEqual(self.next(job, client_id=str(uuid4())).status_code, 409)
                value = self.next(job).json()
                self.assertEqual((value['state'], value['done'], value['skipped'], value['message']), ('running', 1, 1, ''))
                self.assertNotEqual(value['task']['id'], task['id'])
                self.receipt(job, value['task'])

    def test_profile_change_separates_price_context(self):
        job=self.start()
        self.receipt(job,self.next(job).json()['task'])
        self.receipt(job,self.next(job).json()['task'])
        with self.factory() as s:
            previous=s.get(Policy,1).context_version
        job2=self.start(marketplace='wb',kind='probe')
        self.next(job2)
        with self.factory() as s:
            self.assertEqual(s.get(Policy,1).context_version,previous+1)
            self.assertEqual(s.scalar(select(Comparison).where(Comparison.price_type=='loyalty_price')).status,'stale')

    def test_context_and_token_and_owner_and_csrf_enforced(self):
        job=self.start()
        task=self.next(job).json()['task']
        self.assertEqual(self.next(job,client_id=str(uuid4())).status_code,409)
        self.assertEqual(self.next(job,csrf_token='bad').status_code,403)
        self.assertEqual(self.client.post(f'/api/browser/{job}/claim',headers={**self.headers,'X-Bellenne-User-Id':'2'},
            json={'csrf_token':'csrf','client_id':self.client_id}).status_code,404)
        self.assertEqual(self.receipt(job,task,token='x'*32).status_code,409)
        self.assertEqual(self.receipt(job,task,observed_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()).status_code,409)
        self.assertEqual(self.receipt(job,task,observation={'identity':'123','city':'Москва','hasBox':True,'benefitConfirmed':True,'benefit':'2917 ₽','cookies':'secret'}).status_code,422)
        self.assertEqual(self.client.get('/browser-extension').status_code,401)

    def test_concurrent_receipt_and_claim_use_one_snapshot(self):
        job=self.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks=list(pool.map(lambda _:self.next(job).json()['task'],range(2)))
        self.assertEqual(tasks[0],tasks[1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts=list(pool.map(lambda _:self.receipt(job,tasks[0]),range(2)))
        self.assertEqual([r.status_code for r in receipts],[200,200])
        with self.factory() as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot)),1)

    def test_server_probe_resets_browser_context(self):
        job=self.start(marketplace='wb',kind='probe')
        self.receipt(job,self.next(job).json()['task'])
        with self.factory() as s:
            previous=s.get(Policy,1).context_version
        self.client.post('/jobs',headers=self.headers,data={'csrf_token':'csrf','kind':'probe','marketplace':'wb','collector':'server'})
        with self.factory() as s:
            self.assertIsNone(s.get(Policy,1).browser_client_id)
            self.assertEqual(s.get(Policy,1).context_version,previous+1)

    def test_ui_controls_download_and_narrow_permissions(self):
        from zipfile import ZipFile
        from io import BytesIO
        import json
        job=self.start()
        for path in ['/integrations',f'/jobs/{job}']:
            response=self.client.get(path,headers=self.headers)
            self.assertEqual(response.status_code,200,response.text)
        html=self.client.get(f'/jobs/{job}',headers=self.headers).text
        self.assertIn('data-browser-job',html)
        self.assertIn('Пропустить карточку',html)
        response=self.client.get('/browser-extension',headers=self.headers)
        self.assertEqual(response.status_code,200)
        with ZipFile(BytesIO(response.content)) as z:
            manifest=json.loads(z.read('bellenne-parity-extension/manifest.json'))
            self.assertEqual(manifest['permissions'],['storage','alarms'])
            self.assertNotIn('<all_urls>',manifest['host_permissions'])
            self.assertEqual(manifest['manifest_version'],3)


if __name__=='__main__': unittest.main()
