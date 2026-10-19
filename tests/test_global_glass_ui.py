"""全站浏览器回归：合成数据与完全接口拦截，禁止真实业务写入。

运行时必须显式设置 BASE_URL。浏览器使用洛杉矶时区以检查日志北京时间。
"""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import re
import unittest
from urllib.parse import parse_qs, urlsplit
try:
    from playwright.sync_api import expect, sync_playwright
except ModuleNotFoundError:
    expect = sync_playwright = None

BASE = os.environ.get('BASE_URL', '').rstrip('/')
OUT = Path(os.environ.get('SCREENSHOT_DIR', '/verification'))
CHROME = os.environ.get('CHROMIUM_PATH', '/ms-playwright/chromium-1243/chrome-linux64/chrome')
ROUTES = [('/image', '生图'), ('/accounts', '号池管理'), ('/image-manager', '图片管理'), ('/logs', '日志管理'), ('/debug', '调试'), ('/settings', '设置')]
MODELS = ['auto', 'gpt-5.3-mini', 'gpt-5.6', 'gpt-5.6-mini', 'gpt-6', 'gpt-6-mini', 'gpt-image-2', 'gpt-image-2.5']
ACCOUNTS = [dict(access_token=f'example-access-{i:03d}', account_ref=f'{i+1:064x}',
    email=f'account{i+1}@example.test', source_type='codex', type='edu_plus' if i < 13 else 'Free',
    status='正常' if i < 20 else ['限流', '限流', '异常', '异常', '禁用', '禁用'][i-20],
    quota=120, success=i, fail=i % 3, image_inflight=0, proxy='', created_at='2026-10-15 03:48:00',
    has_refresh_token=True, auto_refresh_token=True, image_cleanup_enabled=False,
    access_token_expires_at='2026-11-01T00:30:00Z') for i in range(26)]
SETTINGS = dict(base_url='https://preview.example.test', image_transport='conversation', image_models=MODELS,
    default_image_model='gpt-image-2', refresh_account_interval_minute=60, image_retention_days=30,
    image_poll_timeout_secs=120, image_account_concurrency=1, log_levels=['INFO', 'WARNING', 'ERROR'])
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"><rect width="640" height="480" fill="#dce9ff"/><circle cx="320" cy="240" r="140" fill="#8cb9ff"/><text x="320" y="248" text-anchor="middle" font-family="sans-serif" font-size="36" fill="#172237">Preview</text></svg>'
IMAGES = [dict(rel=f'2026-10-20/preview-{i}.svg', name=f'preview-{i}.svg', date='2026-10-20',
    size=18000+i, url=f'/images/2026-10-20/preview-{i}.svg', created_at='2026-10-20T01:30:00Z',
    width=640, height=480, tags=['示例', '设计' if i % 2 else '测试']) for i in range(15)]
LOGS = [dict(id=f'example-log-{i:03d}', time='2026-10-20 09:30:00', type='call', summary=f'合成调用 {i+1}',
    detail=dict(status='success' if i % 3 else 'failed', duration_ms=1234, token_name='预览密钥', urls=[])) for i in range(45)]
METRICS = dict(window_seconds=60, rpm=3, requests=3, success=2, failed=1, avg_duration_ms=1234,
    by_endpoint=[], trend=[], total_requests=45, uptime_seconds=3600, server_time='2026-10-20T01:30:00Z')

