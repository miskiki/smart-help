# -*- coding: utf-8 -*-
"""确定性条款切分（M3 数据层第二步）。

为什么需要：policy_detail 的 conditions/target/benefit 是自由文本，下游规则原子与
证据绑定必须能指回原文的具体片段。这里只做**字符级、可复现**的切分：按显式句末
标点和换行断开，保留原文绝对偏移；不调用模型、不做语义归类，因此同一输入在任何
机器、任何批次上都得到同一结果。

两个关键约定：
  * 片段 text 恰等于原文 text[start:end]，start/end 是**原始文本**中的绝对坐标；
  * 片段之间允许有空洞——被丢弃的空白、句末标点、过短碎片仍留在原文里。按顺序
    用空洞补齐即可逐字还原输入（见 docs/redesign/15-m3-clause-split.md）。

为什么不在切分时猜语义：条款类型（对象/条件/标准/材料/流程）应由后续抽取层基于
原文判断；切分层一旦"顺手"分类，错误会以数据形式沉淀，且难以回滚。kind 参数只
透传调用方给的标签，本模块从不读取或改写它。
"""

# 句末标点与换行：中英文都要覆盖；不切逗号/顿号，因为它们是句内并列，不是条款边界。
SEPARATORS = '。；;！？!?\n\r'

# 少于 4 个字符的碎片多半是"好""新生儿""新建"这类残词，作为条款没有证据价值；
# 4 字是硬下限，不做语言相关的"看起来像词"判断。
MIN_LEN = 4


def _trim(text, start, end):
    """把 [start, end) 两端的空白收进来，返回内容真实边界。

    只去空白，不去句末标点：标点已经被 SEPARATORS 切在片段之外，保留片段内
    的引号/括号等原字符，保证 text[start:end] 与原文逐字一致。
    """
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _sentences(text):
    """按 SEPARATORS 扫描出原始区间；标点本身不属于任何片段。"""
    start = 0
    for pos, ch in enumerate(text):
        if ch in SEPARATORS:
            yield start, pos
            start = pos + 1
    yield start, len(text)


def _chunks(start, end, max_len):
    """把 [start, end) 按 max_len 硬切成等长窗口。

    为什么硬切而不是找逗号/词边界：max_len 是给向量化与展示兜底的硬上限，
    找"看起来更自然"的断点会引入新的语义判断；固定窗口可预测、可测试。
    末尾不足 MIN_LEN 的残片仍由 split() 按最短长度规则丢弃。
    """
    if not max_len or end - start <= max_len:
        yield start, end
        return
    pos = start
    while pos < end:
        nxt = min(pos + max_len, end)
        yield pos, nxt
        pos = nxt


def split(text, kind=None, max_len=None):
    """把一段政策原文切成带绝对偏移的条款片段。

    参数：
      text    原始文本；None/空串/纯空白返回 []，其他非 str 类型抛 TypeError。
      kind    仅原样写进每个片段的 kind 字段，不参与切分；默认 None。
      max_len 正整数时，对仍然超长的片段按字符数硬切；None 表示只按标点/换行切。
              硬切后的每个片段仍满足 text[start:end]，且长度 <= max_len；
              不足 MIN_LEN 的残片按同一最短长度规则丢弃。

    返回：按原文顺序排列的 list[dict]，键固定为 idx/start/end/text/kind。
      idx    输出序号（从 0 递增；被丢弃的碎片不占号）。
      start  片段首字符在原始 text 中的下标（含）。
      end    片段尾后下标（不含），text[start:end] == 片段 text。
    """
    if text is None:
        return []
    if not isinstance(text, str):
        raise TypeError('clause_split.split 只接受 str 或 None，收到 %s' % type(text).__name__)
    if max_len is not None:
        if isinstance(max_len, bool) or not isinstance(max_len, int):
            raise TypeError('max_len 必须是正整数或 None')
        if max_len <= 0:
            raise ValueError('max_len 必须大于 0')

    parts = []
    for sent_start, sent_end in _sentences(text):
        sent_start, sent_end = _trim(text, sent_start, sent_end)
        if sent_end - sent_start < MIN_LEN:
            continue
        for start, end in _chunks(sent_start, sent_end, max_len):
            start, end = _trim(text, start, end)
            if end - start < MIN_LEN:
                continue
            parts.append(dict(
                idx=len(parts),
                start=start,
                end=end,
                text=text[start:end],
                kind=kind,
            ))
    return parts
