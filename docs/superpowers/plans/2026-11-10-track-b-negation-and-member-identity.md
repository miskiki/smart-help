# Track B（否定语义与成员身份）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户明说的「无残疾」真正落成成员事实（布尔 `disabled=false`），并让「母亲李某／我本人」这类称谓被绑定到具体成员，使追问能点名而不是「成员1」。

**Architecture:** 三处改动，全在后端事实层与追问文案层，不动前端、不动规则库。①把「健康≠无残疾」与「明说无残疾必须记 `False`」这两条相反要求同时写进抽取提示词（现在只有前一条，把后者也压掉了）；②扩展成员姓名/称谓发现，产出可渲染的成员标签；③用一次离线端到端见证证明「说了无残疾就再也不问等级」。

**Tech Stack:** Python 3.10 / Flask / unittest（`python -m unittest discover -s tests`）/ 本地 Qwen3.6-35B（仅复现脚本用，测试全部离线）

**Spec:** `docs/superpowers/specs/2026-11-10-model-admin-ui-and-negation-design.md` §8（实证修订）；本节之前的 §3.2 已被推翻，只作历史推理参考

## Global Constraints

- 全量回归命令：`PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`，当前基线 **1018 OK**，每个 Task 结束必须全绿。
- 不新增运行时开关；不新增环境变量。
- 不改 `src/policy_core/reasoning.py:951` 那条「已验证的明确肯定否定必须保留为 conflicts」的行为。
- 不改 `src/household_facts.py`（`full_model` 生产路径不经过它）；不改 `src/policy_core/rule_flow.py`（39 条规则里 0 条引用 `disability_grade`，无证据支持改动）。
- 提示词里凡是面向模型的句子，必须同时保留「禁止从句」与「允许从句」，不允许只写一半。

## Review Focus

1. **用户说「我们都很健康」**：不得据此输出 `disabled=false`——健康不等于否认残疾。测试见 Task 1。
2. **用户说「我母亲无残疾」**：必须输出 `disabled=false` 且 `quote` 含该否定词逐字片段，不得抛 `health_not_disability_denial`。测试见 Task 1。
3. **用户说「母亲是残疾人，还没评级」**：不得被否定逻辑吞掉，等级追问必须保留。测试见 Task 1。
4. **用户用称谓而非「成员N」指代**（「我母亲李某58岁」）：成员标签必须能渲染出姓名。测试见 Task 2。
5. **纯姓名无称谓**（「李某60岁」）：不得因为不是「成员N」就被当成家庭成员之外的人而丢失。测试见 Task 2。

---

### Task 1: 抽取提示词区分「健康」与「显式否认残疾」

**Files:**
- Modify: `src/policy_core/facts.py:17`（`FACT_PROMPT` 里的「健康不能单独证明无残疾」一句）
- Modify: `src/policy_core/full_facts.py:145`（`ingest()` 内联提示词尾部的「健康绝不提disabled=false」）
- Test: `tests/test_fact_prompt_negation.py`（新建）

**Interfaces:**
- Consumes: `src/policy_core/full_facts.py:23` `validate_fact(item: dict, text: str, fields: dict, members: set) -> None`（抛 `ValueError` 表示拒绝）；`src/policy_core/full_facts.py:142` `ingest(hh, text, fields, model, extract_only=None, source_text=None)`；`src/policy_core/facts.py` 的 `FACT_PROMPT`。
- Produces: `facts.FACT_PROMPT` 内新增两句固定文案（下文 Step 3 给出原文）；`full_facts` 模块级常量 `NEGATION_PROMPT_TAIL: str`（供 Task 3 与测试引用）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_fact_prompt_negation.py`：

```python
# -*- coding: utf-8 -*-
import unittest

from policy_core import facts, full_facts


class TestFactPromptNegationContract(unittest.TestCase):
    def test_shared_prompt_states_both_halves(self):
        """同一个提示词里必须同时给出禁止与允许，否则模型只学会"别提"。"""
        self.assertIn('健康不等于无残疾', facts.FACT_PROMPT)
        self.assertIn('用户明说无残疾时，必须输出 disabled=false', facts.FACT_PROMPT)

    def test_extract_tail_no_longer_forbids_blanket(self):
        self.assertNotIn('健康绝不提disabled=false', full_facts.NEGATION_PROMPT_TAIL)
        self.assertIn('用户明说无残疾时，必须输出 disabled=false', full_facts.NEGATION_PROMPT_TAIL)


