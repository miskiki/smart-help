# -*- coding: utf-8 -*-
"""admin.js 模型段的契约护栏（字符串/结构断言，不是行为测试）。

纯 DOM 无打包框架，真正的交互行为靠浏览器冒烟覆盖；这里只钉住几条
一旦回退就会造成数据损坏或体验断裂的约定：
1. Tab 切换只做本地过滤，不发请求（旧实现每次 change 都 GET 一次列表）。
2. 编辑态的模型表单 payload 绝不含 api_key（凭证走 /credentials 子资源）。
3. 凭证三态卡的三态样式与两个子资源调用都在。
4. 旧 DOM id 不得复活（Task 3 已把弹窗换成抽屉）。
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / 'src' / 'static' / 'admin.js').read_text(encoding='utf8')


def _fn_body(name, src=JS):
    """抓 `function <name>(...) {` 到下一个顶格的 `  }`（两空格缩进闭合）。"""
    m = re.search(r'\n  (?:async )?function ' + re.escape(name) + r'\([^)]*\) \{\n(.*?)\n  \}\n',
                  src, re.S)
    return m.group(1) if m else None


class TestAdminJsModelContract(unittest.TestCase):
    def test_tab_switch_does_not_request(self):
        body = _fn_body('switchModelTab')
        self.assertIsNotNone(body, 'switchModelTab 不存在')
        self.assertNotIn('request(', body)
        self.assertIn('renderModels(', body)

    def test_edit_mode_never_sends_api_key(self):
        body = _fn_body('saveModel')
        self.assertIsNotNone(body, 'saveModel 不存在')
        # 唯一允许出现 api_key 赋值的位置必须被 modelEditingId 取反守卫
        assigns = [ln for ln in body.splitlines() if 'payload.api_key' in ln]
        self.assertEqual(len(assigns), 1, 'payload.api_key 赋值应只有新建态一处：%r' % assigns)
        self.assertIn('!modelEditingId', assigns[0])
        self.assertIn('pendingApiKey', assigns[0])

    def test_credential_is_its_own_subresource(self):
        body = _fn_body('saveCredential')
        self.assertIsNotNone(body, 'saveCredential 不存在')
        self.assertIn("'/credentials'", body)
        self.assertIn("method: 'PUT'", body)
        rm = _fn_body('removeCredential')
        self.assertIsNotNone(rm, 'removeCredential 不存在')
        self.assertIn("'/credentials'", rm)
        self.assertIn("method: 'DELETE'", rm)

    def test_credential_card_has_three_states(self):
        body = _fn_body('renderCredentialCard')
        self.assertIsNotNone(body, 'renderCredentialCard 不存在')
        for cls in ('cred-state--configured', 'cred-state--unconfigured', 'cred-state--editing'):
            self.assertIn(cls, body)

    def test_type_select_locked_in_edit_mode(self):
        body = _fn_body('openModelDrawer')
        self.assertIsNotNone(body, 'openModelDrawer 不存在')
        self.assertIn("$('mf-type').disabled = !!model", body)

    def test_old_dom_ids_are_gone(self):
        for old in ('model-add-btn', 'model-refresh-btn', 'model-type-filter',
                    'models-list', 'model-mask'):
            self.assertNotIn(old, JS, '旧 id 残留：%s' % old)

    def test_test_draft_calls_endpoint(self):
        body = _fn_body('testDraft')
        self.assertIsNotNone(body, 'testDraft 不存在')
        self.assertIn("'/api/admin/models/test-draft'", body)
        self.assertIn("method: 'POST'", body)
        # 编辑态带 id 让后端用已存密钥兜底（明文永不回显）
        self.assertIn('body.id = modelEditingId', body)


class TestAdminHtmlModelDrawer(unittest.TestCase):
    """Task 4 把字段填回 #model-form-body；骨架 id 仍由 test_admin_model_dom 守住。"""

    @classmethod
    def setUpClass(cls):
        cls.HTML = (ROOT / 'src' / 'static' / 'admin.html').read_text(encoding='utf8')
        m = re.search(r'<div id="model-form-body">(.*?)\n    </div>', cls.HTML, re.S)
        cls.BODY = m.group(1) if m else ''

    def test_fields_inside_drawer_body(self):
        self.assertIn('model-form-body', self.HTML)
        for fid in ('mf-name', 'mf-type', 'mf-provider', 'mf-base-url', 'mf-model-name',
                    'mf-dim-row', 'mf-query-instruction', 'mf-json-mode', 'model-form-error'):
            self.assertIn('id="%s"' % fid, self.BODY, '抽屉缺字段 %s' % fid)

    def test_credential_box_is_placeholder_not_input(self):
        self.assertIn('id="model-cred-box"', self.BODY)
        # 旧实现把 mf-api-key 写死在 HTML 里；现在由凭证卡按状态渲染
        self.assertNotIn('id="mf-api-key"', self.BODY)

    def test_no_old_modal_left(self):
        self.assertNotIn('id="model-mask"', self.HTML)
        self.assertNotIn('id="models-list"', self.HTML)


class TestReviewRegressionFixes(unittest.TestCase):
    """最终评审提的几条 high/medium：失败原因看不见、默认删不掉、遮罩缺失、明文残留。"""

    def test_request_surfaces_backend_detail(self):
        """后端 400/200-ok:false 的 detail 必须进 message，否则用户只见"请求失败"。"""
        self.assertIn('data.detail', JS)
        m = re.search(r"let message = \(data && \(.*?\)\)", JS)
        self.assertIsNotNone(m)
        self.assertIn('data.detail', m.group(0))

    def test_test_draft_renders_inline_result(self):
        body = _fn_body('testDraft')
        self.assertIsNotNone(body)
        self.assertIn("$('save-test-result')", body)
        self.assertIn('err.data', body)          # 失败也要拿到 detail
        self.assertIn('404', body)               # 旧服务端兜底

    def test_default_model_keeps_delete_button(self):
        body = _fn_body('modelCardActions')
        self.assertIsNotNone(body)
        # 删除按钮不再被 !m.is_default 包住：block 里只剩"设为默认"
        block = re.search(r"if \(!m\.is_default\) \{(.*?)\n    \}", body, re.S)
        self.assertIsNotNone(block)
        self.assertNotIn('mop="delete"', block.group(1))
        self.assertIn('data-default', body)

    def test_drawer_closes_on_mask_click_and_clears_plaintext(self):
        self.assertIn("$('model-drawer').addEventListener('click'", JS)
        body = _fn_body('clearCredDraft')
        self.assertIsNotNone(body, 'clearCredDraft 不存在')
        self.assertIn("pendingApiKey = ''", body)
        self.assertIn("model-cred-input", body)
        # 三个关闭点都要清
        self.assertIn('clearCredDraft', _fn_body('saveModel'))

    def test_drawer_rows_follow_type(self):
        body = _fn_body('syncDrawerRows')
        self.assertIsNotNone(body)
        self.assertIn("'mf-query-row'", body)
        self.assertIn("'mf-json-row'", body)


if __name__ == '__main__':
    unittest.main()