@unittest.skipUnless(sync_playwright is not None and BASE, '需要 Playwright 和显式 BASE_URL')
class GlobalGlassUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=CHROME, headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'])
    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
    def setUp(self):
        self.contexts, self.errors, self.unexpected, self.unknown, self.requests = [], [], [], [], []
        self.allowed_writes = set()
        self.settings = copy.deepcopy(SETTINGS)
        self.management_enabled = True
        self.logs = copy.deepcopy(LOGS)
        self.image_tasks, self.file_tasks = [], []
        self.page = self.new_page()
    def tearDown(self):
        for context in self.contexts:
            context.close()
        self.assertEqual(self.errors, [], '不得有未捕获脚本异常')
        self.assertEqual(self.unexpected, [], '未知写入已拦截，禁止真实业务提交')
        self.assertEqual(self.unknown, [], '只读接口缺少 fixture，禁止绕过拦截')
    def new_page(self, width=1440, height=1000, empty=False, reduced_motion='no-preference', role='admin'):
        context = self.browser.new_context(viewport={'width':width, 'height':height}, color_scheme='light',
            timezone_id='America/Los_Angeles', reduced_motion=reduced_motion, service_workers='block')
        self.contexts.append(context)
        context.add_init_script("if (!localStorage.getItem('chatgpt2api-theme')) localStorage.setItem('chatgpt2api-theme', 'light')")
        accounts = [] if empty else ACCOUNTS
        def handle(route):
            request, parsed = route.request, urlsplit(route.request.url)
            path, method = parsed.path.rstrip('/'), request.method
            if parsed.netloc != urlsplit(BASE).netloc:
                return route.abort()
            if not (path.startswith(('/api/', '/auth/', '/v1/')) or path == '/version'):
                if path.startswith('/images/'):
                    return route.fulfill(content_type='image/svg+xml', body=SVG)
                return route.continue_()
            body = None
            if request.post_data:
                try:
                    body = json.loads(request.post_data)
                except (TypeError, ValueError):
                    body = '<non-json fixture>'
            self.requests.append(dict(method=method, path=path, query=parse_qs(parsed.query), body=body))
            if path == '/auth/login':
                return route.fulfill(json=dict(role=role, subject_id='preview-admin', name='预览管理员', ok=True, valid=True, version='1.7.0'))
            if method not in ('GET', 'HEAD'):
                if (method, path) not in self.allowed_writes:
                    self.unexpected.append(f'{method} {path}')
                    return route.fulfill(status=400, json={'detail':'fixture 禁止写入'})
                if path == '/api/images/management':
                    self.management_enabled = bool(body['enabled'])
                    return route.fulfill(json={'enabled':self.management_enabled})
                if path == '/api/settings':
                    self.settings.update(body or {})
                    return route.fulfill(json={'config':self.settings})
                if path == '/api/accounts':
                    return route.fulfill(json={'items':accounts, 'added':1, 'success':1, 'failed':0})
                self.unexpected.append(f'未定义响应 {method} {path}')
                return route.fulfill(status=400, json={'detail':'未定义写入 fixture'})
            payloads = {
                '/api/accounts':{'items':accounts},
                '/api/accounts/operations':{'items':[dict(id=a['account_ref'], label=a['email'], supported=True,
                    expires_at=a['access_token_expires_at'], has_refresh_token=True, auto_refresh_token=True,
                    image_cleanup_enabled=False, status=a['status']) for a in accounts]},
                '/v1/models':{'object':'list', 'data':[{'id':m,'object':'model'} for m in MODELS]},
                '/api/settings':{'config':self.settings}, '/api/cpa/pools':{'pools':[]}, '/api/sub2api/servers':{'servers':[]},
                '/api/auth/users':{'items':[]}, '/api/backups':{'items':[], 'state':{'running':False}, 'settings':{}},
                '/api/images/management':{'enabled':self.management_enabled}, '/api/third-party-apps':{'third_party_apps':{'infinite_canvas':{'enabled':False,'url':''}}}, '/api/image-tasks':{'items':self.image_tasks, 'missing_ids':[]},
                '/api/file-tasks':{'items':[], 'missing_ids':[]}, '/v1/editable-file-tasks':{'items':self.file_tasks, 'missing_ids':[]}, '/api/images':{'items':IMAGES},
                '/api/images/tags':{'tags':['示例','设计','测试']},
                '/api/images/storage':dict(image_count=15,image_size_bytes=270000,image_size_mb=0.26,total_count=15,total_size=270000,total_mb=0.26,free_mb=5000,disk_total_mb=10000,disk_used_mb=5000,disk_free_mb=5000),
                '/api/metrics/rpm':METRICS, '/api/proxy':{'proxy':{'enabled':False,'url':'','chatgpt_url':'','custom_url':''}},
                '/api/proxy/runtime':{'settings':{'enabled':False},'status':{'running':False}}, '/version':{'version':'1.7.0'},
            }
            if path == '/api/logs':
                q = parse_qs(parsed.query)
                size, number = int(q.get('page_size',[20])[0]), int(q.get('page',[1])[0])
                return route.fulfill(json=dict(items=self.logs[(number-1)*size:number*size], page=number, page_size=size, has_more=number*size<len(self.logs)))
            if path in payloads:
                return route.fulfill(json=payloads[path])
            self.unknown.append(f'{method} {path}')
            return route.fulfill(status=400, json={'detail':'缺少只读 fixture'})
        context.route('**/*', handle)
        page = context.new_page()
        page.set_default_timeout(8000)
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.goto(BASE+'/login')
        page.locator('input[type="password"]').fill('example-preview-key')
        page.get_by_role('button', name='登录', exact=True).click()
        expect(page).not_to_have_url(re.compile('/login/?$'))
        return page
    def goto(self, path):
        self.page.goto(BASE+path)
        expect(self.page.locator('main h1').first).to_be_visible()
    def assert_shell(self, path):
        page, sidebar = self.page, self.page.locator('aside[data-global-sidebar]')
        expect(page.locator('body')).to_have_attribute('data-global-glass', re.compile('.*'))
        expect(page.locator('main[data-page-content]')).to_have_count(1)
        if page.viewport_size['width'] < 768 and sidebar.count() == 0:
            expect(page.get_by_role('button',name='打开导航',exact=True)).to_be_visible()
            return
        expect(sidebar).to_have_count(1)
        nav = sidebar.get_by_role('navigation',name='主导航')
        for route, _ in ROUTES:
            expect(nav.locator(f'a[href="{route}"], a[href="{route}/"]')).to_have_count(1)
        selected = nav.locator('[aria-current="page"]')
        expect(selected).to_have_count(1)
        self.assertEqual(selected.get_attribute('href').rstrip('/'), path)
        for nav in page.locator('header nav').all():
            self.assertLess(nav.locator(','.join(f'a[href="{route}"]' for route,_ in ROUTES)).count(), 6)
        self.assertNotIn('资源管理', sidebar.inner_text())
        expect(page.get_by_role('button',name='打开无限画布',exact=True)).to_have_count(0)
    def assert_no_overflow(self):
        d = self.page.evaluate('() => ({client:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth})')
        self.assertLessEqual(d['root'], d['client']+1, d)
        self.assertLessEqual(d['body'], d['client']+1, d)
    def test_00_direct_entry_all_six_routes(self):
        for path,name in ROUTES:
            with self.subTest(route=path):
                self.goto(path)
                self.assert_shell(path)
                self.assert_no_overflow()
                self.page.screenshot(path=str(OUT/(name+'-desktop.png')), full_page=True)
    def test_10_spa_navigation_keeps_sidebar_node(self):
        self.goto('/accounts')
        self.assert_shell('/accounts')
        self.page.evaluate("window.__glassNode=document.querySelector('aside[data-global-sidebar]');window.__glassSPA='same-document'")
        for path,_ in ROUTES:
            self.page.locator(f'aside[data-global-sidebar] nav a[href="{path}"], aside[data-global-sidebar] nav a[href="{path}/"]').click()
            expect(self.page).to_have_url(re.compile(re.escape(path)+'/?$'))
            self.assert_shell(path)
            self.assertTrue(self.page.evaluate("window.__glassNode===document.querySelector('aside[data-global-sidebar]')"))
            self.assertEqual(self.page.evaluate('window.__glassSPA'), 'same-document')
    def test_20_responsive_left_sidebar_and_mobile_drawer(self):
        for width,height in [(1440,1000),(768,1024),(375,812)]:
            self.page.set_viewport_size({'width':width,'height':height})
            for path,name in ROUTES:
                with self.subTest(width=width,route=path):
                    self.goto(path)
                    self.assert_shell(path)
                    self.assert_no_overflow()
                    sidebar = self.page.locator('aside[data-global-sidebar]')
                    if width < 768:
                        opener = self.page.get_by_role('button', name='打开导航', exact=True)
                        opener.click()
                        expect(sidebar).to_be_visible()
                        self.assert_shell(path)
                        box = sidebar.bounding_box()
                        self.assertLessEqual(box['x'],20)
                        self.assertLessEqual(box['width'],width*0.90)
                        self.page.keyboard.press('Escape')
                        expect(opener).to_be_focused()
                    else:
                        box = sidebar.bounding_box()
                        self.assertLessEqual(box['x'],20)
                        self.assertLessEqual(box['width'],280)
                    self.page.screenshot(path=str(OUT/f'{name}-{width}.png'), full_page=True)
    def test_30_commands_theme_and_motion_preferences(self):
        self.goto('/accounts')
        sidebar = self.page.locator('aside[data-global-sidebar]')
        before = sidebar.bounding_box()['width']
        self.page.get_by_role('button',name='折叠侧栏',exact=True).click()
        self.assertLess(sidebar.bounding_box()['width'],before)
        self.page.get_by_role('button',name='展开侧栏',exact=True).click()
        self.page.get_by_role('button',name='打开命令面板',exact=True).click()
        dialog = self.page.get_by_role('dialog')
        dialog.get_by_role('combobox').fill('图片管理')
        dialog.get_by_role('option',name=re.compile('图片管理')).click()
        expect(self.page).to_have_url(re.compile('/image-manager/?$'))
        self.page.get_by_role('button',name='切换主题',exact=True).click()
        expect(self.page.locator('html')).to_have_class(re.compile('dark'))
        self.page.get_by_role('button',name='动态与外观设置',exact=True).click()
        self.page.get_by_role('radio',name='关闭动态',exact=True).check()
        expect(self.page.locator('html')).to_have_attribute('data-motion','off')
        self.page.keyboard.press('Escape')
        self.goto('/logs')
        expect(self.page.locator('html')).to_have_attribute('data-motion','off')
        expect(self.page.locator('html')).to_have_class(re.compile('dark'))
    def test_35_motion_off_switches_debug_panel_immediately(self):
        self.goto('/debug')
        self.page.get_by_role('button',name='动态与外观设置',exact=True).click()
        self.page.get_by_role('radio',name='关闭动态',exact=True).check()
        self.page.keyboard.press('Escape')
        self.page.wait_for_timeout(200)
        result = self.page.get_by_role('tab',name='搜索',exact=True).evaluate('''async el => {
          el.click();
          await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          return {motion:document.documentElement.dataset.motion,
            partial:[...document.querySelectorAll('main [style]')].map(e=>({opacity:e.style.opacity,computed:getComputedStyle(e).opacity})).filter(e=>e.opacity && Number(e.computed)<0.999),
            running:document.getAnimations().filter(a=>a.playState==='running' && a.effect?.target?.closest('main')).map(a=>a.effect.getComputedTiming().duration)};
        }''')
        self.assertEqual(result['motion'],'off')
        self.assertEqual(result['partial'],[],result)
        self.assertEqual(result['running'],[],result)
    def test_40_account_search_filters_and_safe_operations(self):
        self.goto('/accounts')
        expect(self.page.locator('.pool-stats > *')).to_have_count(6)
        rows = self.page.locator('tr[data-account-ref]')
        expect(rows).to_have_count(10)
        search = self.page.locator('.pool-list input[placeholder]').first
        search.fill('account26@example.test')
        expect(rows).to_have_count(1)
        search.fill('')
        selects = self.page.locator('.pool-list').get_by_role('combobox')
        selects.nth(0).click()
        self.page.get_by_role('option',name='Free',exact=True).click()
        selects.nth(1).click()
        self.page.get_by_role('option',name='限流',exact=True).click()
        expect(rows).to_have_count(2)
        rows.first.get_by_role('checkbox').click()
        self.page.locator('#account-batch-operations').click()
        dialog = self.page.locator('#account-operations-dialog')
        expect(dialog).to_contain_text('account21@example.test')
        expect(dialog.get_by_role('button',name='清理图像空间',exact=True)).to_be_enabled()
        dialog.get_by_role('button',name='关闭账号操作').click()
        self.assertEqual([r for r in self.requests if r['method']!='GET' and r['path']!='/auth/login'],[])
    def test_50_complete_session_json_import_is_intercepted(self):
        self.goto('/accounts')
        self.allowed_writes.add(('POST','/api/accounts'))
        self.page.get_by_role('button',name=re.compile('^导入(账户)?$')).click()
        self.page.get_by_role('button',name=re.compile('Session JSON')).click()
        dialog = self.page.get_by_role('dialog')
        dialog.locator('textarea').fill(json.dumps(dict(accessToken='synthetic-session-access',sessionToken='headersegment.encryptedkey.ivsegment.ciphertextsegment.authsegment',user={'email':'session@example.test'})))
        dialog.get_by_role('button',name=re.compile('^(导入|确认导入|导入 JSON)$')).click()
        self.page.wait_for_timeout(150)
        imported = [r for r in self.requests if r['path']=='/api/accounts' and r['method']=='POST']
        self.assertEqual(len(imported),1)
        account = imported[0]['body']['accounts'][0]
        self.assertEqual(account['access_token'],'synthetic-session-access')
        self.assertEqual(account.get('sessionToken',account.get('web_session_token')),'headersegment.encryptedkey.ivsegment.ciphertextsegment.authsegment')
        self.assertEqual(account['source_type'],'web')
    def test_60_logs_beijing_time_and_pagination(self):
        self.goto('/logs')
        rows = self.page.locator('main table').last.locator('tbody tr')
        expect(rows.first).to_contain_text('09:30:00')
        expect(rows).to_have_count(20)
        self.page.locator('main button:has(svg.lucide-chevron-right)').click()
        expect(rows.first).to_contain_text('合成调用 21')
        self.assertTrue(any(r['query'].get('page')==['2'] for r in self.requests if r['path']=='/api/logs'))
    def test_65_new_log_polling_preserves_current_reading(self):
        self.goto('/logs')
        rows = self.page.locator('main table').last.locator('tbody tr')
        expect(rows.first).to_contain_text('合成调用 1')
        self.logs.insert(0,dict(id='synthetic-new-log',time='2026-10-20 09:31:00',type='call',summary='新增合成调用',detail={'status':'success','duration_ms':1000,'urls':[]}))
        self.page.get_by_role('checkbox',name='每五秒检查新日志',exact=True).check()
        notice = self.page.get_by_role('button',name=re.compile('有 1 条新记录'))
        expect(notice).to_be_visible(timeout=7500)
        expect(rows.first).to_contain_text('合成调用 1')
        notice.click()
        expect(rows.first).to_contain_text('新增合成调用')
    def test_70_image_manager_view_and_lightbox(self):
        self.goto('/image-manager')
        self.allowed_writes.add(('POST','/api/images/management'))
        self.page.get_by_role('button',name='图片管理已启用',exact=True).click()
        expect(self.page.get_by_role('button',name='启用图片管理',exact=True)).to_have_attribute('aria-pressed','false')
        self.page.get_by_role('button',name='启用图片管理',exact=True).click()
        expect(self.page.get_by_role('button',name='图片管理已启用',exact=True)).to_have_attribute('aria-pressed','true')
        self.page.get_by_role('button',name='列表视图',exact=True).click()
        expect(self.page.locator('[data-image-view="list"]')).to_be_visible()
        self.page.get_by_role('button',name='网格视图',exact=True).click()
        expect(self.page.locator('[data-image-view="grid"]')).to_be_visible()
        self.page.get_by_role('button',name=re.compile('预览.*preview-0')).click()
        expect(self.page.get_by_role('dialog')).to_be_visible()
        self.page.keyboard.press('Escape')
        expect(self.page.get_by_role('dialog')).to_have_count(0)
    def test_80_debug_all_five_panels(self):
        self.goto('/debug')
        for title in ['搜索Skills','搜索','PPT生成','PSD生成','对话']:
            tab = self.page.get_by_role('tab',name=title,exact=True)
            tab.click()
            expect(tab).to_have_attribute('aria-selected','true')
            if title == '搜索Skills':
                expect(self.page.get_by_role('heading',name='中文安装指令',exact=True)).to_be_visible()
            elif title == '搜索':
                expect(self.page.get_by_role('textbox',name='搜索问题',exact=True)).to_be_visible()
            elif title == '对话':
                expect(self.page.get_by_role('heading',name='请求',exact=True)).to_be_visible()
            else:
                expect(self.page.get_by_role('heading',name=title,exact=True)).to_be_visible()
            self.assert_no_overflow()
    def test_90_settings_transport_save_and_unsaved_draft(self):
        self.goto('/settings')
        transport = self.page.locator('#image-transport-mode, #image-transport, [data-image-transport]').first
        expect(transport).to_be_visible()
        self.allowed_writes.add(('POST','/api/settings'))
        transport.click()
        self.page.get_by_role('option',name=re.compile('语音模式|Voice',re.I)).click()
        self.page.get_by_role('button',name=re.compile('^保存(配置|设置)?$')).first.click()
        self.page.wait_for_timeout(150)
        writes = [r for r in self.requests if r['path']=='/api/settings' and r['method']=='POST']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0]['body']['image_transport'],'voice')
        self.page.locator('main input[placeholder="https://example.com"]').fill('https://draft.example.test')
        self.page.locator('aside[data-global-sidebar] nav a[href="/image"], aside[data-global-sidebar] nav a[href="/image/"]').click()
        dialog = self.page.get_by_role('dialog')
        expect(dialog).to_contain_text(re.compile('未保存'))
        dialog.get_by_role('button',name=re.compile('继续编辑|留在此页')).click()
        expect(self.page).to_have_url(re.compile('/settings/?$'))
        self.assertEqual(len([r for r in self.requests if r['path']=='/api/settings' and r['method']=='POST']),1)
        # 撤销本地草稿后进行干净 SPA 重入，不额外提交任何设置写入。
        self.page.get_by_role('button',name='撤销更改',exact=True).click()
        reset=self.page.get_by_role('dialog')
        reset.get_by_role('button',name='撤销更改',exact=True).click()
        expect(self.page.locator('main input[placeholder="https://example.com"]')).to_have_value(self.settings['base_url'])
        self.page.locator('aside[data-global-sidebar] nav a[href="/image"], aside[data-global-sidebar] nav a[href="/image/"]').click()
        expect(self.page).to_have_url(re.compile('/image/?$'))
        self.settings['base_url']='https://external-update.example.test'
        before_reads=len([r for r in self.requests if r['path']=='/api/settings' and r['method']=='GET'])
        self.page.locator('aside[data-global-sidebar] nav a[href="/settings"], aside[data-global-sidebar] nav a[href="/settings/"]').click()
        expect(self.page).to_have_url(re.compile('/settings/?$'))
        expect(self.page.locator('main input[placeholder="https://example.com"]')).to_have_value('https://external-update.example.test')
        self.assertGreater(len([r for r in self.requests if r['path']=='/api/settings' and r['method']=='GET']),before_reads)
        self.assertEqual(len([r for r in self.requests if r['path']=='/api/settings' and r['method']=='POST']),1)

    def test_95_user_navigation_preserves_existing_access(self):
        page = self.new_page(role='user')
        expect(page).to_have_url(re.compile('/image/?$'))
        sidebar = page.locator('aside[data-global-sidebar]')
        expect(sidebar).to_have_count(1)
        expect(sidebar.locator('nav a[href="/image"], nav a[href="/image/"]')).to_have_count(1)
        for path,_ in ROUTES[1:]:
            expect(sidebar.locator(f'nav a[href="{path}"], nav a[href="{path}/"]')).to_have_count(0)

    def test_96_task_center_status_order_and_deep_links(self):
        created='2026-10-20T01:00:00Z'
        self.image_tasks=[dict(id='a'*32,status='success',mode='generate',model='gpt-image-2',created_at=created,updated_at='2026-10-20T01:03:00Z',data=[{'url':IMAGES[0]['url']}]),
            dict(id='b'*32,status='running',mode='generate',model='gpt-image-2',created_at=created,updated_at=created,data=[])]
        self.file_tasks=[dict(id='c'*32,kind='ppt',status='partial',prompt_preview='合成PPT任务',created_at=created,updated_at='2026-10-20T01:05:00Z'),
            dict(id='d'*32,kind='psd',status='ready',prompt_preview='合成PSD任务',created_at=created,updated_at='2026-10-20T01:04:00Z')]
        self.goto('/accounts')
        self.page.get_by_role('button',name='打开任务中心',exact=True).click()
        entries=self.page.locator('.glass-task-entry')
        expect(entries).to_have_count(4)
        expect(entries.first).to_contain_text('b'*12)
        expect(entries.nth(1)).to_contain_text('c'*12)
        expect(entries.nth(1).locator('.glass-task-state')).to_have_class(re.compile('warning'))
        expect(entries.nth(1)).to_contain_text('部分完成')
        expect(entries.nth(2).locator('.glass-task-state')).to_have_class(re.compile('ready'))
        expect(entries.nth(2)).to_contain_text('等待确认')
        self.page.screenshot(path=str(OUT/'任务中心-desktop.png'),full_page=True)
        entries.filter(has_text='a'*12).get_by_role('button',name='查看',exact=True).click()
        expect(self.page).to_have_url(re.compile(r'/image/?\?task_id='+'a'*32))
        expect(self.page.locator('[data-linked-image-task="'+'a'*32+'"]')).to_be_visible()
        for kind, task_id, title in [('ppt','c'*32,'PPT生成'),('psd','d'*32,'PSD生成')]:
            self.page.get_by_role('button',name='打开任务中心',exact=True).click()
            self.page.locator('.glass-task-entry').filter(has_text=task_id[:12]).get_by_role('button',name='查看',exact=True).click()
            expect(self.page).to_have_url(re.compile(r'/debug/?\?tab='+kind+'&task_id='+task_id))
            expect(self.page.get_by_role('tab',name=title,exact=True)).to_have_attribute('aria-selected','true')
            expect(self.page.locator('[data-file-task-id="'+task_id+'"]')).to_be_visible()
            expect(self.page.locator('main textarea').first).to_have_value('合成'+kind.upper()+'任务')
        self.assertEqual([r for r in self.requests if r['method']!='GET' and r['path']!='/auth/login'],[])
    def test_97_all_48_component_examples_and_real_interactions(self):
        self.goto('/accounts')
        self.page.get_by_role('button',name='动态与外观设置',exact=True).click()
        self.page.get_by_role('button',name=re.compile(r'查看\s*48\s*项动态组件')).click()
        expect(self.page).to_have_url(re.compile('/settings/components-demo/?$'))
        expect(self.page.locator('aside[data-global-sidebar] nav [aria-current="page"]')).to_contain_text('设置')
        examples=set()
        groups=[('A','导航与框架'),('B','输入与选择'),('C','浮层与操作'),('D','列表与数据'),('E','图片与操控'),('F','任务与反馈')]
        for key,title in groups:
            self.page.get_by_role('tab',name=title,exact=True).click()
            cards=self.page.locator('[data-component-example]')
            expect(cards).to_have_count(8)
            values=cards.evaluate_all('(nodes)=>nodes.map(n=>n.dataset.componentExample)')
            self.assertEqual(values,[key+str(i).zfill(2) for i in range(1,9)])
            examples.update(values)
            self.page.screenshot(path=str(OUT/f'动态组件-{key}.png'),full_page=True)
        self.assertEqual(len(examples),48)
        self.page.get_by_role('tab',name='输入与选择',exact=True).click()
        switch=self.page.locator('[data-component-example="B06"]').get_by_role('switch')
        expect(switch).not_to_be_checked()
        switch.check()
        expect(switch).to_be_checked()
        self.page.get_by_role('tab',name='浮层与操作',exact=True).click()
        self.page.locator('[data-component-example="C05"]').get_by_role('button',name='确认操作',exact=True).click()
        dialog=self.page.get_by_role('dialog')
        expect(dialog).to_contain_text('这是独立的组件演示，不会提交业务操作。')
        dialog.get_by_role('button',name='完成',exact=True).click()
        expect(dialog).to_have_count(0)
        self.page.set_viewport_size({'width':375,'height':812})
        self.assert_no_overflow()
        self.page.screenshot(path=str(OUT/'动态组件-mobile.png'),full_page=True)
        self.assertEqual([r for r in self.requests if r['method']!='GET' and r['path']!='/auth/login'],[])

if __name__ == '__main__':
    unittest.main(verbosity=2)