class TestValidateFactDisabled(unittest.TestCase):
    def setUp(self):
        self.fields = facts.dictionary()
        self.members = {'1', 'household'}

    def test_explicit_denial_is_accepted(self):
        item = dict(entity='1', field='disabled', value=False, quote='我母亲无残疾')
        full_facts.validate_fact(item, '我母亲无残疾。', self.fields, self.members)

    def test_health_alone_is_rejected(self):
        item = dict(entity='1', field='disabled', value=False, quote='我母亲身体健康')
        with self.assertRaises(ValueError) as ctx:
            full_facts.validate_fact(item, '我母亲身体健康。', self.fields, self.members)
        self.assertIn('health_not_disability_denial', str(ctx.exception))

    def test_disability_stated_without_grade_is_still_true(self):
        item = dict(entity='1', field='disabled', value=True, quote='母亲是残疾人，还没评级')
        full_facts.validate_fact(item, '母亲是残疾人，还没评级。', self.fields, self.members)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_fact_prompt_negation -v`
Expected: FAIL —— `AttributeError: module 'policy_core.full_facts' has no attribute 'NEGATION_PROMPT_TAIL'`（第一、二条），第三、四条组应已通过（证明守卫本身没坏）。

- [ ] **Step 3: 改提示词**

在 `src/policy_core/facts.py` 的 `FACT_PROMPT` 中，把现有「健康不能单独证明无残疾」一处改写成两句、缺一不可：

```
健康不等于无残疾：仅凭"身体健康/没有病/都很硬朗"不得输出 disabled=false。
用户明说无残疾时，必须输出 disabled=false（"无残疾""没有残疾""不是残疾人""没有残疾证"都算），quote 必须含该否定词的逐字片段。
```

在 `src/policy_core/full_facts.py` 模块级（`MEMBER` 常量附近）新增：

```python
NEGATION_PROMPT_TAIL = (
    '\n健康不等于无残疾：仅凭"身体健康"不得输出 disabled=false；'
    '用户明说无残疾时，必须输出 disabled=false，quote 取含否定词的原文片段。'
)
```

并删掉 `ingest()` 内联提示词里 `健康绝不提disabled=false；` 这半句（其余不动），把 `NEGATION_PROMPT_TAIL` 拼在同样的位置。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_fact_prompt_negation -v`
Expected: PASS（5 条）

- [ ] **Step 5: 全量回归**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`
Expected: `OK`，总数 1018 + 5 = 1023

- [ ] **Step 6: Commit**

```bash
git add src/policy_core/facts.py src/policy_core/full_facts.py tests/test_fact_prompt_negation.py
git commit -m "fix(facts): 抽取提示词区分健康与非残疾，显式否认残疾必须落 disabled=false"
```

---

### Task 2: 成员称谓与姓名发现

**Files:**
- Modify: `src/policy_core/member_labels.py:79-113`（`_MEMBER_HEAD` 与 `extract_member_names`）
- Modify: `src/policy_core/full_facts.py:165-170`（`ingest` 里 `st['member_names']` 的写入点）
- Test: `tests/test_member_labels.py`（已存在则追加类，不存在则新建）

**Interfaces:**
- Consumes: 既有 `extract_member_names(text: str) -> dict[str, str]`（键是成员编号字符串）、`member_display(member_no, name) -> str`（已有「成员1（李某）」渲染能力，本任务不改它）。
- Produces: `extract_member_names` 的新行为——除「成员N（姓名）」外，还识别 **称谓+姓名**（`母亲李某`、`我母亲李某`、`父亲李四`）与 **称谓单独出现时的角色映射**；新增 `extract_kinship_roles(text: str) -> dict[str, str]` 返回 `{称谓: 姓名或空串}`，纯函数、无副作用。

- [ ] **Step 1: 钉住 `member_no` 的分配规则（一次性调查，产出写进任务报告）**

Run:
```bash
grep -rn "members'\].append\|st\['members'\]" src/policy_core/full_facts.py src/policy_core/full_flow.py src/household_state.py | head -20
```

把「本轮新成员被分配 `member_no` 的那一处函数名与 `file:line`」记进本任务的 `task-b-report.md`。
这一步的判定标准：能指着某个具体函数说「成员编号是在这里定的」。若 `full_facts` 只是把 LLM 返回的 `members` 列表当作编号来源，就把该结论写清楚（则 Task 2 的绑定只能做「姓名 → 编号」的**同序映射**，不得猜测）。

- [ ] **Step 2: 写失败测试**

在 `tests/test_member_labels.py` 追加：

```python
class TestKinshipAndNameDiscovery(unittest.TestCase):
    def test_kinship_with_name(self):
        found = member_labels.extract_kinship_roles('我母亲李某58岁，我本人30岁')
        self.assertEqual(found.get('母亲'), '李某')
        self.assertEqual(found.get('本人'), '')

    def test_plain_name_next_to_kinship(self):
        found = member_labels.extract_kinship_roles('李某是我母亲，60岁')
        self.assertEqual(found.get('母亲'), '李某')

    def test_unknown_kinship_is_absent(self):
        self.assertEqual(member_labels.extract_kinship_roles('家里三口人'), {})

    def test_member_head_still_works(self):
        self.assertEqual(member_labels.extract_member_names('成员1（张三）'), {'1': '张三'})
```

- [ ] **Step 3: 跑测试确认失败**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_member_labels -v`
Expected: FAIL —— `AttributeError: module 'policy_core.member_labels' has no attribute 'extract_kinship_roles'`

