# -*- coding: utf-8 -*-
"""M3 条款切分单测：绝对偏移、边界规则、max_len 强切、幂等与真实库样例。

全部标准库 unittest；不连库、不调模型、不写数据。真实样例是 policy_detail
detail_id=4897 的 conditions 原文硬编码，避免测试依赖运行环境。
"""
import unittest

from policy_core.clause_split import MIN_LEN, SEPARATORS, split


def rebuild(text, parts):
    """按绝对偏移把片段放回原文。

    片段之间的空洞就是被丢弃的空白/句末标点；把片段和空洞按原顺序拼起来必须
    逐字等于输入，这是"偏移可还原"的可执行定义。
    """
    out = []
    cursor = 0
    for part in parts:
        out.append(text[cursor:part['start']])
        out.append(text[part['start']:part['end']])
        cursor = part['end']
    out.append(text[cursor:])
    return ''.join(out)


class SplitBasicsTest(unittest.TestCase):
    def test_empty_inputs(self):
        for text in ('', '   ', '\t\r\n', None):
            self.assertEqual(split(text), [], repr(text))
        self.assertEqual(split(' \n 。；!? '), [])

    def test_single_sentence(self):
        text = '符合条件的防止返贫致贫对象。'
        parts = split(text, kind='conditions')
        self.assertEqual(len(parts), 1)
        part = parts[0]
        self.assertEqual((part['idx'], part['kind']), (0, 'conditions'))
        self.assertEqual(part['text'], '符合条件的防止返贫致贫对象')
        self.assertEqual((part['start'], part['end']), (0, len(text) - 1))
        self.assertEqual(text[part['start']:part['end']], part['text'])

    def test_multiple_sentences(self):
        text = '第一句话。第二句话！第三句话？'
        parts = split(text)
        self.assertEqual([p['text'] for p in parts], ['第一句话', '第二句话', '第三句话'])
        self.assertEqual([p['idx'] for p in parts], [0, 1, 2])
        self.assertEqual([(p['start'], p['end']) for p in parts], [(0, 4), (5, 9), (10, 14)])
        self.assertEqual(rebuild(text, parts), text)

    def test_semicolon_and_newline(self):
        text = ('第一条条件；第二条条件;第三条条件\n第四条条件\r\n'
                '第五条条件！第六条条件？第七条条件!第八条条件?')
        parts = split(text)
        self.assertEqual(
            [p['text'] for p in parts],
            ['第一条条件', '第二条条件', '第三条条件', '第四条条件',
             '第五条条件', '第六条条件', '第七条条件', '第八条条件'],
        )
        self.assertEqual(rebuild(text, parts), text)

    def test_short_and_blank_fragments_dropped(self):
        text = '好。\n  \n这是一个完整条款。。短。'
        self.assertEqual([p['text'] for p in split(text)], ['这是一个完整条款'])
        # 3 字丢弃、4 字保留，边界按字符数而不是字节数。
        self.assertEqual(split('三个字。'), [])
        self.assertEqual([p['text'] for p in split('四个字符。')], ['四个字符'])

    def test_commas_are_not_boundaries(self):
        # 逗号/顿号是句内并列，不切；否则 conditions 会被拆成大量没有主语的碎片。
        text = '第一句话，还是第一句话。'
        parts = split(text)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['text'], '第一句话，还是第一句话')

    def test_kind_is_passthrough_only(self):
        marker = object()
        parts = split('第一句话。第二句话。', kind=marker)
        self.assertTrue(all(p['kind'] is marker for p in parts))
        self.assertEqual([p['kind'] for p in split('第一句话。')], [None])
        self.assertEqual([p['kind'] for p in split('第一句话。', kind='')], [''])

    def test_non_str_rejected(self):
        for value in (123, b'bytes', ['text']):
            with self.assertRaises(TypeError, msg=repr(value)):
                split(value)

    def test_separator_contract(self):
        self.assertEqual(SEPARATORS, '。；;！？!?\n\r')
        self.assertEqual(MIN_LEN, 4)


