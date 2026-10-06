"""Real unpacked MV3 extension, intercepted synthetic pages, no live accounts."""
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zipfile import ZipFile
import unittest
import os
from unittest.mock import patch
import tests.test_storefront as fixtures
from sqlalchemy import func, select
from app.models import BrowserTask, CollectionAttempt, Job, Product, Snapshot
from app.storefront import EXTRACT


WB = '''<header><a>Москва</a></header><table><tr><td>Артикул</td><td>123</td></tr></table><div id="root"></div><p>Рекомендуем 1 ₽</p><script>setTimeout(()=>document.querySelector('#root').innerHTML='<div class="priceBlockPriceWrap--test"><button class="priceBlockWalletPrice--test"><h2>2917 ₽</h2></button><ins class="priceBlockFinalPrice--test">4000 ₽</ins><span class="priceBlockOldPrice--test">10000 ₽</span></div>',200)</script>'''
OZON = '''<header><button>Москва</button></header><button>Артикул: 789</button><div data-widget="webPrice"><button><span class="tsHeadline600Large">3043 ₽</span>С Ozon Картой</button><span class="tsHeadline500Medium">4000 ₽</span><span style="text-decoration:line-through">10000 ₽</span></div><p>В рассрочку 10 ₽</p>'''


class ExtensionTests(unittest.TestCase):
    setUp = fixtures.StorefrontJobsTests.setUp
    tearDown = fixtures.StorefrontJobsTests.tearDown

    def launch(self, runtime, directory):
        extension = Path(directory)/'bellenne-parity-extension'
        context = runtime.chromium.launch_persistent_context(str(Path(directory)/'profile'),channel='chromium',headless=True,
            args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'])
        context.route('http://localhost:17863/**',self.parity_route)
        context.route('https://www.wildberries.ru/**',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body=WB))
        context.route('https://www.ozon.ru/**',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body=OZON))
        return context

    def parity_route(self, route):
        request=route.request
        url=urlsplit(request.url)
        path=url.path.removeprefix('/parity') or '/'
        headers={**request.headers,**self.headers}
        response=self.client.request(request.method,path+('?' + url.query if url.query else ''),headers=headers,content=request.post_data_buffer,follow_redirects=False)
        if url.hostname == 'one.customcraft-mes.ru' and response.status_code == 303:
            # A fulfilled Chromium redirect can bypass interception on its next
            # request. Keep this HTTPS fixture offline with an explicit navigation.
            route.fulfill(content_type='text/html; charset=utf-8', body=f'<script>location.replace({json.dumps(response.headers["location"])})</script>')
            return
        if path.endswith('/receipt') and getattr(self,'lose_receipt_response',False) and response.status_code==200:
            self.lose_receipt_response=False
            route.fulfill(status=503,content_type='application/json',body='{"detail":"Fixture: connection interrupted after commit"}')
            return
        filtered={k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding')}
        route.fulfill(status=response.status_code,headers=filtered,body=response.content)

    def unpack(self,directory):
        with ZipFile(Path(__file__).parent.parent/'app/static/bellenne-parity-extension.zip') as z:
            z.extractall(directory)

    def test_production_origin_collects_prices_and_accepts_parity_root(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context = self.launch(runtime, directory)
            context.route('https://one.customcraft-mes.ru/**', self.parity_route)
            try:
                page = context.new_page()
                page.goto('https://one.customcraft-mes.ru/parity')
                worker = context.service_workers[0]
                urls = ['https://one.customcraft-mes.ru/parity', 'https://one.customcraft-mes.ru/parity/',
                        'https://one.customcraft-mes.ru/parity/jobs/13', 'https://one.customcraft-mes.ru/parity-other',
                        'https://one.customcraft-mes.ru/', 'https://one.customcraft-mes.ru.evil.example/parity',
                        'http://one.customcraft-mes.ru/parity', 'http://localhost:9999/parity']
                self.assertEqual(worker.evaluate('urls => urls.map(url => parityCore.bridge(url))', urls),
                                 [True, True, True, False, False, False, False, False])
                page.goto('https://one.customcraft-mes.ru/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено', timeout=15000)
                page.get_by_role('button', name='Собрать цены', exact=True).click()
                page.wait_for_url('https://one.customcraft-mes.ru/parity/jobs/*')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено', timeout=30000)
                with self.factory() as session:
                    job = session.scalar(select(Job).order_by(Job.id.desc()))
                    self.assertEqual((job.status, job.prices_updated), ('success', 2))
                    self.assertEqual({str(p.loyalty_price) for p in session.scalars(select(Snapshot))}, {'2917', '3043'})
            finally:
                context.close()

    def test_active_production_job_cannot_be_rebound_to_localhost(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context = self.launch(runtime, directory)
            context.route('https://one.customcraft-mes.ru/**', self.parity_route)
            held = []
            context.route('https://www.ozon.ru/**', lambda route: held.append(route))
            try:
                page = context.new_page()
                page.goto('https://one.customcraft-mes.ru/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено', timeout=15000)
                page.get_by_role('button', name='Собрать цены', exact=True).click()
                page.wait_for_url('https://one.customcraft-mes.ru/parity/jobs/*')
                expect(page.locator('[data-job-phase]')).to_have_text('Сбор в вашем браузере', timeout=15000)
                worker = context.service_workers[0]
                before = worker.evaluate("chrome.storage.local.get(['run','outbox'])")
                self.assertEqual(before['run']['origin'], 'https://one.customcraft-mes.ru')
                local = context.new_page()
                local.goto(f"http://localhost:17863/parity/jobs/{before['run']['jobId']}")
                expect(local.locator('[data-extension-status]')).to_contain_text('другой установки', timeout=10000)
                after = worker.evaluate("chrome.storage.local.get(['run','outbox'])")
                self.assertEqual(after['run']['origin'], before['run']['origin'])
                self.assertEqual(after['run']['bridgeTab'], before['run']['bridgeTab'])
                popup = context.new_page()
                popup.goto(worker.url.replace('/worker.js', '/popup.html'))
                self.assertTrue(popup.evaluate("chrome.runtime.sendMessage({type:'OPEN',target:'parity'})")['ok'])
                self.assertTrue(page.url.startswith('https://one.customcraft-mes.ru/parity/jobs/'))
                # Navigating the assigned bridge to another host must also block
                # background API calls even when its tab ID stays unchanged.
                page.goto(f"http://localhost:17863/parity/jobs/{before['run']['jobId']}")
                result = worker.evaluate("async run => {try {await api(run,'claim',{}); return 'unsafe';} catch(e) {return e.message;}}", before['run'])
                self.assertIn('вкладку Parity', result)
            finally:
                for route in held:
                    route.abort()
                context.unroute_all(behavior='ignoreErrors')
                context.close()

    def test_one_click_collects_dynamic_cards_with_real_extension(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context=self.launch(runtime,directory)
            try:
                page=context.new_page()
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено',timeout=15000)
                if os.getenv('PARITY_TEST_ARTIFACT_DIR'):
                    folder=Path(os.environ['PARITY_TEST_ARTIFACT_DIR'])
                    folder.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(folder/'extension-desktop.png'))
                page.get_by_role('button',name='Собрать цены',exact=True).click()
                page.wait_for_url('**/parity/jobs/*')
                try:
                    expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено',timeout=25000)
                except AssertionError:
                    print('Fixture extension status:',page.locator('[data-extension-status]').text_content())
                    print('Fixture worker state:',context.service_workers[0].evaluate('chrome.storage.local.get()'))
                    print('Fixture pages:',[p.url for p in context.pages])
                    for card in context.pages:
                        if 'ozon.ru' in card.url:
                            print('Fixture DOM observation:',card.evaluate(EXTRACT,{'marketplace':'ozon'}))
                    raise
                with self.factory() as s:
                    job=s.scalar(select(Job).order_by(Job.id.desc()))
                    self.assertEqual((job.kind,job.status,job.prices_updated),('browser','success',2))
                    self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot)),2)
                    self.assertEqual({str(p.loyalty_price) for p in s.scalars(select(Snapshot))},{'2917','3043'})
                self.assertEqual(len([p for p in context.pages if urlsplit(p.url).hostname in ['www.wildberries.ru','www.ozon.ru']]),1)
            finally:
                context.close()

    def test_pause_browser_restart_resume_retains_queue_and_snapshot(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context=self.launch(runtime,directory)
            page=context.new_page()
            response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser'})
            job_id=response.json()['job_id']
            try:
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                # Pausing before collection verifies persistence without racing
                # an arbitrary sleep against a successful card capture.
                self.client.post(f'/browser/{job_id}/control',headers=self.headers,data={'csrf_token':'csrf','action':'pause'})
                expect(page.locator('[data-job-phase]')).to_have_text('Сбор приостановлен',timeout=15000)
                page.wait_for_function("document.querySelector('[data-extension-status]').textContent.includes('подключено')")
                worker=context.service_workers[0]
                identity=worker.evaluate('(async()=> (await chrome.storage.local.get("clientId")).clientId)()')
            finally:
                context.close()
            context=self.launch(runtime,directory)
            try:
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено',timeout=15000)
                self.assertEqual(context.service_workers[0].evaluate('(async()=> (await chrome.storage.local.get("clientId")).clientId)()'),identity)
                page.locator('[data-browser-control="resume"]').click()
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено',timeout=25000)
                with self.factory() as s:
                    self.assertEqual(s.get(Job,job_id).prices_updated,2)
                    self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot)),2)
            finally:
                context.close()

    def test_mobile_setup_has_no_page_overflow_and_menu_works(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context=self.launch(runtime,directory)
            try:
                page=context.new_page()
                page.set_viewport_size({'width':390,'height':844})
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено',timeout=15000)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),390)
                menu=page.get_by_role('button',name='Меню',exact=True)
                expect(menu).to_be_visible()
                menu.click()
                expect(page.get_by_role('link',name='Обзор',exact=True)).to_be_visible()
                menu.click()
                if os.getenv('PARITY_TEST_ARTIFACT_DIR'):
                    folder=Path(os.environ['PARITY_TEST_ARTIFACT_DIR'])
                    folder.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(folder/'extension-mobile.png'))
            finally:
                context.close()

    def test_lost_receipt_reply_survives_restart_without_duplicate(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            self.lose_receipt_response=True
            response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'wb'})
            job_id=response.json()['job_id']
            context=self.launch(runtime,directory)
            try:
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                page.wait_for_function("document.querySelector('[data-extension-status]').textContent.includes('подключено')")
                worker=context.service_workers[0]
                worker.evaluate('''async () => {
                    const deadline=Date.now()+15000;
                    while(Date.now()<deadline) {
                        const state=await chrome.storage.local.get();
                        if(state.outbox && state.run && !state.run.reading) return;
                        await new Promise(r=>setTimeout(r,50));
                    }
                    throw new Error('Fixture outbox not retained');
                }''')
                with self.factory() as s:
                    self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot)),1)
            finally:
                context.close()

            context=self.launch(runtime,directory)
            try:
                page=context.new_page()
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено',timeout=15000)
                self.assertIsNone(context.service_workers[0].evaluate('(async()=> (await chrome.storage.local.get("outbox")).outbox)()'))
                with self.factory() as s:
                    self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot)),1)
                    self.assertEqual(s.get(Job,job_id).status,'success')
            finally:
                context.close()

    def test_hanging_navigation_skips_after_retry_budget_without_a_snapshot(self):
        from playwright.sync_api import sync_playwright, expect
        from app import browser
        original_claim=browser.claim
        def short_timeout(session,job,client_id,timeout_seconds):
            return original_claim(session,job,client_id,0.2)
        held=[]
        with TemporaryDirectory() as directory, sync_playwright() as runtime, patch('app.main.browser.claim',side_effect=short_timeout):
            self.unpack(directory)
            context=self.launch(runtime,directory)
            context.route('https://www.wildberries.ru/**',lambda route:held.append(route))
            try:
                page=context.new_page()
                response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'wb'})
                job_id=response.json()['job_id']
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено',timeout=60000)
                self.assertTrue(held)
                state=context.service_workers[0].evaluate('chrome.storage.local.get()')
                self.assertIsNone(state['run'])
                self.assertEqual(state['progress']['attempt'],self.settings.retry_attempts)
                self.assertEqual(len(held),self.settings.retry_attempts)
                with self.factory() as s:
                    self.assertEqual((s.get(Job,job_id).phase,s.get(Job,job_id).status,s.get(Job,job_id).errors_count),('complete','failed',1))
                    self.assertEqual(s.scalar(select(func.count()).select_from(Snapshot)),0)
            finally:
                for route in held:
                    try: route.abort()
                    except Exception: pass  # Replaced tabs have already aborted their request.
                context.close()



    def test_popup_keeps_finished_progress_after_layout_skip(self):
        from playwright.sync_api import sync_playwright, expect
        from app import browser
        original_claim = browser.claim
        def timeout(session, job, client_id, timeout_seconds):
            result = original_claim(session, job, client_id, timeout_seconds)
            if result.get('task') and result['task']['marketplace'] == 'ozon':
                result['task']['timeout_seconds'] = 2
            return result
        with TemporaryDirectory() as directory, sync_playwright() as runtime, patch('app.main.browser.claim', side_effect=timeout):
            self.unpack(directory)
            context = self.launch(runtime, directory)
            context.route('https://www.ozon.ru/**', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='<button>Артикул: 789</button><h1>Нет блока цены</h1>'))
            try:
                page = context.new_page()
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено', timeout=15000)
                page.get_by_role('button', name='Собрать цены', exact=True).click()
                page.wait_for_url('**/parity/jobs/*')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено', timeout=30000)
                popup = context.new_page()
                extension_id = context.service_workers[0].url.split('/')[2]
                popup.goto(f'chrome-extension://{extension_id}/popup.html')
                expect(popup.locator('#state')).to_have_text('Сбор завершён')
                expect(popup.locator('#counts')).to_have_text('Обработано 2 из 2')
                expect(popup.locator('#results')).to_have_text('Цен сохранено: 1 · пропущено: 1')
                popup.reload()
                expect(popup.locator('#counts')).to_have_text('Обработано 2 из 2')
                self.assertLessEqual(popup.evaluate('document.documentElement.scrollWidth'), popup.evaluate('innerWidth'))
                with self.factory() as session:
                    self.assertEqual(session.scalar(select(BrowserTask).where(BrowserTask.state == 'skipped')).error_code, 'layout')
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 1)
            finally:
                context.close()

    def test_ready_price_is_read_while_subresource_keeps_loading(self):
        from playwright.sync_api import sync_playwright, expect
        held=[]
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context=self.launch(runtime,directory)
            def card(route):
                if route.request.url.endswith('pending-image'):
                    held.append(route)
                else:
                    route.fulfill(content_type='text/html; charset=utf-8',body=WB+'<img src="/pending-image">')
            context.route('https://www.wildberries.ru/**',card)
            try:
                page=context.new_page()
                response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'wb'})
                job_id=response.json()['job_id']
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено',timeout=25000)
                self.assertTrue(held)
                with self.factory() as s:
                    self.assertEqual(s.get(Job,job_id).prices_updated,1)
            finally:
                for route in held:
                    try: route.abort()
                    except Exception: pass
                context.close()

    def test_unanswered_bridge_times_out_and_popup_stays_responsive(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context=self.launch(runtime,directory)
            try:
                response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser'})
                job_id=response.json()['job_id']
                self.client.post(f'/browser/{job_id}/control',headers=self.headers,data={'csrf_token':'csrf','action':'pause'})
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено',timeout=15000)
                worker=context.service_workers[0]
                worker.evaluate("""() => {
                    globalThis.fixtureSend=chrome.tabs.sendMessage.bind(chrome.tabs);
                    chrome.tabs.sendMessage=(id,msg)=>msg.type==='API' ? new Promise(()=>{}) : fixtureSend(id,msg);
                    queue(pump);
                }""")
                popup=context.new_page()
                popup.goto(f'chrome-extension://{worker.url.split('/')[2]}/popup.html')
                expect(popup.locator('#counts')).to_have_text('Обработано 0 из 2',timeout=3000)
                expect(popup.locator('#state')).to_have_text('Нет связи с Parity',timeout=15000)
                expect(popup.locator('#error')).to_contain_text('очередь')
                worker.evaluate('() => { chrome.tabs.sendMessage=fixtureSend; queue(pump); }')
                expect(popup.locator('#state')).to_have_text('Сбор на паузе',timeout=15000)
                expect(popup.locator('#resume')).to_be_enabled()
                with self.factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),0)
            finally:
                context.close()

    def test_popup_manual_pause_resume_and_automatic_block_skip(self):
        from playwright.sync_api import sync_playwright, expect
        from app import browser
        original_claim=browser.claim
        def timeout(session,job,client_id,timeout_seconds):
            value=original_claim(session,job,client_id,timeout_seconds)
            if value.get("task") and value["task"]["marketplace"]=="ozon": value["task"]["timeout_seconds"]=2
            return value
        with TemporaryDirectory() as directory, sync_playwright() as runtime, patch('app.main.browser.claim',side_effect=timeout):
            self.unpack(directory)
            context=self.launch(runtime,directory)
            ozon_requests=[]
            def blocked_card(route):
                ozon_requests.append(route.request.url)
                route.fulfill(content_type='text/html; charset=utf-8',body='<h1>Доступ ограничен</h1>')
            context.route('https://www.ozon.ru/**',blocked_card)
            try:
                response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser'})
                job_id=response.json()['job_id']
                self.client.post(f'/browser/{job_id}/control',headers=self.headers,data={'csrf_token':'csrf','action':'pause'})
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено',timeout=15000)
                popup=context.new_page()
                popup.goto(f'chrome-extension://{context.service_workers[0].url.split('/')[2]}/popup.html')
                expect(popup.locator('#state')).to_have_text('Сбор на паузе')
                popup.locator('#resume').click()
                expect(popup.locator('#state')).to_have_text('Сбор завершён',timeout=20000)
                expect(popup.locator('#results')).to_have_text('Цен сохранено: 1 · пропущено: 1')
                with self.factory() as session:
                    self.assertEqual(session.get(Job,job_id).status,'partial')
                    self.assertEqual(len(ozon_requests),1)
                    self.assertEqual(session.scalar(select(BrowserTask).where(BrowserTask.state=='skipped')).error_code,'blocked')
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),1)
            finally:
                context.close()

    def test_stale_finished_job_from_old_profile_does_not_block_next_job(self):
        from playwright.sync_api import sync_playwright, expect
        from uuid import uuid4
        old_client=str(uuid4())
        old=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'wb'}).json()['job_id']
        task=self.client.post(f'/api/browser/{old}/claim',headers=self.headers,json={'csrf_token':'csrf','client_id':old_client}).json()['task']
        self.client.post(f'/api/browser/{old}/receipt',headers=self.headers,json={
            'csrf_token':'csrf','client_id':old_client,'task_id':task['id'],'token':task['token'],
            'observed_at':datetime.now(timezone.utc).isoformat(),
            'observation':{'identity':task['identifier'],'city':'Москва','hasBox':True,'benefitConfirmed':True,'benefit':'2917 ₽','regular':'4000 ₽','reference':'10000 ₽'}})
        new=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'wb'}).json()['job_id']
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context=self.launch(runtime,directory)
            try:
                worker=context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
                worker.evaluate("""async old => {
                    await serial;
                    await chrome.storage.local.set({clientId:crypto.randomUUID(),run:{jobId:old},outbox:null,progress:null});
                }""",old)
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{new}')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено',timeout=15000)
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено',timeout=25000)
                with self.factory() as session:
                    self.assertEqual(session.get(Job,new).status,'success')
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),2)
            finally:
                context.close()

    def test_real_profile_conflict_has_explicit_popup_error_and_valid_job_number(self):
        from playwright.sync_api import sync_playwright, expect
        from uuid import uuid4
        job=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'wb'}).json()['job_id']
        self.client.post(f'/api/browser/{job}/claim',headers=self.headers,json={'csrf_token':'csrf','client_id':str(uuid4())})
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context=self.launch(runtime,directory)
            try:
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{job}')
                expect(page.locator('[data-extension-status]')).to_contain_text('другим профилем',timeout=15000)
                popup=context.new_page()
                popup.goto(f'chrome-extension://{context.service_workers[0].url.split('/')[2]}/popup.html')
                expect(popup.locator('#state')).to_have_text('Задание в другом профиле браузера')
                expect(popup.locator('#error')).to_contain_text('другим профилем')
                expect(popup.locator('#counts')).to_have_text('Прогресс пока недоступен')
                expect(popup.locator('#open')).to_have_text(f'Открыть задание #{job}')
                expect(popup.locator('#resume')).to_be_disabled()
                with self.factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),0)
            finally:
                context.close()

    def test_navigation_retries_reload_then_replace_tab_and_save_once(self):
        from playwright.sync_api import sync_playwright, expect
        from app import browser
        original_claim=browser.claim
        requests=[]
        def timeout(session,job,client_id,timeout_seconds):
            return original_claim(session,job,client_id,0.2 if len(requests)<3 else timeout_seconds)
        with TemporaryDirectory() as directory, sync_playwright() as runtime, patch('app.main.browser.claim',side_effect=timeout):
            self.unpack(directory)
            context=self.launch(runtime,directory)
            def card(route):
                requests.append(route)
                if len(requests)>=3: route.fulfill(content_type='text/html; charset=utf-8',body=OZON)
            context.route('https://www.ozon.ru/**',card)
            try:
                page=context.new_page()
                response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'ozon'})
                job_id=response.json()['job_id']
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено',timeout=40000)
                self.assertEqual(len(requests),3)
                self.assertEqual(len([p for p in context.pages if 'ozon.ru' in p.url]),1)
                with self.factory() as session:
                    self.assertEqual(session.get(Job,job_id).status,'success')
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),1)
                    self.assertEqual(str(session.scalar(select(Snapshot)).loyalty_price),'3043')
            finally:
                for route in requests[:2]:
                    try: route.abort()
                    except Exception: pass
                context.close()

    def test_retry_budget_survives_pause_and_browser_restart(self):
        from playwright.sync_api import sync_playwright, expect
        from app import browser
        original_claim=browser.claim
        def timeout(session,job,client_id,timeout_seconds):
            return original_claim(session,job,client_id,0.2)
        with TemporaryDirectory() as directory, sync_playwright() as runtime, patch('app.main.browser.claim',side_effect=timeout):
            self.unpack(directory)
            held=[]
            context=self.launch(runtime,directory)
            context.route('https://www.ozon.ru/**',lambda route:held.append(route))
            response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'ozon'})
            job_id=response.json()['job_id']
            try:
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                popup=context.new_page()
                worker=context.service_workers[0]
                popup.goto(f'chrome-extension://{worker.url.split('/')[2]}/popup.html')
                expect(popup.locator('#state')).to_contain_text('Автоматический повтор',timeout=15000)
                popup.locator('#pause').click()
                expect(popup.locator('#state')).to_have_text('Сбор на паузе')
                self.assertEqual(worker.evaluate('(async()=> (await chrome.storage.local.get("run")).run.attempt)()'),2)
            finally:
                for route in held:
                    try: route.abort()
                    except Exception: pass
                held.clear()
                context.close()
            context=self.launch(runtime,directory)
            context.route('https://www.ozon.ru/**',lambda route:held.append(route))
            try:
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено',timeout=15000)
                worker=context.service_workers[0]
                self.assertEqual(worker.evaluate('(async()=> (await chrome.storage.local.get("run")).run.attempt)()'),2)
                page.locator('[data-browser-control="resume"]').click()
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено',timeout=40000)
                self.assertEqual(worker.evaluate('(async()=> (await chrome.storage.local.get("progress")).progress.attempt)()'),3)
                with self.factory() as session:
                    self.assertEqual(session.get(Job,job_id).prices_updated,0)
            finally:
                for route in held:
                    try: route.abort()
                    except Exception: pass
                held.clear()
                context.close()

    def test_worker_samples_price_when_page_reader_timer_never_returns(self):
        from playwright.sync_api import sync_playwright, expect
        from app import browser
        original_claim=browser.claim
        def timeout(session,job,client_id,timeout_seconds):
            return original_claim(session,job,client_id,0.2)
        with TemporaryDirectory() as directory, sync_playwright() as runtime, patch('app.main.browser.claim',side_effect=timeout):
            self.unpack(directory)
            # Simulate a throttled/stuck page timer while message handling and DOM
            # remain available. This fixture never emits OBSERVATION on its own.
            script=Path(directory)/'bellenne-parity-extension/card.js'
            script.write_text("chrome.runtime.onMessage.addListener((m,s,r)=>{ if(m.type==='SAMPLE') r({observation:parityExtract({marketplace:m.task.marketplace})}); else r({ready:true,ok:true}); });",encoding='utf-8')
            context=self.launch(runtime,directory)
            try:
                response=self.client.post('/jobs',headers={**self.headers,'Accept':'application/json'},data={'csrf_token':'csrf','collector':'browser','kind':'probe','marketplace':'ozon'})
                job_id=response.json()['job_id']
                page=context.new_page()
                page.goto(f'http://localhost:17863/parity/jobs/{job_id}')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено',timeout=35000)
                with self.factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)),1)
            finally:
                context.close()

    def test_wrong_sku_skips_card_and_finishes_queue_without_manual_action(self):
        from playwright.sync_api import sync_playwright, expect
        from app import browser
        original_claim = browser.claim
        def timeout(session, job, client_id, timeout_seconds):
            result = original_claim(session, job, client_id, timeout_seconds)
            if result.get('task') and result['task']['marketplace'] == 'ozon':
                result['task']['timeout_seconds'] = 2
            return result
        self.lose_receipt_response = True
        with TemporaryDirectory() as directory, sync_playwright() as runtime, patch('app.main.browser.claim', side_effect=timeout):
            self.unpack(directory)
            context = self.launch(runtime, directory)
            context.route('https://www.ozon.ru/**', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=OZON.replace('Артикул: 789', 'Артикул: 999')))
            try:
                page = context.new_page()
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено', timeout=15000)
                page.get_by_role('button', name='Собрать цены', exact=True).click()
                page.wait_for_url('**/parity/jobs/*')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено', timeout=35000)
                extension_id = context.service_workers[0].url.split('/')[2]
                popup = context.new_page()
                popup.goto(f'chrome-extension://{extension_id}/popup.html')
                expect(popup.locator('#state')).to_have_text('Сбор завершён')
                expect(popup.locator('#counts')).to_have_text('Обработано 2 из 2')
                expect(popup.locator('#results')).to_have_text('Цен сохранено: 1 · пропущено: 1')
                with self.factory() as session:
                    job = session.scalar(select(Job).order_by(Job.id.desc()))
                    self.assertEqual((job.status, job.prices_updated, job.errors_count), ('partial', 1, 1))
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 1)
                    snapshot = session.scalar(select(Snapshot))
                    self.assertEqual(session.get(Product, snapshot.product_id).marketplace, 'wb')
                    skipped = session.scalar(select(BrowserTask).where(BrowserTask.state == 'skipped'))
                    self.assertEqual(skipped.error_code, 'identity')
                    self.assertEqual(session.get(Product, skipped.product_id).marketplace, 'ozon')
                    self.assertEqual(session.scalar(select(func.count()).select_from(CollectionAttempt).where(CollectionAttempt.status == 'skipped')), 1)
            finally:
                context.close()

    def test_marketplace_redirect_and_missing_moscow_do_not_block_prices(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context = self.launch(runtime, directory)
            context.route('https://www.ozon.ru/**', lambda route: route.fulfill(
                content_type='text/html', body='<script>location.replace("https://ozon.ru/product/test-789/")</script>'))
            context.route('https://ozon.ru/**', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=OZON.replace('<header><button>Москва</button></header>', '')))
            context.route('https://www.wildberries.ru/**', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=WB.replace('Москва', 'Казань')))
            try:
                page = context.new_page()
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено', timeout=15000)
                page.get_by_role('button', name='Собрать цены', exact=True).click()
                page.wait_for_url('**/parity/jobs/*')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено', timeout=30000)
                with self.factory() as session:
                    job = session.scalar(select(Job).order_by(Job.id.desc()))
                    self.assertEqual((job.status, job.prices_updated, job.errors_count), ('success', 2, 0))
                    snapshots = list(session.scalars(select(Snapshot)))
                    self.assertEqual(len(snapshots), 2)
                    self.assertTrue(all(snapshot.context['city'] is None and not snapshot.context['region_checked'] for snapshot in snapshots))
                extension_id = context.service_workers[0].url.split('/')[2]
                popup = context.new_page()
                popup.goto(f'chrome-extension://{extension_id}/popup.html')
                expect(popup.locator('#state')).to_have_text('Сбор завершён')
                expect(popup.locator('#results')).to_have_text('Цен сохранено: 2 · пропущено: 0')
                self.assertNotIn('выберите Москву', page.content())
            finally:
                context.close()

    def test_external_redirect_skips_unreadable_card_without_setup_prompt(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context = self.launch(runtime, directory)
            context.route('https://www.ozon.ru/**', lambda route: route.fulfill(
                content_type='text/html', body='<script>location.replace("https://example.org/login")</script>'))
            context.route('https://example.org/**', lambda route: route.fulfill(content_type='text/html', body='Unavailable fixture'))
            try:
                page = context.new_page()
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено', timeout=15000)
                validation = context.service_workers[0].evaluate(
                    "task => [parityCore.card(task), parityCore.card(task,undefined), parityCore.card(task,'https://example.org/login')]",
                    {'identifier': '789', 'marketplace': 'ozon', 'url': 'https://www.ozon.ru/product/789/'})
                self.assertEqual(validation, [True, False, False])
                page.get_by_role('button', name='Собрать цены', exact=True).click()
                page.wait_for_url('**/parity/jobs/*')
                try:
                    expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено', timeout=30000)
                except AssertionError:
                    print('Fixture message:', page.locator('[data-browser-message]').text_content())
                    print('Fixture state:', context.service_workers[0].evaluate('chrome.storage.local.get()'))
                    print('Fixture URLs:', [card.url for card in context.pages])
                    raise
                with self.factory() as session:
                    job = session.scalar(select(Job).order_by(Job.id.desc()))
                    self.assertEqual((job.status, job.prices_updated, job.errors_count), ('partial', 1, 1))
                    skipped = session.scalar(select(BrowserTask).where(BrowserTask.state == 'skipped'))
                    self.assertEqual(skipped.error_code, 'unavailable')
                    snapshot = session.scalar(select(Snapshot))
                    self.assertEqual(session.get(Product, snapshot.product_id).marketplace, 'wb')
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 1)
            finally:
                context.close()

    def test_mixed_card_errors_and_wb_sold_out_never_pause_queue(self):
        from playwright.sync_api import sync_playwright, expect
        from app import browser
        with self.factory() as session:
            existing = session.scalar(select(Product).where(Product.marketplace == 'wb'))
            for identifier in ['124', '125', '126']:
                session.add(Product(owner_id=1, account_id=existing.account_id, marketplace='wb',
                    external_id=identifier, sku=identifier, seller_article=f'TEST-{identifier}', name='Тест', status='active'))
            session.commit()
        original_claim = browser.claim
        def timeout(session, job, client_id, timeout_seconds):
            result = original_claim(session, job, client_id, timeout_seconds)
            if result.get('task') and result['task']['identifier'] == '125':
                result['task']['timeout_seconds'] = 2
            return result
        def wb_card(route):
            identifier = route.request.url.split('/catalog/')[1].split('/')[0]
            if identifier == '124':
                html = '<main><h1>Товар закончился</h1><div>Нет в наличии</div></main>'
            elif identifier == '125':
                html = '<table><tr><td>Артикул</td><td>125</td></tr></table><h1>Непрочитанный блок цены</h1>'
            else:
                html = WB.replace('<td>123</td>', f'<td>{identifier}</td>')
            route.fulfill(content_type='text/html; charset=utf-8', body=html)
        with TemporaryDirectory() as directory, sync_playwright() as runtime, patch('app.main.browser.claim', side_effect=timeout):
            self.unpack(directory)
            context = self.launch(runtime, directory)
            context.route('https://www.wildberries.ru/**', wb_card)
            context.route('https://www.ozon.ru/**', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='<h1>Доступ ограничен</h1>'))
            try:
                page = context.new_page()
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено', timeout=15000)
                page.get_by_role('button', name='Собрать цены', exact=True).click()
                page.wait_for_url('**/parity/jobs/*')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено', timeout=45000)
                popup = context.new_page()
                popup.goto(f'chrome-extension://{context.service_workers[0].url.split("/")[2]}/popup.html')
                expect(popup.locator('#counts')).to_have_text('Обработано 5 из 5')
                expect(popup.locator('#results')).to_have_text('Цен сохранено: 2 · пропущено: 3')
                with self.factory() as session:
                    job = session.scalar(select(Job).order_by(Job.id.desc()))
                    self.assertEqual((job.phase, job.status), ('complete', 'partial'))
                    self.assertEqual({task.error_code for task in session.scalars(select(BrowserTask).where(BrowserTask.state == 'skipped'))}, {'blocked', 'unavailable', 'layout'})
                    self.assertEqual(session.scalar(select(func.count()).select_from(Snapshot)), 2)
            finally:
                context.close()

    def test_local_tab_failure_skips_card_and_continues(self):
        from playwright.sync_api import sync_playwright, expect
        with TemporaryDirectory() as directory, sync_playwright() as runtime:
            self.unpack(directory)
            context = self.launch(runtime, directory)
            try:
                page = context.new_page()
                page.goto('http://localhost:17863/parity/integrations')
                expect(page.locator('[data-extension-status]')).to_contain_text('подключено', timeout=15000)
                context.service_workers[0].evaluate('''() => {
                    const original = chrome.tabs.update.bind(chrome.tabs);
                    chrome.tabs.update = (id, options) => options.url?.includes('ozon.ru') ? Promise.reject(new Error('Fixture tab error')) : original(id, options);
                }''')
                page.get_by_role('button', name='Собрать цены', exact=True).click()
                page.wait_for_url('**/parity/jobs/*')
                expect(page.locator('[data-job-phase]')).to_have_text('Задание завершено', timeout=30000)
                with self.factory() as session:
                    job = session.scalar(select(Job).order_by(Job.id.desc()))
                    self.assertEqual((job.status, job.prices_updated, job.errors_count), ('partial', 1, 1))
                    self.assertEqual(session.scalar(select(BrowserTask).where(BrowserTask.state == 'skipped')).error_code, 'internal')
            finally:
                context.close()

if __name__=='__main__': unittest.main()
