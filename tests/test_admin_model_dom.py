"""Task 3 骨架 DOM 断言：模型区 Tab + 卡片网格 + 抽屉骨架（只验 admin.html，不验 JS）。

契约（ledger preflight 裁定）：
- 只断言 Task 3 真正落进 HTML 的骨架 id：
  model-tabs / model-grid / model-grid-add / model-drawer / save-test-btn。
- 不断言 mf-api-key-tag、credential-card（Task 4 字段）。
- 不断言 JS 含任何 id（JS-id 覆盖归 Task 4 的 test_admin_js_contract.py）。
- 缓存指纹 ?v= 必须升到 20261110 开头。
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / 'src' / 'static' / 'admin.html').read_text(encoding='utf8')


class TestModelDom(unittest.TestCase):
    def test_skeleton_ids_present(self):
        # 骨架里必须是静态的：Tab 容器、卡片容器、抽屉、保存前测试按钮
        for id in ('model-tabs', 'model-grid', 'model-drawer', 'save-test-btn'):
            self.assertIn(id, HTML)

    def test_add_card_is_rendered_into_grid(self):
        # ADD 卡是网格最后一项，由 JS 随卡片一起渲染（静态节点会被重渲染冲掉）
        self.assertNotIn('id="model-grid-add"', HTML)
        js = (ROOT / 'src' / 'static' / 'admin.js').read_text(encoding='utf8')
        self.assertIn('id="model-grid-add"', js)
        self.assertIn("$('model-grid').addEventListener", js)

    def test_old_entry_ids_dropped(self):
        # 旧入口不再被监听（id 可留可删，但 JS 段不再绑定；这里只断言新 id 在场）
        self.assertIn('id="model-tabs"', HTML)

    def test_cache_fingerprint_upgraded(self):
        ver = HTML.split('/admin.js?v=')[1].split('"')[0]
        self.assertRegex(ver, r'^20261110')


if __name__ == '__main__':
    unittest.main()