class OffsetTest(unittest.TestCase):
    def test_fragments_are_exact_absolute_slices(self):
        text = '  第一句话。\r\n第二句话；短。\n\n第三句话！  '
        parts = split(text)
        self.assertEqual([p['text'] for p in parts], ['第一句话', '第二句话', '第三句话'])
        self.assertGreater(parts[0]['start'], 0)  # 偏移是原文本绝对坐标，不是片段内坐标
        previous_end = 0
        for part in parts:
            self.assertEqual(text[part['start']:part['end']], part['text'])
            self.assertEqual(part['text'], part['text'].strip())
            self.assertGreater(part['end'], part['start'])
            self.assertGreaterEqual(part['start'], previous_end)  # 有序、不重叠
            previous_end = part['end']

    def test_offsets_restore_full_input(self):
        text = '  第一句话。\r\n\n第二句话；短。第三句话！  '
        parts = split(text)
        self.assertEqual(rebuild(text, parts), text)
        # 片段本身不含句末标点，空洞里仍有它们；两者合起来才是完整原文。
        self.assertEqual(''.join(p['text'] for p in parts), '第一句话第二句话第三句话')


class MaxLenTest(unittest.TestCase):
    def test_hard_split_keeps_offsets_and_cap(self):
        text = '一二三四五六七八九十'
        parts = split(text, max_len=4)
        self.assertEqual([p['text'] for p in parts], ['一二三四', '五六七八'])
        self.assertEqual([(p['start'], p['end']) for p in parts], [(0, 4), (4, 8)])
        self.assertTrue(all(len(p['text']) <= 4 for p in parts))
        self.assertEqual(rebuild(text, parts), text)

    def test_hard_split_with_sentences(self):
        text = '一二三四五六七八九十十一。甲乙丙丁戊己庚辛壬癸子丑。'
        parts = split(text, max_len=6)
        self.assertEqual(
            [p['text'] for p in parts],
            ['一二三四五六', '七八九十十一', '甲乙丙丁戊己', '庚辛壬癸子丑'],
        )
        self.assertTrue(all(len(p['text']) <= 6 for p in parts))
        self.assertEqual(rebuild(text, parts), text)
        self.assertEqual(text[parts[2]['start']:parts[2]['end']], parts[2]['text'])

    def test_max_len_boundary_does_not_split(self):
        self.assertEqual([p['text'] for p in split('一二三四', max_len=4)], ['一二三四'])
        self.assertEqual([p['text'] for p in split('一二三四', max_len=10)], ['一二三四'])
        self.assertEqual([p['text'] for p in split('一二三四', max_len=None)], ['一二三四'])

    def test_short_tail_after_hard_split_dropped(self):
        # 8 字按 6 切：前 6 字保留，尾部 2 字按最短长度规则丢弃；偏移仍可还原。
        text = '一二三四五六七八'
        parts = split(text, max_len=6)
        self.assertEqual([p['text'] for p in parts], ['一二三四五六'])
        self.assertEqual(rebuild(text, parts), text)

    def test_max_len_validation(self):
        with self.assertRaises(ValueError):
            split('一二三四', max_len=0)
        with self.assertRaises(ValueError):
            split('一二三四', max_len=-1)
        for value in ('4', 4.0, True):
            with self.assertRaises(TypeError, msg=repr(value)):
                split('一二三四', max_len=value)


class IdempotenceTest(unittest.TestCase):
    def test_repeated_calls_identical(self):
        text = '  第一句话。\r\n第二句话；第三句话！  '
        self.assertEqual(split(text), split(text))
        self.assertEqual(split(text, kind='conditions', max_len=4),
                         split(text, kind='conditions', max_len=4))

    def test_resplitting_each_clause_is_identity(self):
        text = '第一条条件；第二条条件。\n第三条条件！'
        for part in split(text, kind='conditions'):
            again = split(part['text'], kind=part['kind'])
            self.assertEqual(again, [dict(idx=0, start=0, end=len(part['text']),
                                          text=part['text'], kind=part['kind'])])


class RealPolicyDetailTest(unittest.TestCase):
    # policy_detail detail_id=4897（region=吉州区）conditions 原文，硬编码。
    REAL_CONDITION = (
        '对持有吉州区户籍，共同生活的家庭成员人均收入低于户籍所在地最低生活保障标准，'
        '且家庭财产状况符合户籍所在地规定的家庭，给予最低生活保障。'
    )

    def test_real_condition_split(self):
        text = self.REAL_CONDITION
        parts = split(text, kind='conditions')
        self.assertEqual(len(parts), 1)
        part = parts[0]
        self.assertEqual(part['text'], text[:-1])
        self.assertEqual((part['start'], part['end']), (0, len(text) - 1))
        self.assertEqual(part['kind'], 'conditions')
        self.assertEqual(text[part['start']:part['end']], part['text'])
        self.assertEqual(rebuild(text, parts), text)
        self.assertGreaterEqual(len(part['text']), MIN_LEN)


if __name__ == '__main__':
    unittest.main()
