# -*- coding: utf-8 -*-
"""政策管理后台页面（/admin）测试：静态页面契约 + Flask 路由冒烟。

本测试不连数据库、不启动任何 worker：app.create_app 的重依赖
（config/models/api/authentication/conversations/chain_trace/policy_core）
全部用内存桩替换，只验证新增 /admin 路由返回静态页面与 no-store 响应头，
以及 src/static/admin.html、src/static/admin.js 的关键契约（区块、接口、CSRF、安全文案）。
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from flask import Blueprint

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, 'src', 'static')
ADMIN_HTML = os.path.join(STATIC_DIR, 'admin.html')
ADMIN_JS = os.path.join(STATIC_DIR, 'admin.js')


def _read(path):
    with open(path, encoding='utf8') as handle:
        return handle.read()


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _node_bin():
    """node 不保证在 PATH（本机 nvm 安装），按常见位置兜底，找不到则跳过语法检查。"""
    candidates = [
        os.environ.get('NODE_BIN'),
        shutil.which('node'),
        '/home/adminroot/.nvm/versions/node/v24.22.0/bin/node',
        '/usr/local/bin/node',
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ''


class AdminPageStaticTest(unittest.TestCase):
    """静态契约：页面与脚本存在、三大区块与全部已落地接口都被引用。"""

    def setUp(self):
        self.html = _read(ADMIN_HTML)
        self.js = _read(ADMIN_JS)

    def test_admin_html_has_three_sections_and_controls(self):
        for token in (
            'id="login-view"', 'id="app-view"',
            'id="search-card"', 'id="lifecycle-card"', 'id="import-card"',
            'id="flag-lifecycle"', 'id="flag-import"',
            'id="policy-rows"', 'id="drawer-mask"', 'id="action-mask"',
            'id="import-report"', 'id="import-batch"',
            '/admin.js',
        ):
            self.assertIn(token, self.html, token)
        for action in ('suspend', 'archive', 'restore', 'delete', 'publish', 'rollback'):
            self.assertIn('data-action="%s"' % action, self.html)

    def test_admin_html_depends_on_nothing_external(self):
        # 不引 auth.js：其全局 fetch 包装会把 401 跳转到 /ask，劫持本页"重新登录"提示；
        # 也不引任何 CDN/外链资源，政务内网可用。
        self.assertNotIn('/auth.js', self.html)
        self.assertNotIn('src="http', self.html)
        self.assertNotIn('<link', self.html)
        self.assertNotIn('cdn', self.html.lower())

    def test_admin_js_covers_landed_apis(self):
        for token in (
            '/api/me', '/api/login', '/api/logout',
            '/api/policies?', '/api/policies/filters',
            '/impact', '/versions',
            'suspend', 'archive', 'restore', 'delete', 'publish', 'rollback',
            '/api/policies/import/preview',
            '/api/policies/import',
            '/commit', '/rollback',
            '/api/policies/__probe__/impact',
            '/api/policies/import/__probe__',
            'policy_lifecycle_disabled', 'policy_import_disabled',
        ):
            self.assertIn(token, self.js, token)

    def test_admin_js_constructs_all_landed_routes(self):
        """6 条生命周期命令与 5 条导入路由都在脚本里真实拼出，不是只写在注释里。"""
        self.assertIn(
            "'/api/policies/' + encodeURIComponent(action.item.policy_code) + '/' + action.name",
            self.js)
        for token in ("'/api/policies/' + code + '/impact'",
                      "'/api/policies/' + code + '/versions'",
                      "'/api/policies/import/preview'",
                      "'/api/policies/import'",
                      "'/api/policies/import/' + encodeURIComponent(id)",
                      "'/api/policies/import/' + encodeURIComponent(id) + '/commit'",
                      "'/api/policies/import/' + encodeURIComponent(id) + '/rollback'"):
            self.assertIn(token, self.js, token)

    def test_admin_js_auth_and_csrf_follow_existing_cookie_contract(self):
        self.assertIn('helpdesk_csrf', self.js)
        self.assertIn('X-CSRF-Token', self.js)
        self.assertIn("credentials: 'same-origin'", self.js)
        # 不把令牌落到 localStorage：JWT 始终由 HttpOnly cookie 持有。
        self.assertNotIn('localStorage', self.js)

    def test_page_load_path_does_not_write(self):
        """页面初始化只允许 GET：init 函数体内不得出现 POST。"""
        start = self.js.index('async function init()')
        end = self.js.index("if (document.readyState")
        init_body = self.js[start:end]
        self.assertNotIn("method: 'POST'", init_body)
        self.assertIn('detectFlags', init_body)
        self.assertIn('loadPolicies', init_body)

    def test_admin_js_error_and_double_confirm_copy(self):
        for token in ('该操作需要二次确认', '再次确认执行', '确认提交', '确认回滚',
                      '登录已失效，请重新登录', '请求超时，请稍后重试'):
            self.assertIn(token, self.js, token)

    @unittest.skipUnless(_node_bin(), 'node 不可用，跳过 JS 语法检查')
    def test_admin_js_passes_node_syntax_check(self):
        proc = subprocess.run(
            [_node_bin(), '--check', ADMIN_JS],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class AdminRouteTest(unittest.TestCase):
    """真实 Flask 路由栈：/admin 返回页面，/admin.js 由静态路由提供，均不碰库。"""

    def setUp(self):
        self.upload_dir = tempfile.mkdtemp(prefix='admin_page_')
        self.addCleanup(shutil.rmtree, self.upload_dir, ignore_errors=True)

    def _fakes(self):
        config = _module(
            'config',
            DATABASE_URL='sqlite://',
            SECRET_KEY='test-secret',
            UPLOAD_DIR=self.upload_dir,
            MAX_CONTENT_LENGTH=1024 * 1024,
            KG_GRAPH_ENABLED=False,
            KG_AUTO_UPDATE=False,
            AUTH_DATABASE_URL='sqlite://',
            AUTH_JWT_SECRET='x' * 32,
        )
        policy_core = _module('policy_core', enabled=mock.Mock(return_value=False))
        policy_core.showcase = _module('policy_core.showcase', enabled=mock.Mock(return_value=False))
        policy_core.full_settings = _module('policy_core.full_settings', enabled=mock.Mock(return_value=False))
        policy_core.routes = _module('policy_core.routes', bp=Blueprint('core', __name__))
        policy_core.policy_worker = _module('policy_core.policy_worker', start=mock.Mock())
        policy_core.publishing = _module('policy_core.publishing', start=mock.Mock())
        db = mock.Mock(name='db')
        return {
            'config': config,
            'models': _module('models', db=db),
            'api': _module('api', bp=Blueprint('api', __name__)),
            'authentication': _module('authentication', install=mock.Mock()),
            'conversations': _module('conversations', chat_bp=Blueprint('chat', __name__)),
            'chain_trace': _module('chain_trace', install=mock.Mock()),
            'policy_core': policy_core,
            'policy_core.showcase': policy_core.showcase,
            'policy_core.full_settings': policy_core.full_settings,
            'policy_core.routes': policy_core.routes,
            'policy_core.policy_worker': policy_core.policy_worker,
            'policy_core.publishing': policy_core.publishing,
        }, db

    def _load_app(self):
        fakes, db = self._fakes()
        with mock.patch.dict(sys.modules, fakes):
            sys.modules.pop('app', None)
            module = importlib.import_module('app')
        self.addCleanup(sys.modules.pop, 'app', None)
        return module, fakes, db

    def test_admin_route_serves_page_with_no_store(self):
        module, _fakes, _db = self._load_app()
        client = module.app.test_client()
        resp = client.get('/admin')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.mimetype.startswith('text/html'), resp.mimetype)
        self.assertIn('no-store', resp.headers.get('Cache-Control', ''))
        self.assertEqual(resp.headers.get('Pragma'), 'no-cache')
        self.assertEqual(resp.headers.get('Expires'), '0')
        body = resp.get_data(as_text=True)
        self.assertIn('政策管理后台', body)
        self.assertIn('id="app-view"', body)
        self.assertIn('/admin.js', body)

    def test_admin_js_served_by_flask_static_route(self):
        module, _fakes, _db = self._load_app()
        client = module.app.test_client()
        resp = client.get('/admin.js')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('javascript', resp.mimetype.lower())
        body = resp.get_data(as_text=True)
        self.assertIn('/api/policies/import/preview', body)
        self.assertIn('helpdesk_csrf', body)

    def test_admin_route_render_does_not_touch_database(self):
        module, _fakes, db = self._load_app()
        db.reset_mock()
        resp = module.app.test_client().get('/admin')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.mock_calls, [])

    def test_existing_index_route_still_works(self):
        module, _fakes, _db = self._load_app()
        resp = module.app.test_client().get('/')
        self.assertEqual(resp.status_code, 200)


if __name__ == '__main__':
    unittest.main()