- [ ] **Step 4: 实现 `extract_kinship_roles(text: str) -> dict[str, str]`**

在 `src/policy_core/member_labels.py` 加：

- 模块级 `KINSHIP = ('母亲','妈妈','父亲','爸爸','配偶','妻子','丈夫','儿子','女儿','孙子','孙女','本人')`
- 函数：先按 `KINSHIP` 找称谓位置；再在称谓**紧邻**（前 6 字或后 6 字内，且不同时跨句号/逗号）找一段 2–4 字的纯中文，且不是 `_NAME_STOPWORDS` 里的词 → 记为姓名；找不到姓名则记空串。同称谓只记第一次。
- `extract_member_names` 末尾追加：对 `extract_kinship_roles` 得到的姓名，若 `members` 已知（函数新增可选参数 `known_members: list[str] | None = None`，默认 `None` 保持旧行为），按出现顺序与已知编号一一对应填入；`known_members` 为 `None` 时不猜测。

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_member_labels -v`
Expected: PASS（含既有用例不回归）

- [ ] **Step 6: 全量回归**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add src/policy_core/member_labels.py tests/test_member_labels.py
git commit -m "feat(member): 识别称谓与相邻姓名，为追问点名提供依据"
```

---

### Task 3: 离线端到端见证（说了无残疾就不再问等级）

**Files:**
- Test: `tests/test_negation_end_to_end.py`（新建）
- Reuse: `docs/superpowers/evidence/2026-11-10-negation/repro_negation4.py`（先读它，理解真实链路怎么喂 `hh`）

**Interfaces:**
- Consumes: `policy_core.full_flow.run(hh, text, session_id, user_id, model)`；`policy_core.full_facts.validate_fact`（Task 1）；`policy_core.member_labels.extract_kinship_roles`（Task 2）；测试手法照抄 `tests/test_readonly_national_flow.py:50-60`（`mock.patch.object(full_flow, ...)`）。
- Produces: 一个不依赖真实 LLM、不依赖数据库的回归测试，锁住「显式否认 ⇒ 不追问 `disability_grade`」。

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
import unittest
from unittest import mock

from policy_core import full_flow


def _fake_ingest(hh, text, fields, model, extract_only=None, source_text=None):
    """把'我母亲无残疾'落成 disabled=false，其余环节照常。"""
    st = full_flow.state(hh) if hasattr(full_flow, 'state') else None
    raise unittest.SkipTest('见 Step 3：改用 facts.state(hh) 构造')


class TestNegationNeverAsksGrade(unittest.TestCase):
    def test_explicit_denial_suppresses_grade_question(self):
        ...
```

实现要求（写测试时就把断言写全，实现留到 Step 3 不适用——本任务只写测试与见证）：

- 用 `tests/test_readonly_national_flow.py` 同类手法，把会联网/会写库的依赖全部 `mock.patch.object` 掉；
- 构造 `hh`：两名成员（1 号 30 岁、2 号 58 岁），`policy_core.facts` 里 `2:disabled = {'state':'known','value':False}`，`household:region` 已知为莲花县；
- 断言：`fff_questions = result.get('questions') or []` 的 key 列表里**不含**任何以 `:disability_grade` / `:disability_certificate` 结尾的键；
- 反向断言（同文件第二个用例）：把 `2:disabled` 换成 `{'state':'known','value':True}` 且 `disability_grade` 缺失时，等级追问**必须**出现——证明不是把追问整体关掉。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_negation_end_to_end -v`
Expected: FAIL 或 ERROR（测试尚未补全/依赖尚未 mock 干净）

- [ ] **Step 3: 补完测试到可运行**

把 mock 与断言补全至可运行；若发现 `full_flow.run` 需要数据库会话（`QaLog`），照 `tests/test_readonly_national_flow.py` 的做法 mock 掉 `household_state.latest`，不要真的连库。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest tests.test_negation_end_to_end -v`
Expected: PASS（2 条）

- [ ] **Step 5: 全量回归**

Run: `PYTHONPATH=src:. /home/adminroot/miniconda3/envs/smart-help-web/bin/python -m unittest discover -s tests -p "test_*.py"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add tests/test_negation_end_to_end.py
git commit -m "test(negation): 离线见证显式否认不再触发残疾等级追问"
```

---

## 验收（人工，跑真链路）

改完后重跑真实链路（不改脚本，只改输入）：

```bash
cd /home/adminroot/smart-help && set -a && . deploy/web.env && set +a
PYTHONPATH=src /home/adminroot/miniconda3/envs/smart-help-web/bin/python \
  docs/superpowers/evidence/2026-11-10-negation/repro_negation4.py
```

判据：
1. 第二轮之后 `members[1].fields` 里出现 `disability='无残疾'`（或等价）且**没有** `disability_grade`；
2. 追问键里不出现 `disability_grade`；
3. 追问文案若涉及该成员，显示为「成员2（李某）」而不是「成员2」。
