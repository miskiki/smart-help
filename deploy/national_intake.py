#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全国政策数据接入 CLI（T24）。

场景：同事/共享渠道拿到的全国政策数据（CSV / XLSX / JSON）先放进
data/incoming/（或 NATIONAL_INTAKE_DIR 指向的目录），用本命令扫描、按
既有 policy_import 的字段与校验口径做只读归一，输出质量与覆盖报告；
默认 dry-run 绝不写库，确认无误后再用 --apply 走既有导入管线。

设计约束（与 T19/T22 审计一致）：
- 默认不写库：只有 --apply 且 POLICY_IMPORT=1 才创建批次并提交；
- 字段/校验/地区归一口径复用 policy_import（normalize_row/_validate_row/
  _plan_rows），不在本文件实现第二套写库逻辑，避免两处口径漂移；
- FP=0：同批重复编号、日期冲突、库内冲突都进报告并阻止写库，不静默放过；
- 编码兼容：UTF-8（含 BOM）优先，失败回落 GB18030；保留原始文件行号；
- 降级：数据库不可用时输出离线报告并返回退出码 4，扫描本身不中断。

退出码：0 无错误行；1 有错误行 / --apply 被拒绝或失败；2 参数错误或
POLICY_IMPORT 未开启（--apply 时）；4 数据库不可用（已降级为离线报告）。
"""
import argparse
import csv
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

VERSION = 'national-intake-1'
DEFAULT_INTAKE_DIR = os.path.join(BASE, 'data', 'incoming')
READ_EXTS = ('.csv', '.xlsx', '.json')
MAX_REPORT_ERRORS = 200
MAX_REPORT_WARNINGS = 100
MAX_REPORT_UNRESOLVED = 100
MAX_REPORT_LIST = 200

LEVEL_ORDER = ('C', 'S', 'U', 'X', 'unknown')
REGION_ORDER = ('nation', 'province', 'city', 'county', 'zone', 'unknown')

# 字段别名：键是接入层规范字段，值是原始列名（含英文）。全国数据列名与
# 现有导出/表格导入列名不完全一致（如“政策所属行政区域”“补贴标准/元”），
# 这里先做一层归一再交给 policy_import.normalize_row，未知列直接忽略。
INTAKE_ALIASES = {
    'policy_code': ('政策编号', '政策编码', '政策代码', '编号', '代码',
                    'policy_code', 'policycode', 'code'),
    'policy_name': ('政策名称', '政策名', '名称', '标题',
                    'policy_name', 'policyname', 'name', 'title'),
    'regions': ('行政区域', '政策所属行政区域', '所属行政区域', '政策适用范围',
                '适用范围', '适用地区', '地区', '区域', '省市区',
                'regions', 'region'),
    'level': ('政策层级', '层级', '级别', 'level'),
    'content': ('政策内容', '政策正文', '内容', 'content'),
    'conditions': ('政策适用条件', '适用条件', '申请条件', '条件',
                   'conditions', 'condition'),
    'benefit': ('补贴标准', '补贴标准/元', '补贴标准（元）', '补贴标准(元)',
                '补助标准', '补贴金额', '待遇', '标准', 'benefit', 'subsidy'),
    'unit': ('补贴单位', '计量单位', '单位', 'unit'),
    'cycle': ('补贴周期', '发放周期', '周期', 'cycle'),
    'effect_date': ('政策生效日期', '生效日期', '开始日期', '实施日期',
                    'effect_date', 'start_date', 'effective_date'),
    'expire_date': ('政策失效日期', '失效日期', '截止日期', '结束日期',
                    'expire_date', 'end_date'),
    'issuer': ('政策制定主体部门', '制定主体部门', '制定主体', '发文单位',
               '制定部门', 'issuer'),
    'implementer': ('政策实施部门', '实施部门', '主管部门', '执行部门',
                    'implementer'),
    'doc_ref': ('政策文件名称及文号', '文号', '文件文号', '发文字号',
                '政策文号', 'doc_ref', 'docref'),
    'target': ('政策适用对象', '适用对象', '目标对象', '对象', '目标',
               'target', 'object'),
    'process': ('申领流程', '办理流程', '流程', '申请流程',
                'process', 'procedure'),
    'attr': ('政策属性', '属性', 'attr', 'attribute'),
    'category1': ('政策一级分类', '一级分类', '一级类别', 'category1'),
    'category2': ('政策二级分类', '二级分类', '二级类别', 'category2'),
    'category3': ('政策三级分类', '三级分类', '三级类别', 'category3'),
    'contact': ('政策联系单位/联系人', '政策联系单位、联系人', '联系方式',
                '联系人', '联系单位', 'contact'),
    'phone': ('联系电话', '电话', 'phone', 'tel'),
    'source': ('来源', '文件来源', 'source'),
    'status': ('状态', 'status'),
    'level_note': ('层级说明', '备注说明', 'level_note'),
}

# 以下列在真实全国数据里存在，但 Policy 没有对应字段或已有别处承载，
# 明确忽略而不是当作未知列丢弃，文档 22 会说明口径。
IGNORED_HEADERS = (
    '年份', '年度', 'year', '帮扶方式', '最高限额/元', '最高限额（元）',
    '最高限额(元)', '最高限额', '享受户数', '享受人数', '受益类别',
    '适用劳动能力类型', '适用健康状况', '适用劳动力年龄', '适用致贫返贫类型',
)

# 直接透传给 policy_import 规范字段（明细字段在 _map_to_import 里另组）。
PASSTHROUGH_FIELDS = (
    'policy_code', 'policy_name', 'level', 'category1', 'category2',
    'category3', 'attr', 'doc_ref', 'regions', 'target', 'conditions',
    'benefit', 'process', 'contact', 'phone', 'effect_date', 'expire_date',
    'status', 'source', 'level_note', 'unit', 'cycle',
)
README_TEXT = '''# 全国政策数据接入目录（data/incoming）

把待接入的全国政策数据文件放在本目录，支持 .csv / .xlsx / .json。
默认 dry-run 只读检查、不写库：

    cd <repo> && set -a && . deploy/web.env && set +a
    PYTHONPATH=src:. python deploy/national_intake.py            # 扫描本目录，只读报告
    PYTHONPATH=src:. python deploy/national_intake.py --json     # 机器可读报告
    PYTHONPATH=src:. python deploy/national_intake.py --offline  # 完全不连库
    PYTHONPATH=src:. python deploy/national_intake.py --apply    # 写库（需 POLICY_IMPORT=1）

字段约定、编码、批次与回滚说明见 docs/redesign/22-national-intake.md。
'''


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _norm_header(value):
    """表头归一：去 BOM、去空白、英文转小写，与 policy_import 保持一致。"""
    text_value = str(value or '').replace('\ufeff', '')
    text_value = re.sub(r'\s+', '', text_value)
    return text_value.strip().lower()


_HEADER_LOOKUP = {}
for _canon, _names in INTAKE_ALIASES.items():
    for _name in _names:
        _HEADER_LOOKUP[_norm_header(_name)] = _canon
        _HEADER_LOOKUP[_norm_header(_canon)] = _canon
for _name in IGNORED_HEADERS:
    _HEADER_LOOKUP[_norm_header(_name)] = None


def _cell_text(value):
    """单元格转字符串；日期/数字做稳定格式化，避免 5000.0 这类噪声。"""
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (list, tuple)):
        parts = [_cell_text(item) for item in value]
        return '、'.join(part for part in parts if part)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    # datetime/date：只保留日期部分，与政策日期字段兼容。
    if hasattr(value, 'isoformat'):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value).strip()


def _map_header(value):
    key = _norm_header(value)
    if not key:
        return None
    return _HEADER_LOOKUP.get(key)


def _read_text(path):
    with open(path, 'rb') as handle:
        data = handle.read()
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError('无法识别文件编码（支持 UTF-8 / GB18030）')


def _csv_matrix(path):
    reader = csv.reader(io.StringIO(_read_text(path)))
    return [list(row) for row in reader]


def _xlsx_sheets(path):
    try:
        from openpyxl import load_workbook
    except Exception as exc:  # openpyxl 是既有依赖，缺失时要给人话提示
        raise ValueError('读取 XLSX 需要 openpyxl：%s' % exc)
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheets = []
    try:
        for sheet in workbook.worksheets:
            matrix = [list(row) for row in sheet.iter_rows(values_only=True)]
            sheets.append((sheet.title, matrix))
    finally:
        try:
            workbook.close()
        except Exception:
            pass
    return sheets


def _records_from_matrix(matrix, sheet=None):
    """从二维表格提取记录：先找表头行，再逐行按列号取值。

    行号使用矩阵物理下标 + 1（CSV 为记录序号，XLSX 为 sheet 内物理行号），
    这样报告里的行号可以直接回到原始文件定位。
    """
    header_index = None
    header_map = {}
    header_cells = []
    for index, row in enumerate(matrix):
        cells = [_cell_text(cell) for cell in row]
        if not any(cells):
            continue
        mapping = {}
        for column, cell in enumerate(cells):
            canonical = _map_header(cell)
            if canonical:
                mapping[column] = canonical
        names = set(mapping.values())
        if 'policy_name' in names or 'policy_code' in names:
            header_index = index
            header_map = mapping
            header_cells = cells
            break
    if header_index is None:
        return None, []

    warnings = []
    seen = {}
    for column in sorted(header_map):
        canonical = header_map[column]
        if canonical in seen:
            warnings.append('表头第 %d 列“%s”与第 %d 列重复映射到字段 %s，已取前者'
                            % (column + 1, header_cells[column], seen[canonical] + 1,
                               canonical))
        else:
            seen[canonical] = column

    records = []
    for index in range(header_index + 1, len(matrix)):
        cells = [_cell_text(cell) for cell in matrix[index]]
        if not any(cells):
            continue
        values = {}
        for column in sorted(header_map):
            canonical = header_map[column]
            if canonical in values:
                continue
            if column < len(cells) and cells[column]:
                values[canonical] = cells[column]
        if values:
            records.append({'row_no': index + 1, 'sheet': sheet, 'values': values})
    return records, warnings


def _records_from_json(path):
    try:
        data = json.loads(_read_text(path))
    except ValueError as exc:
        raise ValueError('JSON 解析失败：%s' % exc)
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        if isinstance(data.get('rows'), list):
            rows = data['rows']
        elif isinstance(data.get('data'), list):
            rows = data['data']
        else:
            rows = [data]
    else:
        raise ValueError('JSON 顶层必须是对象数组或 {"rows": [...]}')
    records = []
    warnings = []
    for index, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            warnings.append({'row_no': index, 'warnings': ['JSON 第 %d 行不是对象，已跳过' % index]})
            continue
        values = {}
        for key, value in item.items():
            canonical = _map_header(key)
            if not canonical or canonical in values:
                continue
            text_value = _cell_text(value)
            if text_value:
                values[canonical] = text_value
        if values:
            records.append({'row_no': index, 'sheet': None, 'values': values})
    return records, warnings


def read_records(path):
    """读取一个文件，返回 (records, warnings)；格式/表头错误抛 ValueError。"""
    suffix = os.path.splitext(path)[1].lower()
    if suffix == '.csv':
        records, warnings = _records_from_matrix(_csv_matrix(path))
        if records is None:
            raise ValueError('未找到表头：至少需要“政策名称”或“政策编号”列')
        return records, warnings
    if suffix == '.xlsx':
        records = []
        warnings = []
        found = False
        for sheet, matrix in _xlsx_sheets(path):
            sheet_records, sheet_warnings = _records_from_matrix(matrix, sheet)
            if sheet_records is None:
                if any(any(_cell_text(cell) for cell in row) for row in matrix):
                    warnings.append('工作表 %s 未找到表头，已跳过' % sheet)
                continue
            found = True
            records.extend(sheet_records)
            warnings.extend(sheet_warnings)
        if not found:
            raise ValueError('未找到表头：至少需要“政策名称”或“政策编号”列')
        return records, warnings
    if suffix == '.json':
        return _records_from_json(path)
    raise ValueError('不支持的文件类型：%s（支持 .csv / .xlsx / .json）' % suffix)


# ---------------------------------------------------------------------------
# 字段映射：接入层列名 -> policy_import 规范行
# ---------------------------------------------------------------------------
def _map_to_import(values):
    """把一行接入字段映射成 policy_import.normalize_row 认识的规范行。

    - 政策内容（Policy 无正文字段）只在没有“政策适用条件”时兜底为条件；
    - 制定部门/实施部门合并进 level_note，避免这两个字段丢失；
    - 有申请条件或适用对象时合成一条明细，让明细表/地区归一有内容可落。
    """
    raw = {field: values[field] for field in PASSTHROUGH_FIELDS if values.get(field)}
    warnings = []

    content = values.get('content') or ''
    if content:
        if raw.get('conditions'):
            warnings.append('已有政策适用条件，政策内容未导入（Policy 无正文字段）')
        else:
            raw['conditions'] = content
            warnings.append('政策内容已作为申请条件兜底（Policy 无正文字段）')

    issuer = values.get('issuer') or ''
    implementer = values.get('implementer') or ''
    if not raw.get('level_note') and (issuer or implementer):
        parts = []
        if issuer:
            parts.append('制定部门：%s' % issuer)
        if implementer:
            parts.append('实施部门：%s' % implementer)
        raw['level_note'] = '；'.join(parts)

    if raw.get('conditions') or raw.get('target'):
        detail = {
            'region': raw.get('regions') or '',
            'benefit': raw.get('benefit') or '',
            'unit': raw.get('unit') or '',
            'cycle': raw.get('cycle') or '',
            'conditions': raw.get('conditions') or '',
            'target': raw.get('target') or '',
            'process': raw.get('process') or '',
            'effect_date': raw.get('effect_date') or '',
            'expire_date': raw.get('expire_date') or '',
        }
        raw['details'] = [{key: value for key, value in detail.items() if value}]
    return raw, warnings


# ---------------------------------------------------------------------------
# 校验与统计
# ---------------------------------------------------------------------------
def _region_bucket(scope_row):
    if scope_row.get('zone'):
        return 'zone'
    level = scope_row.get('level') or ''
    if level in ('nation', 'province', 'city', 'county'):
        return level
    return 'unknown'


def _analyze_file(file_input, policy_import, policy_scope):
    """逐行复用 policy_import._validate_row 校验，并累计质量/覆盖统计。"""
    validate_row = getattr(policy_import, '_validate_row', None)
    parse_date = getattr(policy_import, '_parse_date', None)
    if not callable(validate_row):
        raise RuntimeError('policy_import 缺少 _validate_row，无法复用统一校验口径')

    name = file_input['name']
    records = file_input['records']
    errors = []
    warnings = []
    unresolved = []
    # JSON 路径的 warnings 是带行号的 dict，CSV/XLSX 路径是纯文本（如表头
    # 重复列的提示）；两种形态统一包装成报告用的 {file,row_no,warnings} 结构。
    for item in file_input.get('warnings') or []:
        if isinstance(item, dict):
            warnings.append({'file': name, 'row_no': item.get('row_no') or 0,
                             'warnings': list(item.get('warnings') or [])})
        else:
            warnings.append({'file': name, 'row_no': 0,
                             'warnings': [str(item)]})

    row_items = []
    for record in records:
        raw, map_warnings = _map_to_import(record['values'])
        result = validate_row(raw, record['row_no'])
        row_items.append({'record': record, 'raw': raw, 'result': result})
        for text_value in map_warnings:
            item = {'file': name, 'row_no': record['row_no'], 'warnings': [text_value]}
            if record.get('sheet'):
                item['sheet'] = record['sheet']
            warnings.append(item)

    # 同一文件里重复编号必须整组隔离，与 policy_import 的 conflict 语义一致。
    by_code = {}
    for entry in row_items:
        code = entry['result'].get('policy_code') or ''
        if code:
            by_code.setdefault(code, []).append(entry)
    for code, group in by_code.items():
        if len(group) > 1:
            message = '同批次重复政策编号 %s（共 %d 行），整组隔离' % (code, len(group))
            for entry in group:
                entry['result']['status'] = 'invalid'
                entry['result']['errors'].append(message)

    level_distribution = {key: 0 for key in LEVEL_ORDER}
    region_distribution = {key: 0 for key in REGION_ORDER}
    provinces = set()
    rows_ok = 0
    meta_rows = []
    date_rows = []

    for entry in row_items:
        record = entry['record']
        result = entry['result']
        normalized = result.get('normalized') or {}
        code = result.get('policy_code') or ''
        level = normalized.get('level') or 'unknown'
        if level not in level_distribution:
            level = 'unknown'
        level_distribution[level] += 1

        regions = normalized.get('regions') or ''
        scope_rows = []
        if regions:
            detail_regions = []
            for index, detail in enumerate(normalized.get('details') or []):
                if isinstance(detail, dict):
                    detail_regions.append((index + 1, detail.get('region') or ''))
            try:
                scope_rows = policy_scope.rows_for(code, regions, detail_regions)
            except Exception:
                # 区划数据异常不能让整份报告失败；计入“无法解析”待人工核。
                scope_rows = []
        if scope_rows:
            # 行级地区与明细地区会指向同一区划（如“莲花县”同时出现在
            # regions 和明细 region），按 region_key 去重后再计数，避免同一
            # 地区被重复统计；仍按 scope 行而不是按省去重（多地区行按多个
            # 区划各计一次，报告会注明口径）。
            unique_scope = []
            seen_regions = set()
            for scope_row in scope_rows:
                key = (scope_row.get('region_key'), bool(scope_row.get('zone')))
                if key in seen_regions:
                    continue
                seen_regions.add(key)
                unique_scope.append(scope_row)
            for scope_row in unique_scope:
                region_distribution[_region_bucket(scope_row)] += 1
                province = (scope_row.get('province') or '').strip()
                if province:
                    provinces.add(province)
        else:
            region_distribution['unknown'] += 1

        if result.get('status') == 'ok':
            rows_ok += 1

        effect_date = normalized.get('effect_date') or ''
        expire_date = normalized.get('expire_date') or ''
        parsed_effect = parse_date(effect_date) if (effect_date and parse_date) else None
        parsed_expire = parse_date(expire_date) if (expire_date and parse_date) else None
        if parsed_effect and parsed_expire and parsed_expire < parsed_effect:
            date_rows.append({
                'kind': 'expire_before_effect', 'file': name,
                'row_no': record['row_no'], 'policy_code': code,
                'effect_date': parsed_effect, 'expire_date': parsed_expire,
            })
        meta_rows.append({'row_no': record['row_no'], 'policy_code': code,
                          'level': level, 'effect': parsed_effect or '',
                          'expire': parsed_expire or '', 'regions': regions})

        if result['errors']:
            item = {'file': name, 'row_no': record['row_no'],
                    'policy_code': code, 'errors': list(result['errors'])}
            if record.get('sheet'):
                item['sheet'] = record['sheet']
            errors.append(item)
        if result['warnings']:
            item = {'file': name, 'row_no': record['row_no'],
                    'policy_code': code, 'warnings': list(result['warnings'])}
            if record.get('sheet'):
                item['sheet'] = record['sheet']
            warnings.append(item)
        for unresolved_row in result.get('unresolved') or []:
            item = {'file': name, 'row_no': record['row_no'], 'policy_code': code,
                    'raw_text': unresolved_row.get('raw_text'),
                    'source_field': unresolved_row.get('source_field')}
            if record.get('sheet'):
                item['sheet'] = record['sheet']
            unresolved.append(item)

    duplicates_within = sum(1 for group in by_code.values() if len(group) > 1)
    return {
        'name': name,
        'path': file_input['path'],
        'rows_total': len(records),
        'rows_ok': rows_ok,
        'rows_error': len(records) - rows_ok,
        'level_distribution': level_distribution,
        'region_distribution': region_distribution,
        'covered_provinces': len(provinces),
        'provinces': sorted(provinces),
        'duplicate_codes': duplicates_within,
        'date_conflicts': len(date_rows),
        'errors': errors,
        'warnings': warnings,
        'unresolved': unresolved,
        '_meta': meta_rows,
        '_date_rows': date_rows,
    }


def _assemble_report(analyzed, read_errors, intake_dir):
    report = {
        'version': VERSION, 'mode': 'dry_run', 'intake_dir': intake_dir or '',
        'files': [], 'files_total': len(analyzed) + len(read_errors),
        'rows_total': 0, 'rows_ok': 0, 'rows_error': 0,
        'level_distribution': {key: 0 for key in LEVEL_ORDER},
        'region_distribution': {key: 0 for key in REGION_ORDER},
        'covered_provinces': 0, 'provinces': [],
        'duplicate_codes': 0, 'duplicate_code_list': [],
        'date_conflicts': 0, 'date_conflict_list': [],
        'errors': [], 'warnings': [], 'unresolved': [],
        'file_errors': list(read_errors),
        'db': {'status': 'skipped', 'reason': '无待扫描文件'},
        'write': False, 'applied': [], 'note': '未写库',
    }
    errors = []
    warnings = []
    unresolved = []
    provinces = set()
    date_entries = []
    meta_rows = []
    for item in analyzed:
        report['files'].append({
            'name': item['name'], 'path': item['path'],
            'rows_total': item['rows_total'], 'rows_ok': item['rows_ok'],
            'rows_error': item['rows_error'],
            'level_distribution': item['level_distribution'],
            'region_distribution': item['region_distribution'],
            'covered_provinces': item['covered_provinces'],
            'duplicate_codes': item['duplicate_codes'],
            'date_conflicts': item['date_conflicts'],
        })
        report['rows_total'] += item['rows_total']
        report['rows_ok'] += item['rows_ok']
        report['rows_error'] += item['rows_error']
        for key in LEVEL_ORDER:
            report['level_distribution'][key] += item['level_distribution'][key]
        for key in REGION_ORDER:
            report['region_distribution'][key] += item['region_distribution'][key]
        provinces.update(item['provinces'])
        errors.extend(item['errors'])
        warnings.extend(item['warnings'])
        unresolved.extend(item['unresolved'])
        date_entries.extend(item['_date_rows'])
        for row in item['_meta']:
            row = dict(row)
            row['file'] = item['name']
            meta_rows.append(row)

    report['covered_provinces'] = len(provinces)
    report['provinces'] = sorted(provinces)

    # 重复编号：同一编号出现 2 次以上；跨文件重复只提醒，不改行状态。
    grouped = {}
    for row in meta_rows:
        if row['policy_code']:
            grouped.setdefault(row['policy_code'], []).append(row)
    duplicate_list = []
    for code in sorted(grouped):
        rows = grouped[code]
        if len(rows) <= 1:
            continue
        files = sorted({row['file'] for row in rows})
        duplicate_list.append({
            'code': code, 'count': len(rows), 'files': files,
            'rows': [{'file': row['file'], 'row_no': row['row_no']}
                     for row in rows][:20],
        })
        if len(files) > 1:
            warnings.append({'file': '、'.join(files), 'row_no': 0,
                             'warnings': ['政策编号 %s 出现在多个文件，--apply 时按文件顺序依次导入'
                                          % code]})
    report['duplicate_codes'] = len(duplicate_list)
    report['duplicate_code_list'] = duplicate_list[:MAX_REPORT_LIST]

    # 日期冲突：行内失效早于生效 + 同一编号多行生效/失效不一致。
    code_dates = {}
    for row in meta_rows:
        if row['policy_code'] and (row['effect'] or row['expire']):
            code_dates.setdefault(row['policy_code'], []).append(row)
    for code in sorted(code_dates):
        rows = code_dates[code]
        pairs = {(row['effect'], row['expire']) for row in rows}
        if len(pairs) > 1:
            date_entries.append({
                'kind': 'code_dates_inconsistent', 'code': code,
                'values': [{'effect_date': effect, 'expire_date': expire}
                           for effect, expire in sorted(pairs)],
                'rows': [{'file': row['file'], 'row_no': row['row_no']}
                         for row in rows][:20],
            })
    report['date_conflicts'] = len(date_entries)
    report['date_conflict_list'] = date_entries[:MAX_REPORT_LIST]

    report['errors'] = errors[:MAX_REPORT_ERRORS]
    report['warnings'] = warnings[:MAX_REPORT_WARNINGS]
    report['unresolved'] = unresolved[:MAX_REPORT_UNRESOLVED]
    return report


def _has_errors(report):
    return bool(report['rows_error'] or report['file_errors'])


# ---------------------------------------------------------------------------
# 数据库：只读 diff / 单文件事务提交
# ---------------------------------------------------------------------------
class _ApplyAbort(Exception):
    pass


def _db_check(policy_import):
    try:
        engine = policy_import.engine()
        with engine.connect() as conn:
            conn.execute(policy_import.text('SELECT 1'))
        return engine, ''
    except Exception as exc:
        return None, '%s: %s' % (type(exc).__name__, str(exc)[:200])


def _db_preview(policy_import, engine, file_inputs):
    """只读：用 policy_import._plan_rows 在库内算 create/update/noop/conflict。"""
    plan_rows = getattr(policy_import, '_plan_rows', None)
    if not callable(plan_rows):
        raise RuntimeError('policy_import 缺少 _plan_rows，无法做库内 diff')
    actions = {action: 0 for action in policy_import.ACTIONS}
    invalid = []
    conflict = []
    changed = 0
    with engine.connect() as conn:
        for file_input in file_inputs:
            results = plan_rows(conn, file_input['mapped_rows'], file_input['row_nos'])
            for item in results:
                action = item.get('action')
                if action in actions:
                    actions[action] += 1
                row = {'file': file_input['name'], 'row_no': item['row_no'],
                       'policy_code': item.get('policy_code') or '',
                       'errors': list(item.get('errors') or [])[:5]}
                if item.get('status') == 'invalid':
                    invalid.append(row)
                elif item.get('status') == 'conflict':
                    conflict.append(row)
                if item.get('changed_fields'):
                    changed += 1
    return {'actions': actions, 'invalid': invalid, 'conflict': conflict,
            'changed': changed}


def _apply_files(policy_import, engine, file_inputs, source, operator):
    """逐文件单事务：staging -> validate -> commit；失败即整体回滚该文件。"""
    applied = []
    failure = ''
    for file_input in file_inputs:
        try:
            with engine.begin() as conn:
                created = policy_import.create_batch(
                    conn, file_input['mapped_rows'], source=source,
                    file_name=file_input['name'], operator=operator, dry_run=False)
                validate_report = policy_import.validate_batch(conn, created['batch_id'])
                if validate_report.get('invalid') or validate_report.get('conflict'):
                    raise _ApplyAbort('校验未通过（valid=%s invalid=%s conflict=%s）'
                                      % (validate_report.get('valid'),
                                         validate_report.get('invalid'),
                                         validate_report.get('conflict')))
                commit_report = policy_import.commit(conn, created['batch_id'], operator)
            applied.append({
                'file': file_input['name'],
                'batch_id': created['batch_id'],
                'created': commit_report.get('created', 0),
                'updated': commit_report.get('updated', 0),
                'noop': commit_report.get('noop', 0),
                'status': commit_report.get('status'),
            })
        except _ApplyAbort as exc:
            failure = '文件 %s：%s（该文件未写库）' % (file_input['name'], exc)
            break
        except Exception as exc:
            failure = '文件 %s 提交失败：%s: %s（该文件已回滚）' % (
                file_input['name'], type(exc).__name__, exc)
            break
    return applied, failure


# ---------------------------------------------------------------------------
# 扫描与目录约定
# ---------------------------------------------------------------------------
def _ensure_readme(directory):
    path = os.path.join(directory, 'README.md')
    if os.path.exists(path):
        return
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(README_TEXT)
    except OSError:
        # README 只是接入提示，写不了也不影响扫描。
        pass


def _scan(args):
    """返回 (paths, scan_dir, error)。--file 优先，否则扫描目录。"""
    if args.file:
        paths = []
        for item in args.file:
            path = os.path.abspath(item)
            if not os.path.exists(path):
                return [], None, '文件不存在：%s' % item
            if os.path.isdir(path):
                return [], None, '--file 需要文件路径（目录请用 --dir）：%s' % item
            paths.append(path)
        # 去重但保持用户给定顺序
        return list(dict.fromkeys(paths)), None, ''
    scan_dir = args.dir or os.environ.get('NATIONAL_INTAKE_DIR') or DEFAULT_INTAKE_DIR
    scan_dir = os.path.abspath(scan_dir)
    if not os.path.isdir(scan_dir):
        try:
            os.makedirs(scan_dir, exist_ok=True)
        except OSError as exc:
            return [], scan_dir, '无法创建目录 %s：%s' % (scan_dir, exc)
        _ensure_readme(scan_dir)
    else:
        _ensure_readme(scan_dir)
    names = sorted(os.listdir(scan_dir))
    paths = []
    for name in names:
        if name.startswith('.') or name.startswith('~$'):
            continue
        if os.path.splitext(name)[1].lower() not in READ_EXTS:
            continue
        path = os.path.join(scan_dir, name)
        if os.path.isfile(path):
            paths.append(path)
    return paths, scan_dir, ''


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _mode_text(mode):
    return {'offline': 'offline（--offline，不连库）',
            'dry_run': 'dry-run（只读检查）',
            'apply': 'apply（写库）'}.get(mode, mode)


def _db_text(db):
    status = (db or {}).get('status')
    if status == 'offline':
        return '离线（--offline，未连接）'
    if status == 'unavailable':
        return '不可用（已降级离线）：%s' % ((db or {}).get('reason') or '')
    if status == 'ok':
        return '已连接'
    if status == 'skipped':
        return '跳过（无待扫描文件）'
    return status or ''


def _format_dist(distribution, order):
    return '，'.join('%s=%s' % (key, distribution.get(key, 0)) for key in order)


def print_report(report, as_json=False):
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    print('== 全国政策数据接入检查（%s）==' % report['version'])
    print('模式            %s' % _mode_text(report['mode']))
    if report.get('intake_dir'):
        print('扫描目录        %s' % report['intake_dir'])
    print('数据库          %s' % _db_text(report.get('db')))
    print('文件数          %s' % report['files_total'])
    print('行数            %s（成功 %s / 错误 %s）'
          % (report['rows_total'], report['rows_ok'], report['rows_error']))
    print('层级分布        %s' % _format_dist(report['level_distribution'], LEVEL_ORDER))
    print('地区分布        %s' % _format_dist(report['region_distribution'], REGION_ORDER))
    print('覆盖省份        %s（%s）'
          % (report['covered_provinces'],
             '、'.join(report['provinces']) if report['provinces'] else '无'))
    print('重复政策编号    %s' % report['duplicate_codes'])
    print('日期冲突        %s' % report['date_conflicts'])
    if report.get('actions'):
        print('库内动作        %s' % json.dumps(report['actions'], ensure_ascii=False,
                                                sort_keys=True))
    if report.get('db_preview'):
        preview = report['db_preview']
        print('库内校验        invalid=%s conflict=%s 将变更=%s'
              % (preview.get('invalid', 0), preview.get('conflict', 0),
                 preview.get('changed', 0)))
    if report['files']:
        print('')
        print('文件明细：')
        for item in report['files']:
            print('  %-28s 行 %-5s 成功 %-5s 错误 %-5s'
                  % (item['name'], item['rows_total'], item['rows_ok'],
                     item['rows_error']))
    for item in report.get('file_errors') or []:
        print('  读取失败 %s：%s' % (item.get('file'), item.get('error')))
    if report.get('errors'):
        print('')
        print('错误：')
        for item in report['errors'][:50]:
            location = item.get('file') or ''
            if item.get('sheet'):
                location += '[%s]' % item['sheet']
            print('  %s:%s %s %s' % (location, item.get('row_no'),
                                      item.get('policy_code') or '',
                                      '；'.join(item.get('errors') or [])))
    if report.get('unresolved'):
        print('')
        print('待核地区（仅提醒，未丢弃）：')
        for item in report['unresolved'][:20]:
            print('  %s:%s %s' % (item.get('file'), item.get('row_no'),
                                  item.get('raw_text')))
    if report.get('warnings'):
        print('')
        print('提醒（前 %d 条）：' % min(len(report['warnings']), 20))
        for item in report['warnings'][:20]:
            print('  %s:%s %s' % (item.get('file'), item.get('row_no'),
                                  '；'.join(item.get('warnings') or [])))
    for item in report.get('applied') or []:
        print('已提交文件      %s batch=%s 新增=%s 更新=%s 无变化=%s'
              % (item.get('file'), item.get('batch_id'), item.get('created'),
                 item.get('updated'), item.get('noop')))
    print('')
    print(report.get('note') or '未写库')


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def load_env(path=None):
    path = path or os.environ.get('POLICY_ENV_FILE') or os.path.join(BASE, 'deploy', 'web.env')
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip())


def _parser():
    parser = argparse.ArgumentParser(
        description='全国政策数据接入检查（默认 dry-run，不写库）')
    parser.add_argument('--file', action='append', default=None,
                        help='待检查文件，可重复：.csv / .xlsx / .json')
    parser.add_argument('--dir', default=None,
                        help='扫描目录（默认 NATIONAL_INTAKE_DIR 或 data/incoming）')
    parser.add_argument('--source', default=None, help='批次来源标识（默认 national-intake）')
    parser.add_argument('--operator', default=None,
                        help='操作人（默认 POLICY_IMPORT_OPERATOR 或 national-intake）')
    parser.add_argument('--dry-run', action='store_true', help='只读检查（默认行为）')
    parser.add_argument('--offline', action='store_true', help='完全不连库，只做离线统计')
    parser.add_argument('--apply', action='store_true',
                        help='写库（需要 POLICY_IMPORT=1）')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    parser.add_argument('--env-file', default=None, help='env 文件路径（默认 deploy/web.env）')
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    load_env(args.env_file)

    try:
        from policy_core import policy_import
        from policy_core import policy_scope
    except Exception as exc:
        print('ERROR: 无法加载 policy_import：%s: %s' % (type(exc).__name__, exc),
              file=sys.stderr)
        return 1

    offline = bool(args.offline)
    apply_mode = bool(args.apply)
    if args.dir and args.file:
        print('ERROR: --dir 与 --file 不能同时使用', file=sys.stderr)
        return 2
    if offline and apply_mode:
        print('ERROR: --offline 与 --apply 不能同时使用', file=sys.stderr)
        return 2
    if apply_mode and not policy_import.enabled():
        print('POLICY_IMPORT 未开启（默认 0）；--apply 需要显式设置 POLICY_IMPORT=1。',
              file=sys.stderr)
        return 2

    source = args.source or os.environ.get('NATIONAL_INTAKE_SOURCE') or 'national-intake'
    operator = (args.operator or os.environ.get('POLICY_IMPORT_OPERATOR')
                or 'national-intake')

    try:
        paths, scan_dir, scan_error = _scan(args)
    except Exception as exc:
        print('ERROR: 扫描失败：%s: %s' % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    if scan_error:
        print('ERROR: %s' % scan_error, file=sys.stderr)
        return 2

    if not paths:
        report = _assemble_report([], [], scan_dir)
        if offline:
            report['mode'] = 'offline'
            report['db'] = {'status': 'offline', 'reason': '--offline'}
        elif apply_mode:
            report['mode'] = 'apply'
            report['db'] = {'status': 'skipped', 'reason': '无待扫描文件'}
        print_report(report, as_json=args.json)
        return 0

    file_inputs = []
    read_errors = []
    for path in paths:
        try:
            records, warnings = read_records(path)
        except Exception as exc:
            read_errors.append({'file': os.path.basename(path), 'path': path,
                                'error': '%s: %s' % (type(exc).__name__, exc)})
            continue
        mapped_rows = []
        row_nos = []
        for record in records:
            raw, _map_warnings = _map_to_import(record['values'])
            mapped_rows.append(raw)
            row_nos.append(record['row_no'])
        file_inputs.append({'name': os.path.basename(path), 'path': path,
                            'records': records, 'warnings': warnings,
                            'mapped_rows': mapped_rows, 'row_nos': row_nos})

    analyzed = []
    for file_input in file_inputs:
        try:
            analyzed.append(_analyze_file(file_input, policy_import, policy_scope))
        except Exception as exc:
            read_errors.append({'file': file_input['name'], 'path': file_input['path'],
                                'error': '分析失败：%s: %s'
                                         % (type(exc).__name__, exc)})

    report = _assemble_report(analyzed, read_errors, scan_dir)

    if offline:
        report['mode'] = 'offline'
        report['db'] = {'status': 'offline', 'reason': '--offline'}
        report['note'] = '未写库'
        print_report(report, as_json=args.json)
        return 1 if _has_errors(report) else 0

    engine, db_error = _db_check(policy_import)
    if db_error:
        report['mode'] = 'apply' if apply_mode else 'dry_run'
        report['db'] = {'status': 'unavailable', 'reason': db_error}
        report['note'] = '未写库（数据库不可用，已降级为离线报告）'
        print_report(report, as_json=args.json)
        print('ERROR: 数据库不可用，已降级离线：%s' % db_error, file=sys.stderr)
        return 4
    report['db'] = {'status': 'ok'}

    if apply_mode:
        report['mode'] = 'apply'
        if _has_errors(report):
            report['note'] = '未写库（存在错误行，已拒绝写入）'
            print_report(report, as_json=args.json)
            print('ERROR: 存在错误行，--apply 已拒绝；请先修正后重试。', file=sys.stderr)
            return 1
        try:
            preview = _db_preview(policy_import, engine, file_inputs)
        except Exception as exc:
            report['db'] = {'status': 'unavailable',
                            'reason': '%s: %s' % (type(exc).__name__, str(exc)[:200])}
            report['note'] = '未写库（数据库不可用，已降级为离线报告）'
            print_report(report, as_json=args.json)
            print('ERROR: 数据库不可用，已降级离线：%s' % report['db']['reason'],
                  file=sys.stderr)
            return 4
        report['db_preview'] = {'actions': preview['actions'],
                                'invalid': len(preview['invalid']),
                                'conflict': len(preview['conflict']),
                                'changed': preview['changed']}
        if preview['invalid'] or preview['conflict']:
            report['db_errors'] = (preview['invalid'] + preview['conflict'])[
                :MAX_REPORT_ERRORS]
            report['note'] = '未写库（库内校验未通过，已拒绝写入）'
            print_report(report, as_json=args.json)
            print('ERROR: 库内校验未通过（invalid=%d conflict=%d），已拒绝写库。'
                  % (len(preview['invalid']), len(preview['conflict'])), file=sys.stderr)
            return 1

        applied, failure = _apply_files(policy_import, engine, file_inputs,
                                        source, operator)
        report['applied'] = applied
        report['write'] = bool(applied)
        if failure:
            report['note'] = ('部分批次已写库，后续文件失败：%s' % failure if applied
                              else '未写库（提交失败）')
            print_report(report, as_json=args.json)
            print('ERROR: %s' % failure, file=sys.stderr)
            return 1
        report['note'] = '已写库'
        print_report(report, as_json=args.json)
        return 0

    # 默认 dry-run：只读 diff，不写任何表。
    report['mode'] = 'dry_run'
    try:
        preview = _db_preview(policy_import, engine, file_inputs)
    except Exception as exc:
        report['db'] = {'status': 'unavailable',
                        'reason': '%s: %s' % (type(exc).__name__, str(exc)[:200])}
        report['note'] = '未写库（数据库不可用，已降级为离线报告）'
        print_report(report, as_json=args.json)
        print('ERROR: 数据库不可用，已降级离线：%s' % report['db']['reason'],
              file=sys.stderr)
        return 4
    report['actions'] = preview['actions']
    report['db_preview'] = {'actions': preview['actions'],
                            'invalid': len(preview['invalid']),
                            'conflict': len(preview['conflict']),
                            'changed': preview['changed']}
    if preview['invalid'] or preview['conflict']:
        report['db_errors'] = (preview['invalid'] + preview['conflict'])[
            :MAX_REPORT_ERRORS]
    report['note'] = '未写库'
    print_report(report, as_json=args.json)
    if _has_errors(report) or preview['invalid'] or preview['conflict']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
