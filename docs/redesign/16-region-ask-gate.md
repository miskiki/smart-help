# 地区无法确认时必须追问：不再静默按江西政策库匹配

状态：已上线（2026-11-08，提交 f34c0e0）
开关：REGION_ASK_GATE（默认 1；设为 0/off/false/no 恢复旧行为）
测试：tests/test_region_ask_gate.py（12 例）

## 1. 问题

src/kg_retrieval.py 第 190-218 行 service_scope_check 对家庭地区返回三种状态：

- ok：能确认属于江西省服务范围；
- blocked：明确是外省（如“湖南省株洲市”），全链路给"仅支持江西省"文案；
- ask：无法确认属于江西（如只写“株洲市”“南昌路88号”、省市矛盾）。

修复前 src/policy_core/full_flow.py 只处理 blocked，ask 被静默忽略，继续在江西-only 政策库检索并可能生成卡片；
claims 三段式（src/policy_core/claim_answer.py）也没有地区状态提示，等于对用户默认了归属地。

## 2. 修复

### 2.1 追问门（src/policy_core/full_flow.py）

- region_ask_gate_enabled()：REGION_ASK_GATE 默认开，0/off/false/no 关闭。
- _region_unresolved(value)：canonical_levels 的 level 为 unknown 或 province（只到省、未到县区）视为未确认。
- 触发条件：REGION_ASK_GATE 开 and service_scope_check 返回 ask and 地区字段能归一（region_known）但未解析到县区。
- 触发后：coverage 记 region_gate=True、region_unconfirmed=reason、region_status='unknown'；发 region.gate 事件；
  跳过规则引擎与政策检索（条件追加 and not region_ask）；只问一个地区问题（fact_key=household:region）；
  回答用确定性别文案：说明本轮没有按属地核验、不会生成政策卡片，并请补充“市／县（区）全称”。

### 2.2 回答边界与空检索（claim_answer.py / result_model.py）

- claim_answer._boundary：region_status=unknown 或 region_unconfirmed 时追加“家庭所在地区本次未确认，
  以上卡片尚未按属地逐条核验，不能视为当地适用政策；补充所在市／县（区）全称后可重新核对。”
- result_model._empty_search_notes：region_gate 时不输出“未检索到政策”的空提示；
  “请补充更完整的家庭信息”兜底句仅在非 region_gate、非 scope_blocked 时出现。

### 2.3 话术中性化（fields.json / household_flow.py）

region 字段含义改为“家庭所在县区，用于判断政策属地；不是户籍、务工或就学地。当前政策库仅收录江西省政策”，
问题改为“家庭所在市／县（区）全称是？（当前仅支持江西省内政策）”。hr 追问原因同步。

### 2.4 事件

src/events.py：DOMAIN_EVENTS 增加 'region.gate': ('region.gate','decide','user','家庭地区待确认')；
narrative 增加对应中文人话“家庭所在地区还不能确认属于江西省服务范围：<reason>”。

## 3. 验收

- 单测：tests/test_region_ask_gate.py 12 例全过；全量 discover 323 例通过。
- 真机 probe（/tmp/probe_region_gate.py，真实链路）：
  株洲带省 → 4.3s，blocked 文案、0 卡；株洲不带省 → 5.2s，region_gate=true、1 个地区追问、0 卡；
  莲花县 → 5.4s，规则路径 7 卡（S-ZB-090/091、S-ZF-008、X-CY-301、X-ZB-300/301/462）。
- 生产 5000（HUP 后 worker 751616）：
  莲花 3.4s / 23 用户事件 / 7 行 / 1148 字 / delta 拼合一致；
  株洲不带省 3.0s / 16 用户事件 / region_gate=true / 1 个追问 / 156 字 / delta 一致；
  青山湖 15.9s / 21 用户事件 / 1 行 S-QJ-043（模型抽取非确定性，gate 不影响 scope=ok）。
- 9 案例回归（eval_run）：9/9 成功、24 卡；c1/c2/c3/c4/c5/c7 各 2 卡（S-ZB-090/091）、
  c8 7 卡集合不变、c9（新建区+雨露计划）2 卡 S-QJ-008/S-QJ-043、c6（无地区）3 卡走全模型（region unknown 封顶 B，带边界说明）。

## 4. 回退与遗留

- 回退：REGION_ASK_GATE=0（进程重启生效）。
- 遗留：家庭完全没填地区（region_known=false）仍走全模型路径（c6），本身不加属地过滤、卡片封顶 B 且带地区未确认边界；
  若要求“没填地区也先追问”，可把触发条件放宽到 not region_known，作为后续独立改动评估。
