"""使用合成账号验证号池布局和现有操作契约；所有 API 请求均在浏览器内拦截。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import unittest
from urllib.parse import urlsplit

try:
    from playwright.sync_api import expect, sync_playwright
except ModuleNotFoundError:
    expect = sync_playwright = None

BASE = os.environ.get('BASE_URL', 'http://127.0.0.1:18219').rstrip('/')
OUT = Path(os.environ.get('SCREENSHOT_DIR', '/verification'))
CHROME = os.environ.get('CHROMIUM_PATH', '/ms-playwright/chromium-1243/chrome-linux64/chrome')
MODELS = ['auto', 'gpt-5.3-mini', 'gpt-5.6', 'gpt-5.6-mini', 'gpt-6', 'gpt-6-mini', 'gpt-image-2', 'gpt-image-2.5']
ACCOUNTS = [dict(access_token=f'example-access-{i:03d}', account_ref=f'{i+1:064x}',
    email=f'account{i+1}@example.test', source_type='codex', type='edu_plus' if i < 13 else 'Free',
    status='正常' if i < 20 else ['限流', '限流', '异常', '异常', '禁用', '禁用'][i-20],
    quota=120, success=i, fail=i % 3, image_inflight=0, proxy='', created_at='2026-10-15 03:48:00',
    has_refresh_token=True, auto_refresh_token=True, image_cleanup_enabled=False,
    access_token_expires_at='2026-11-01T00:30:00Z') for i in range(26)]


@unittest.skipUnless(sync_playwright is not None and os.environ.get('BASE_URL'),
    '浏览器回归需要 Playwright 和显式 BASE_URL')
class AccountPoolLayoutTests(unittest.TestCase):
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
        self.contexts = []
        self.script_errors = []
        self.mutations = []
        self.page = self.new_page()

    def tearDown(self):
        for context in self.contexts:
            context.close()
        self.assertEqual(self.script_errors, [], '浏览器不得出现未捕获的脚本异常')
        self.assertEqual(self.mutations, [], '布局回归不应提交任何账号写入或上游操作')

    def new_page(self, empty=False, width=1672, height=1000):
        context = self.browser.new_context(viewport={'width':width, 'height':height},
            color_scheme='light', timezone_id='America/Los_Angeles')
        self.contexts.append(context)
        context.add_init_script("localStorage.setItem('chatgpt2api-theme', 'light')")
        accounts = [] if empty else ACCOUNTS

        def handle(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.netloc != urlsplit(BASE).netloc:
                return route.abort()
            path = parsed.path.rstrip('/')
            if path.startswith(('/api/', '/auth/')) and request.method not in ('GET', 'HEAD') and path != '/auth/login':
                self.mutations.append(f'{request.method} {path}')
                return route.fulfill(status=400, json={'detail':{'error':'fixture 禁止写入'}})
            if path == '/auth/login':
                return route.fulfill(json={'role':'admin', 'subject_id':'admin', 'name':'管理员', 'valid':True, 'ok':True})
            if path == '/api/accounts':
                return route.fulfill(json={'items':accounts})
            if path == '/api/accounts/operations':
                return route.fulfill(json={'items':[dict(id=a['account_ref'], label=a['email'], supported=True,
                    expires_at=a['access_token_expires_at'], has_refresh_token=True, auto_refresh_token=True,
                    image_cleanup_enabled=False, status=a['status']) for a in accounts]})
            if path == '/v1/models':
                return route.fulfill(json={'object':'list', 'data':[{'id':m,'object':'model'} for m in MODELS]})
            if path == '/api/settings':
                return route.fulfill(json={'config':{}})
            if path == '/api/cpa/pools':
                return route.fulfill(json={'pools':[]})
            if path == '/api/sub2api/servers':
                return route.fulfill(json={'servers':[]})
            if path == '/version':
                return route.fulfill(json={'version':'1.7.0'})
            if path.startswith(('/api/', '/auth/')):
                return route.fulfill(json={})
            return route.continue_()

        context.route('**/*', handle)
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on('pageerror', lambda error: self.script_errors.append(str(error)))
        page.goto(BASE + '/login')
        page.locator('input[type="password"]').fill('example-preview-key')
        page.get_by_role('button', name='登录', exact=True).click()
        # 等待登录持久化和页面跳转完成，避免直达账号页与 IndexedDB 写入竞争。
        expect(page).not_to_have_url(re.compile('/login/?$'))
        page.goto(BASE + '/accounts')
        expect(page.locator('h1').filter(has_text='号池管理')).to_be_visible()
        if not empty:
            expect(page.locator('tr[data-account-ref]')).to_have_count(10)
            expect(page.get_by_role('button', name='操作', exact=True).first).to_be_visible()
        return page

    def expect_layout(self, page):
        expect(page.locator('body')).to_have_attribute('data-account-pool-layout', re.compile('.*'))
        expect(page.locator('.pool-stats > *')).to_have_count(6)
        expect(page.locator('nav.pool-sidebar')).to_have_count(1)
        expect(page.locator('.pool-table-scroll > table')).to_have_count(1)
        expect(page.locator('section.pool-list')).to_have_count(1)

    def test_00_layout_direct_entry(self):
        self.expect_layout(self.page)
        expect(self.page.locator('.pool-sidebar a[href="/accounts"], .pool-sidebar a[href="/accounts/"]')).to_be_visible()
        expect(self.page.locator('.pool-stats')).to_contain_text('26')
        self.assertEqual(self.page.locator('.pool-sidebar a').count(), 6)
        self.assertEqual(self.page.locator('tr[data-account-ref]').count(), 10)

    def test_10_search_type_and_status_filters(self):
        page = self.page
        self.expect_layout(page)
        search = page.locator('.pool-list input[placeholder]').first
        search.fill('account26@example.test')
        expect(page.locator('tr[data-account-ref]')).to_have_count(1)
        expect(page.locator('tr[data-account-ref]')).to_contain_text('account26@example.test')
        search.fill('no-matching-account@example.test')
        expect(page.locator('tr[data-account-ref]')).to_have_count(0)
        search.fill('')
        expect(page.locator('tr[data-account-ref]')).to_have_count(10)
        selects = page.locator('.pool-list').get_by_role('combobox')
        selects.nth(0).click()
        page.get_by_role('option', name='Free', exact=True).click()
        expect(page.locator('tr[data-account-ref]').first).to_contain_text('account14@example.test')
        selects.nth(1).click()
        page.get_by_role('option', name='限流', exact=True).click()
        expect(page.locator('tr[data-account-ref]')).to_have_count(2)
        for row in page.locator('tr[data-account-ref]').all():
            expect(row).to_contain_text('Free')
            expect(row).to_contain_text('限流')
        selects.nth(0).click()
        page.get_by_role('option', name='全部类型', exact=True).click()
        selects.nth(1).click()
        page.get_by_role('option', name='全部状态', exact=True).click()
        expect(page.locator('tr[data-account-ref]')).to_have_count(10)

    def test_20_pagination_selection_and_existing_dialogs(self):
        page = self.page
        self.expect_layout(page)
        rows = page.locator('tr[data-account-ref]')
        rows.first.get_by_role('checkbox').click()
        expect(page.locator('#account-batch-operations')).to_contain_text('已选 1')
        page.locator('.pool-list').get_by_role('button', name='2', exact=True).click()
        expect(rows.first).to_contain_text('account11@example.test')
        rows.first.get_by_role('checkbox').click()
        expect(page.locator('#account-batch-operations')).to_contain_text('已选 2')
        page.locator('#account-batch-operations').click()
        dialog = page.locator('#account-operations-dialog')
        expect(dialog).to_be_visible()
        expect(dialog.locator('.account-op-summary')).to_have_count(2)
        expect(dialog).to_contain_text('account1@example.test')
        expect(dialog).to_contain_text('account11@example.test')
        dialog.get_by_role('button', name='关闭账号操作').click()
        page.locator('#account-select-all').click()
        expect(page.locator('#account-batch-operations')).to_contain_text('已选 26')
        page.locator('#account-select-all').click()
        expect(page.locator('#account-batch-operations')).to_contain_text('已选 0')
        expect(page.locator('#account-batch-operations')).to_be_disabled()
        page.locator('.pool-list').get_by_role('button', name='1', exact=True).click()
        rows.first.get_by_role('button', name='操作', exact=True).click()
        expect(dialog).to_be_visible()
        expect(dialog.locator('.account-op-summary')).to_have_count(1)
        expect(dialog).to_contain_text('account1@example.test')
        expect(dialog.get_by_role('button', name='清理图像空间', exact=True)).to_be_enabled()
        dialog.get_by_role('button', name='关闭账号操作').click()
        rows.first.locator('td').last.locator('button').first.click()
        expect(page.get_by_role('heading', name='编辑账户', exact=True)).to_be_visible()
        page.get_by_role('button', name='取消', exact=True).click()
        expect(page.get_by_role('heading', name='编辑账户', exact=True)).to_have_count(0)

    def test_30_spa_navigation_deactivates_and_restores_layout(self):
        page = self.page
        self.expect_layout(page)
        page.evaluate('window.__poolLayoutNavigationSentinel = "same-document"')
        page.locator('.pool-sidebar a[href="/settings"], .pool-sidebar a[href="/settings/"]').click()
        expect(page).to_have_url(re.compile('/settings/?$'))
        expect(page.locator('body')).not_to_have_attribute('data-account-pool-layout', re.compile('.*'))
        # 扩展保留原节点上的类；路由标记解除后应恢复普通顶部导航。
        self.assertEqual(page.locator('header nav').evaluate('(el) => getComputedStyle(el).position'), 'static')
        expect(page.locator('nav [aria-current="page"]')).to_have_count(0)
        expect(page.locator('nav .pool-nav-icon:visible')).to_have_count(0)
        self.assertEqual(page.evaluate('window.__poolLayoutNavigationSentinel'), 'same-document')
        page.locator('nav a[href="/accounts"], nav a[href="/accounts/"]').click()
        expect(page).to_have_url(re.compile('/accounts/?$'))
        self.expect_layout(page)
        expect(page.locator('tr[data-account-ref]')).to_have_count(10)
        expect(page.locator('#account-select-all')).to_have_count(1)
        expect(page.locator('#account-batch-operations')).to_have_count(1)
        self.assertEqual(page.evaluate('window.__poolLayoutNavigationSentinel'), 'same-document')

    def test_40_responsive_overflow_and_screenshots(self):
        page = self.page
        for width, height, name in [(1672,1000,'desktop'), (768,1024,'tablet'), (375,812,'mobile')]:
            with self.subTest(width=width):
                page.set_viewport_size({'width':width, 'height':height})
                self.expect_layout(page)
                page.wait_for_timeout(250)
                dimensions = page.evaluate('''() => ({client:document.documentElement.clientWidth,
                    root:document.documentElement.scrollWidth, body:document.body.scrollWidth,
                    table:document.querySelector('.pool-table-scroll').getBoundingClientRect().width,
                    sidebar:document.querySelector('.pool-sidebar').getBoundingClientRect().width})''')
                self.assertLessEqual(dimensions['root'], dimensions['client'] + 1, dimensions)
                self.assertLessEqual(dimensions['body'], dimensions['client'] + 1, dimensions)
                self.assertLessEqual(dimensions['table'], width + 1, dimensions)
                expect(page.locator('.pool-list input[placeholder]').first).to_be_visible()
                page.screenshot(path=str(OUT / (name + '.png')), full_page=True)

    def test_50_empty_accounts(self):
        page = self.new_page(empty=True)
        self.expect_layout(page)
        expect(page.locator('tr[data-account-ref]')).to_have_count(0)
        expect(page.locator('#account-batch-operations')).to_be_disabled()
        expect(page.locator('#account-select-all')).to_be_disabled()
        self.assertFalse(page.locator('.pool-list').inner_text().strip() == '')
        page.set_viewport_size({'width':375, 'height':812})
        expect(page.locator('.pool-list')).to_be_visible()
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 376)


if __name__ == '__main__':
    unittest.main(verbosity=2)
