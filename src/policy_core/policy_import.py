# -*- coding: utf-8 -*-
"""政策批量导入管线（全国化 P0 / T19）。

流程：staging（import_batch + import_row 保留原始行）
→ validate（字段、地区归一、日期；与库内 diff 出 create/update/noop/conflict）
→ commit（单事务按 policy_code 幂等 upsert + policy_version 快照 + policy_scope + outbox）
→ rollback（按批次版本链恢复上一版 / 删除本批新建）。

设计约束（与 17/18/19 审计、20 设计方案一致）：
- 开关 POLICY_IMPORT 默认 0；关闭时 CLI 拒绝执行，库函数仅供显式调用方使用；
- FP=0：地区无法解析只记 warning 不阻塞（保留待核）；解析失败、同批次重号、
  库内同号多行一律隔离不落库；回滚发现批次后政策又被改过则跳过并报告，不硬还原；
- 写操作只写业务表 + outbox，不直接碰向量/规则/KG；outbox 消费者负责后续同步；
- 不依赖 Flask/SQLAlchemy ORM，raw SQL 同时兼容 sqlite（单测）与 Kingbase/PG（生产）。

注意：Kingbase 把 level 视作保留字，本模块所有原生 SQL 的列名都加双引号。
"""
import csv
import io
import json
import os
import re
import uuid
from datetime import date, datetime

from sqlalchemy import bindparam, create_engine, text

try:  # 金仓版本串补丁，必须在建 engine 前导入
    import config  # noqa: F401
except Exception:  # 独立脚本环境没有 config
    pass

from . import admin_division, policy_scope

VERSION = 'policy-import-1'

BATCH_STATUSES = ('staging', 'validated', 'committed', 'rolled_back', 'failed')
ROW_STATUSES = ('pending', 'ok', 'invalid', 'conflict')
ACTIONS = ('create', 'update', 'noop', 'conflict')

# 与 T18 outbox 的事件类型保持同名；T18 模块未就绪时用本文件的兼容降级表。
OUTBOX_EVENTS = ('policy.vector_rebuild', 'policy.rule_invalidate', 'policy.kg_sync')

MAX_ROWS = 20000
MAX_BUFFER = 2000

LEVEL_ALIASES = {
    'C': 'C', 'S': 'S', 'U': 'U', 'X': 'X',
    '国家': 'C', '国家级': 'C', '全国': 'C', '全国级': 'C',
    '省': 'S', '省级': 'S', '省級': 'S',
    '市': 'U', '市级': 'U', '市級': 'U', '地市': 'U', '地市级': 'U',
    '县': 'X', '县级': 'X', '县区': 'X', '县区级': 'X', '区级': 'X', '区县': 'X',
}

STATUS_ALIASES = {
    '在用': '在用', '启用': '在用', '正常': '在用', '有效': '在用', '现行': '在用',
    '停用': '停用', '废止': '停用', '失效': '停用', '已废止': '停用', '已失效': '停用',
}

# 政策主表可导入字段（不含主键/管理字段）。列名与 models.Policy 对齐。
POLICY_FIELDS = (
    'policy_name', 'level', 'category1', 'category2', 'category3', 'attr',
    'doc_ref', 'regions', 'target', 'conditions', 'benefit', 'process',
    'contact', 'effect_date', 'expire_date', 'status', 'source', 'level_note',
)

# 明细字段（policy_detail 里可导入的列）
DETAIL_FIELDS = ('region', 'benefit', 'process', 'contact', 'phone', 'target',
                 'conditions', 'effect_date', 'expire_date', 'unit', 'cycle',
                 'currency')

_DETAIL_SIG_FIELDS = ('region', 'benefit', 'process', 'contact', 'phone',
                      'target', 'conditions', 'effect_date', 'expire_date')

# 表头别名：兼容现有导出列名、表格导入列名与英文字段名。
_ALIASES = {
    'policy_code': ('政策编号', '编号', '政策编码', '政策代码', '代码', 'policy_code',
                    'policycode', 'code'),
    'policy_name': ('政策名称', '名称', '政策名', '标题', 'policy_name', 'policyname',
                    'name', 'title'),
    'level': ('层级', '级别', '政策层级', 'level'),
    'category1': ('一级分类', '一级类别', '分类', '类别', 'category1', 'category'),
    'category2': ('二级分类', '二级类别', 'category2'),
    'category3': ('三级分类', '三级类别', 'category3'),
    'attr': ('属性', '政策属性', 'attr', 'attribute'),
    'regions': ('适用地区', '地区', '区域', '适用范围', '省市区', 'regions', 'region'),
    'doc_ref': ('文号', '文件文号', '发文字号', '政策文号', 'doc_ref', 'docref'),
    'target': ('适用对象', '目标对象', '对象', '目标', 'target', 'object'),
    'conditions': ('申请条件', '条件', '适用条件', 'conditions', 'condition'),
    'benefit': ('补贴标准', '待遇', '补贴金额', '补助标准', '标准', 'benefit', 'subsidy'),
    'process': ('办理流程', '流程', '申请流程', 'process', 'procedure'),
    'contact': ('联系方式', '联系人', '咨询电话', 'contact'),
    'phone': ('电话', '联系电话', 'phone', 'tel'),
    'effect_date': ('生效日期', '开始日期', '实施日期', 'effect_date', 'start_date',
                    'effective_date'),
    'expire_date': ('失效日期', '截止日期', '结束日期', 'expire_date', 'end_date'),
    'status': ('状态', 'status'),
    'source': ('来源', '文件来源', 'source'),
    'level_note': ('层级说明', '备注说明', 'level_note'),
    'unit': ('单位', '计量单位', 'unit'),
    'cycle': ('周期', '发放周期', 'cycle'),
    'currency': ('币种', '货币', 'currency'),
    'details': ('明细', '政策明细', '明细列表', 'details', 'detail'),
}
_ALIAS_LOOKUP = {}


def _norm_header(value):
    """表头/列名归一：去 BOM、去空白、英文转小写。"""
    text_value = str(value or '').replace('\ufeff', '')
    text_value = re.sub(r'\s+', '', text_value).strip()
    return text_value.lower()


for _canon, _names in _ALIASES.items():
    for _name in _names:
        _ALIAS_LOOKUP[_norm_header(_name)] = _canon


# ---------------------------------------------------------------------------
# 连接与 schema
# ---------------------------------------------------------------------------
_ENGINE = None


def database_url():
    explicit = os.environ.get('ADMIN_DB_URL')
    if explicit:
        return explicit
    try:
        url = config.DATABASE_URL
        if url.startswith('postgresql'):
            return url
    except Exception:
        pass
    return 'sqlite:///' + os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       '..', '..', 'data', 'policy_db', 'smart_policy.db')


def engine(url=None):
    global _ENGINE
    if url:
        return create_engine(url, pool_pre_ping=True)
    if _ENGINE is None:
        _ENGINE = create_engine(database_url(), pool_pre_ping=True, pool_size=2,
                                max_overflow=1)
    return _ENGINE


def enabled():
    """POLICY_IMPORT 开关（默认 0）。API/CLI 入口必须检查。"""
    return os.environ.get('POLICY_IMPORT', '0').strip().lower() in ('1', 'true', 'yes', 'on')


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS "import_batch" (
        "id"         varchar(64) NOT NULL PRIMARY KEY,
        "source"     varchar(128),
        "file_name"  varchar(255),
        "total"      integer NOT NULL DEFAULT 0,
        "valid"      integer NOT NULL DEFAULT 0,
        "invalid"    integer NOT NULL DEFAULT 0,
        "status"     varchar(16) NOT NULL DEFAULT 'staging',
        "dry_run"    integer NOT NULL DEFAULT 1,
        "operator"   varchar(64),
        "created_at" varchar(32),
        "committed_at" varchar(32)
    )""",
    """CREATE TABLE IF NOT EXISTS "import_row" (
        "id"          varchar(64) NOT NULL PRIMARY KEY,
        "batch_id"    varchar(64) NOT NULL,
        "row_no"      integer NOT NULL,
        "raw"         text,
        "status"      varchar(16) NOT NULL DEFAULT 'pending',
        "errors"      text,
        "action"      varchar(16),
        "policy_code" varchar(64)
    )""",
    'CREATE INDEX IF NOT EXISTS "import_row_batch_idx" ON "import_row" ("batch_id")',
    'CREATE INDEX IF NOT EXISTS "import_row_code_idx" ON "import_row" ("policy_code")',
]


def ensure_schema(conn=None, url=None):
    own = conn is None
    if own:
        conn = engine(url).begin()
    try:
        for statement in _SCHEMA:
            conn.execute(text(statement))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 通用 SQL 辅助（双引号列名，规避 Kingbase 保留字）
# ---------------------------------------------------------------------------
def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _table_exists(conn, name):
    if conn.dialect.name == 'sqlite':
        row = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
                           {'n': name}).first()
    else:
        row = conn.execute(text("SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema='public' AND table_name=:n"),
                           {'n': name}).first()
    return bool(row)


def _insert(conn, table, values):
    columns = list(values.keys())
    names, placeholders, params = [], [], {}
    for index, column in enumerate(columns):
        names.append('"%s"' % column.replace('"', '""'))
        param = 'p%d' % index
        placeholders.append(':' + param)
        params[param] = values[column]
    sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (table, ', '.join(names), ', '.join(placeholders))
    return conn.execute(text(sql), params)


def _update(conn, table, values, where):
    params, sets, conditions = {}, [], []
    for index, (column, value) in enumerate(values.items()):
        param = 'p%d' % index
        sets.append('"%s" = :%s' % (column.replace('"', '""'), param))
        params[param] = value
    for index, (column, value) in enumerate(where.items()):
        param = 'w%d' % index
        conditions.append('"%s" = :%s' % (column.replace('"', '""'), param))
        params[param] = value
    sql = 'UPDATE "%s" SET %s WHERE %s' % (table, ', '.join(sets), ' AND '.join(conditions))
    conn.execute(text(sql), params)


def _supports_for_update(conn):
    return conn.dialect.name != 'sqlite'


def _advisory_lock(conn, key=2026101204):
    """PG/Kingbase 事务级 advisory lock：同一时刻只允许一个导入提交。"""
    if conn.dialect.name == 'postgresql':
        conn.execute(text('SELECT pg_advisory_xact_lock(%d)' % int(key)))


# ---------------------------------------------------------------------------
# 输入解析：CSV / XLSX / JSON
# ---------------------------------------------------------------------------
def _cell_text(value):
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _canonical_header(value):
    return _ALIAS_LOOKUP.get(_norm_header(value), '')


def _rows_from_matrix(matrix):
    """表格矩阵 → 行字典列表；跳过表头之前的标题行。"""
    header = None
    rows = []
    for raw_row in matrix:
        cells = list(raw_row or [])
        canonical = [_canonical_header(cell) for cell in cells]
        if header is None:
            if 'policy_name' in canonical or 'policy_code' in canonical:
                header = canonical
            continue
        if all(not _cell_text(cell) for cell in cells):
            continue
        item = {}
        for index, key in enumerate(header):
            if not key or index >= len(cells):
                continue
            value = cells[index]
            if isinstance(value, (list, dict)):
                item[key] = value
            else:
                text_value = _cell_text(value)
                if text_value != '':
                    item[key] = text_value
        if item:
            rows.append(item)
    if header is None:
        raise ValueError('表格缺少表头：至少需要“政策名称”或“政策编号”列')
    return rows


def parse_csv(path):
    with open(path, 'rb') as handle:
        raw = handle.read()
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('gb18030')
    matrix = list(csv.reader(io.StringIO(content)))
    return _rows_from_matrix(matrix)


def parse_xlsx(path):
    import openpyxl
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = []
        for sheet in workbook:
            matrix = [list(row) for row in sheet.iter_rows(values_only=True)]
            if any(any(cell not in (None, '') for cell in row) for row in matrix):
                rows.extend(_rows_from_matrix(matrix))
        return rows
    finally:
        workbook.close()


def parse_json(path):
    with open(path, 'r', encoding='utf-8-sig') as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get('rows'), list):
            rows = payload['rows']
        else:
            rows = [payload]
    else:
        raise ValueError('JSON 顶层必须是对象或对象数组')
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError('JSON 每一行必须是对象')
    return rows


def load_rows(path):
    """读取导入文件，返回原始行字典列表（列名已归一为规范字段名）。"""
    if not os.path.exists(path):
        raise ValueError('文件不存在：%s' % path)
    suffix = os.path.splitext(path)[1].lower()
    if suffix == '.csv':
        rows = parse_csv(path)
    elif suffix == '.xlsx':
        rows = parse_xlsx(path)
    elif suffix == '.json':
        rows = parse_json(path)
    else:
        raise ValueError('支持 CSV / XLSX / JSON；其他格式请另存后导入')
    if len(rows) > MAX_ROWS:
        raise ValueError('单批最多 %d 行，当前 %d 行，请拆分后导入' % (MAX_ROWS, len(rows)))
    return rows


def normalize_row(raw):
    """把一行原始键值归一成规范字段；空值不写入（保留“未提供”语义）。"""
    if not isinstance(raw, dict):
        raise ValueError('行数据必须是对象')
    normalized = {}
    for key, value in raw.items():
        canonical = _canonical_header(key)
        if not canonical:
            continue
        if canonical == 'details':
            normalized['details'] = _parse_details(value)
            continue
        if isinstance(value, (list, tuple)):
            text_value = '、'.join(_cell_text(item) for item in value if _cell_text(item))
        elif isinstance(value, dict):
            text_value = json.dumps(value, ensure_ascii=False)
        else:
            text_value = _cell_text(value)
        if text_value != '':
            normalized[canonical] = text_value
    return normalized


def _parse_details(value):
    if isinstance(value, dict):
        value = [value]
    elif isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError('“明细”列不是合法 JSON 数组')
        if isinstance(value, dict):
            value = [value]
    if not isinstance(value, list):
        raise ValueError('“明细”列必须是对象数组')
    details = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('“明细”列的每个元素必须是对象')
        detail = {}
        for key, cell in item.items():
            canonical = _canonical_header(key)
            if canonical == 'regions':
                canonical = 'region'
            if canonical in DETAIL_FIELDS:
                text_value = _cell_text(cell)
                if text_value != '':
                    detail[canonical] = text_value
        details.append(detail)
    return details


def _parse_date(value):
    """宽松解析常见日期写法，返回 YYYY-MM-DD；无法解析返回 None。"""
    text_value = _cell_text(value)
    if not text_value:
        return None
    candidate = text_value.split(' ')[0].split('T')[0]
    candidate = candidate.replace('年', '-').replace('月', '-').replace('日', '')
    candidate = candidate.replace('/', '-').replace('.', '-')
    if re.fullmatch(r'\d{8}', candidate):
        candidate = '%s-%s-%s' % (candidate[:4], candidate[4:6], candidate[6:])
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _level_code(value):
    text_value = _cell_text(value)
    if not text_value:
        return None
    return LEVEL_ALIASES.get(text_value) or LEVEL_ALIASES.get(text_value.upper())


def _status_code(value):
    text_value = _cell_text(value)
    if not text_value:
        return None
    return STATUS_ALIASES.get(text_value)


# ---------------------------------------------------------------------------
# 校验与 dry-run 差异（只读）
# ---------------------------------------------------------------------------
def _validate_row(raw, row_no):
    result = {'row_no': row_no, 'policy_code': '', 'status': 'ok', 'action': None,
              'errors': [], 'warnings': [], 'changed_fields': [], 'unresolved': [],
              'normalized': {}}
    if not isinstance(raw, dict):
        result['status'] = 'invalid'
        result['errors'].append('行数据必须是对象')
        return result
    try:
        normalized = normalize_row(raw)
    except ValueError as exc:
        result['status'] = 'invalid'
        result['errors'].append(str(exc))
        return result
    result['normalized'] = normalized
    errors, warnings = result['errors'], result['warnings']

    code = _cell_text(normalized.get('policy_code')).upper()
    normalized['policy_code'] = code
    result['policy_code'] = code
    if not code:
        errors.append('政策编号缺失')
    elif len(code) > 64:
        errors.append('政策编号超过 64 字符')
    elif not re.fullmatch(r'[A-Z0-9][A-Z0-9_.\-/:]*', code):
        errors.append('政策编号含非法字符：%s' % code)

    name = _cell_text(normalized.get('policy_name'))
    if not name:
        errors.append('政策名称缺失')
    elif name in ('无', '措施/项目', '政策名称') or name.startswith('（待识别') \
            or name.startswith('（待补充'):
        errors.append('政策名称是模板或占位值，不能导入')
    elif len(name) < 2:
        errors.append('政策名称过短')
    else:
        normalized['policy_name'] = name

    level_raw = _cell_text(normalized.get('level'))
    level = _level_code(level_raw)
    if not level_raw:
        errors.append('层级缺失')
    elif not level:
        errors.append('层级必须为 C/S/U/X（或国家/省/市/县级），当前：%s' % level_raw)
    else:
        normalized['level'] = level

    if 'status' in normalized:
        status = _status_code(normalized['status'])
        if not status:
            errors.append('状态只支持 在用/停用，当前：%s' % normalized['status'])
        else:
            normalized['status'] = status

    for key in ('effect_date', 'expire_date'):
        if normalized.get(key):
            parsed = _parse_date(normalized[key])
            if not parsed:
                errors.append('%s 不是可解析日期：%s' % (key, normalized[key]))
            else:
                normalized[key] = parsed
    if not errors:
        effect, expire = normalized.get('effect_date'), normalized.get('expire_date')
        if effect and expire and expire < effect:
            errors.append('失效日期不能早于生效日期')

    regions = _cell_text(normalized.get('regions'))
    if not regions:
        if normalized.get('level') == 'C':
            normalized['regions'] = '全国'
            warnings.append('未填适用地区，国家级政策按“全国”处理')
        else:
            errors.append('适用地区缺失（省级/市级/县级政策必须给出地区）')
    else:
        normalized['regions'] = regions

    if 'details' in normalized:
        details = []
        for index, item in enumerate(normalized['details'], 1):
            if not (item.get('conditions') or item.get('target')):
                errors.append('明细%d 缺少适用对象或申请条件' % index)
            details.append(item)
        normalized['details'] = details
        normalized['details_provided'] = True
    else:
        single = {field: normalized[field] for field in
                  ('target', 'conditions', 'benefit', 'process', 'contact', 'phone')
                  if normalized.get(field)}
        if single:
            normalized['details'] = [single]
            normalized['details_provided'] = True
        else:
            normalized['details_provided'] = False
            warnings.append('未提供政策明细（申请条件/适用对象），只写政策主表')

    # 地区归一：unresolved 只记 warning，绝不静默丢弃（保留待核）。
    if not errors:
        scope_details = [(index + 1, item.get('region') or '')
                         for index, item in enumerate(normalized.get('details') or [])]
        try:
            scope_rows = policy_scope.rows_for(code, normalized.get('regions') or '',
                                               scope_details)
        except Exception as exc:  # 区划数据异常不能让整批失败
            scope_rows = []
            warnings.append('地区归一失败（%s），保留原始文本待核' % type(exc).__name__)
        if not scope_rows:
            errors.append('适用地区无法归一（未产生任何地区范围）')
        else:
            for row in scope_rows:
                if row['level'] == admin_division.UNKNOWN:
                    result['unresolved'].append({'raw_text': row['raw_text'],
                                                 'source_field': row['source_field']})

    if errors:
        result['status'] = 'invalid'
    return result


def _select_policy_columns():
    return ('policy_id', 'policy_code', 'policy_name', '"level"', 'category1',
            'category2', 'category3', 'attr', 'regions', 'doc_ref', 'target',
            'conditions', 'benefit', 'process', 'contact', 'effect_date',
            'expire_date', 'status', 'cur_version', 'source', 'level_note',
            'updated_at', 'updated_by')


def _find_policies_map(conn, codes):
    codes = [code for code in codes if code]
    if not codes:
        return {}
    statement = text(
        'SELECT %s FROM "policy" WHERE "policy_code" IN :codes'
        % ', '.join(_select_policy_columns())
    ).bindparams(bindparam('codes', expanding=True))
    rows = conn.execute(statement, {'codes': codes}).mappings().all()
    grouped = {}
    for row in rows:
        item = dict(row)
        grouped.setdefault(item['policy_code'], []).append(item)
    return grouped


def _existing_details(conn, policy_id):
    return [dict(row) for row in conn.execute(
        text('SELECT * FROM "policy_detail" WHERE "policy_id" = :pid ORDER BY "detail_id"'),
        {'pid': policy_id}).mappings().all()]


def _detail_signature(details):
    signature = []
    for item in details or []:
        signature.append(tuple(_cell_text(item.get(field)) for field in _DETAIL_SIG_FIELDS))
    return signature


def _effective_details(normalized):
    """把行级兜底（地区/待遇/流程/联系方式）应用到每条明细，供落库与 diff 共用。

    与 policy_ingest.write_details 的语义一致：明细缺省字段回落政策主表值，
    这样“未提供明细”与“按政策主表合成的单条明细”在比较时不会误判为变更。
    """
    effective = []
    for item in normalized.get('details') or []:
        effective.append({
            'region': item.get('region') or normalized.get('regions') or '',
            'benefit': item.get('benefit') or normalized.get('benefit') or '',
            'process': item.get('process') or normalized.get('process') or '',
            'contact': item.get('contact') or normalized.get('contact') or '',
            'phone': item.get('phone') or '',
            'target': item.get('target') or '',
            'conditions': item.get('conditions') or '',
            'effect_date': item.get('effect_date') or normalized.get('effect_date') or '',
            'expire_date': item.get('expire_date') or normalized.get('expire_date') or '',
            'unit': item.get('unit') or '',
            'cycle': item.get('cycle') or '',
            'currency': item.get('currency') or '',
        })
    return effective


def _changed_fields(existing, old_details, normalized):
    changed = []
    for field in POLICY_FIELDS:
        if field not in normalized:
            continue
        old_value = _cell_text(existing.get(field))
        new_value = _cell_text(normalized.get(field))
        if old_value != new_value:
            changed.append(field)
    if normalized.get('details_provided'):
        if _detail_signature(old_details) != _detail_signature(_effective_details(normalized)):
            changed.append('details')
    return changed


def _plan_rows(conn, raw_rows, row_nos=None):
    """对一批原始行做校验 + 库内 diff（只读），返回逐行结果。"""
    results = []
    for index, raw in enumerate(raw_rows):
        row_no = row_nos[index] if row_nos else index + 1
        results.append(_validate_row(raw, row_no))

    # 同批次重复政策编号：全部隔离为 conflict，不猜测保留哪一行。
    grouped = {}
    for item in results:
        if item['status'] == 'ok':
            grouped.setdefault(item['normalized']['policy_code'], []).append(item)
    for code, group in grouped.items():
        if len(group) > 1:
            for item in group:
                item['status'] = 'conflict'
                item['action'] = 'conflict'
                item['errors'].append('同批次重复政策编号 %s（共 %d 行），整组隔离' % (code, len(group)))

    valid_codes = [item['normalized']['policy_code'] for item in results
                   if item['status'] == 'ok']
    existing_map = _find_policies_map(conn, valid_codes)
    details_cache = {}
    for item in results:
        if item['status'] != 'ok':
            continue
        normalized = item['normalized']
        code = normalized['policy_code']
        existing_rows = existing_map.get(code) or []
        if len(existing_rows) > 1:
            item['status'] = 'invalid'
            item['errors'].append('库中存在 %d 条同编号政策，需先人工合并后再导入' % len(existing_rows))
            continue
        if not existing_rows:
            item['action'] = 'create'
            continue
        existing = existing_rows[0]
        policy_id = existing['policy_id']
        if policy_id not in details_cache:
            details_cache[policy_id] = _existing_details(conn, policy_id)
        changed = _changed_fields(existing, details_cache[policy_id], normalized)
        item['changed_fields'] = changed
        item['action'] = 'update' if changed else 'noop'
    return results


def _build_report(results, extra=None):
    ok = [item for item in results if item['status'] == 'ok']
    invalid = [item for item in results if item['status'] == 'invalid']
    conflict = [item for item in results if item['status'] == 'conflict']
    actions = {action: 0 for action in ACTIONS}
    for item in results:
        if item.get('action') in actions:
            actions[item['action']] += 1
    report = {
        'version': VERSION,
        'total': len(results),
        'valid': len(ok),
        'invalid': len(invalid),
        'conflict': len(conflict),
        'actions': actions,
        'errors': [{'row_no': item['row_no'], 'policy_code': item['policy_code'],
                    'errors': item['errors'][:10]}
                   for item in results if item['errors']][:20],
        'warnings': [{'row_no': item['row_no'], 'policy_code': item['policy_code'],
                      'warnings': item['warnings'][:10]}
                     for item in results if item['warnings']][:20],
        'unresolved': [{'row_no': item['row_no'], 'policy_code': item['policy_code'],
                        'raw_text': unresolved['raw_text'],
                        'source_field': unresolved.get('source_field')}
                       for item in results for unresolved in item['unresolved']][:20],
        'changed': [{'row_no': item['row_no'], 'policy_code': item['policy_code'],
                     'changed_fields': item['changed_fields']}
                    for item in results if item['changed_fields']][:20],
    }
    if extra:
        report.update(extra)
    return report


def preview(conn, rows, source='', file_name=''):
    """dry-run：只校验 + diff，不写任何库表（CLI 默认路径）。"""
    if len(rows) > MAX_ROWS:
        raise ValueError('单批最多 %d 行，当前 %d 行，请拆分后导入' % (MAX_ROWS, len(rows)))
    results = _plan_rows(conn, rows)
    return _build_report(results, {'mode': 'dry_run', 'source': source,
                                   'file_name': file_name, 'dry_run': True})


# ---------------------------------------------------------------------------
# staging / validate
# ---------------------------------------------------------------------------
def _load_batch(conn, batch_id, lock=False):
    sql = ('SELECT * FROM "import_batch" WHERE "id" = :batch_id')
    if lock and _supports_for_update(conn):
        sql += ' FOR UPDATE'
    row = conn.execute(text(sql), {'batch_id': batch_id}).mappings().first()
    return dict(row) if row else None


def _load_rows(conn, batch_id):
    return [dict(row) for row in conn.execute(
        text('SELECT * FROM "import_row" WHERE "batch_id" = :batch_id ORDER BY "row_no"'),
        {'batch_id': batch_id}).mappings().all()]


def create_batch(conn, rows, source='', file_name='', operator='', dry_run=False):
    """把原始行写入 staging（import_batch/import_row），不碰政策表。"""
    if len(rows) > MAX_ROWS:
        raise ValueError('单批最多 %d 行，当前 %d 行，请拆分后导入' % (MAX_ROWS, len(rows)))
    ensure_schema(conn)
    batch_id = uuid.uuid4().hex
    _insert(conn, 'import_batch', {
        'id': batch_id, 'source': _cell_text(source), 'file_name': _cell_text(file_name),
        'total': len(rows), 'valid': 0, 'invalid': 0, 'status': 'staging',
        'dry_run': 1 if dry_run else 0, 'operator': _cell_text(operator),
        'created_at': _now(), 'committed_at': None,
    })
    for index, row in enumerate(rows, 1):
        try:
            raw_text = json.dumps(row, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw_text = json.dumps({'__error__': '无法序列化的行'}, ensure_ascii=False)
        _insert(conn, 'import_row', {
            'id': uuid.uuid4().hex, 'batch_id': batch_id, 'row_no': index,
            'raw': raw_text, 'status': 'pending', 'errors': None, 'action': None,
            'policy_code': None,
        })
    return {'batch_id': batch_id, 'total': len(rows), 'dry_run': bool(dry_run),
            'status': 'staging'}


def _row_info_json(item):
    return json.dumps({'errors': item['errors'], 'warnings': item['warnings'],
                       'changed_fields': item['changed_fields'],
                       'unresolved': item['unresolved']}, ensure_ascii=False)


def _persist_plan(conn, results, rows):
    row_ids = {row['row_no']: row['id'] for row in rows}
    for item in results:
        row_status = 'ok' if item['status'] == 'ok' else item['status']
        _update(conn, 'import_row', {
            'status': row_status,
            'action': item.get('action'),
            'policy_code': item.get('policy_code') or None,
            'errors': _row_info_json(item),
        }, {'id': row_ids[item['row_no']]})


def _raw_rows_of(rows):
    raw_rows, row_nos = [], []
    for row in rows:
        try:
            raw = json.loads(row['raw'] or '{}')
        except (TypeError, ValueError):
            raw = None
        raw_rows.append(raw)
        row_nos.append(row['row_no'])
    return raw_rows, row_nos


def validate_batch(conn, batch_id):
    """校验批次并把 create/update/noop/conflict 计划持久化到 import_row。"""
    ensure_schema(conn)
    batch = _load_batch(conn, batch_id)
    if not batch:
        raise ValueError('批次不存在：%s' % batch_id)
    if batch['status'] in ('committed', 'rolled_back'):
        raise ValueError('批次状态为 %s，不能重新校验' % batch['status'])
    rows = _load_rows(conn, batch_id)
    raw_rows, row_nos = _raw_rows_of(rows)
    results = _plan_rows(conn, raw_rows, row_nos)
    _persist_plan(conn, results, rows)
    valid = sum(1 for item in results if item['status'] == 'ok')
    _update(conn, 'import_batch', {
        'total': len(results), 'valid': valid, 'invalid': len(results) - valid,
        'status': 'validated',
    }, {'id': batch_id})
    report = _build_report(results, {
        'batch_id': batch_id, 'mode': 'validate', 'status': 'validated',
        'dry_run': bool(int(batch['dry_run'])), 'source': batch.get('source'),
        'file_name': batch.get('file_name'),
    })
    return report


# ---------------------------------------------------------------------------
# commit：单事务 upsert + 版本 + scope + outbox
# ---------------------------------------------------------------------------
def _version_next(current):
    match = re.match(r'v(\d+)', _cell_text(current) or 'v1')
    return 'v%d' % ((int(match.group(1)) + 1) if match else 1)


def _snapshot(conn, policy_id):
    policy = conn.execute(text('SELECT * FROM "policy" WHERE "policy_id" = :pid'),
                          {'pid': policy_id}).mappings().first()
    if not policy:
        return None
    snapshot = dict(policy)
    snapshot['details'] = _existing_details(conn, policy_id)
    return snapshot


def _detail_regions(conn, policy_id):
    return [(row['detail_id'], row['region'] or '') for row in conn.execute(
        text('SELECT "detail_id", "region" FROM "policy_detail" WHERE "policy_id" = :pid '
             'ORDER BY "detail_id"'), {'pid': policy_id}).mappings().all()]


def _replace_scope(conn, policy_code, regions, detail_regions):
    policy_scope.ensure_schema(conn)
    rows = policy_scope.rows_for(policy_code, regions or '', detail_regions)
    policy_scope.replace_policy(conn, policy_code, rows)
    return rows


def _detail_has_column(conn, name):
    """policy_detail 是否有该列（老库/最小测试库可能缺效期列）。

    MetaData 反射按连接缓存，避免逐条明细重复查询；反射失败按"有列"处理，
    让真正的写库错误暴露出来而不是被静默吞掉。
    """
    info = getattr(conn, 'info', None)
    if not isinstance(info, dict):
        return True
    columns = info.get('_import_detail_columns')
    if columns is None:
        try:
            from sqlalchemy import inspect as sa_inspect
            columns = {column['name'] for column in
                       sa_inspect(conn).get_columns('policy_detail')}
        except Exception:
            columns = set()
        info['_import_detail_columns'] = columns
    return name in columns


def _insert_details(conn, policy_id, normalized):
    for item in _effective_details(normalized):
        extra = {
            'doc': normalized.get('doc_ref') or '',
            'conditions': item.get('conditions') or '',
            'target': item.get('target') or '',
        }
        if any(item.get(key) for key in ('unit', 'cycle', 'currency')):
            extra['benefit_source'] = {key: item.get(key) or ''
                                       for key in ('unit', 'cycle', 'currency')}
        row = {
            'policy_id': policy_id,
            'region': item['region'],
            'benefit': item['benefit'],
            'process': item['process'],
            'contact': item['contact'],
            'phone': item['phone'],
            'target': item['target'],
            'conditions': item['conditions'],
            'extra': json.dumps(extra, ensure_ascii=False),
        }
        # 明细表有 effect_date/expire_date 列，但此前的导入没有落这两列，
        # 导致 diff 签名里明细效期永远为空 → 每次重导都被判成变更。
        for field in ('effect_date', 'expire_date'):
            if _detail_has_column(conn, field):
                row[field] = item.get(field) or ''
        _insert(conn, 'policy_detail', row)


def _enqueue_outbox(conn, event_type, payload):
    """优先复用 T18 的 outbox 模块；未就绪时写兼容降级表，保证导入可独立运行。"""
    try:
        from . import outbox as outbox_module
        if hasattr(outbox_module, 'enqueue'):
            if hasattr(outbox_module, 'ensure_schema'):
                outbox_module.ensure_schema(conn)
            outbox_module.enqueue(conn, event_type, payload)
            return
    except Exception:
        pass
    _ensure_outbox_fallback(conn)
    now = _now()
    _insert(conn, 'outbox', {
        'event_type': event_type,
        'payload': json.dumps(payload, ensure_ascii=False, default=str),
        'status': 'pending', 'attempts': 0, 'created_at': now, 'updated_at': now,
        'last_error': None,
    })


def _ensure_outbox_fallback(conn):
    if conn.dialect.name == 'sqlite':
        id_def = '"id" INTEGER PRIMARY KEY AUTOINCREMENT'
    else:
        id_def = '"id" BIGSERIAL PRIMARY KEY'
    conn.execute(text('''CREATE TABLE IF NOT EXISTS "outbox" (
        %s,
        "event_type" varchar(64) NOT NULL,
        "payload" text,
        "status" varchar(16) NOT NULL DEFAULT 'pending',
        "attempts" integer NOT NULL DEFAULT 0,
        "created_at" varchar(32),
        "updated_at" varchar(32),
        "last_error" text
    )''' % id_def))


def _enqueue_policy_events(conn, policy_code, batch_id, action):
    for event_type in OUTBOX_EVENTS:
        _enqueue_outbox(conn, event_type, {'policy_code': policy_code,
                                           'batch_id': batch_id,
                                           'action': action})


def _write_audit(conn, action, code, name, detail, operator):
    if not _table_exists(conn, 'audit_log'):
        return
    _insert(conn, 'audit_log', {
        'action': action, 'target_code': code, 'target_name': name,
        'detail': detail, 'operator': operator, 'created_at': _now(),
    })


def _apply_create(conn, normalized, batch_id, operator, batch_source):
    code = normalized['policy_code']
    values = {
        'policy_code': code,
        'policy_name': normalized.get('policy_name'),
        'level': normalized.get('level'),
        'category1': normalized.get('category1'),
        'category2': normalized.get('category2'),
        'category3': normalized.get('category3'),
        'attr': normalized.get('attr'),
        'regions': normalized.get('regions'),
        'doc_ref': normalized.get('doc_ref'),
        'target': normalized.get('target'),
        'conditions': normalized.get('conditions'),
        'benefit': normalized.get('benefit'),
        'process': normalized.get('process'),
        'contact': normalized.get('contact'),
        'effect_date': normalized.get('effect_date'),
        'expire_date': normalized.get('expire_date'),
        'status': normalized.get('status') or '在用',
        'cur_version': 'v1',
        'source': normalized.get('source') or batch_source or '',
        'level_note': normalized.get('level_note') or '',
        'updated_at': _now(),
        'updated_by': operator,
    }
    _insert(conn, 'policy', values)
    found = conn.execute(text('SELECT "policy_id" FROM "policy" WHERE "policy_code" = :code '
                              'ORDER BY "policy_id" LIMIT 1'), {'code': code}).first()
    if not found:
        raise RuntimeError('政策写入后未能读回：%s' % code)
    policy_id = found[0]
    _insert_details(conn, policy_id, normalized)
    snapshot = _snapshot(conn, policy_id)
    _insert(conn, 'policy_version', {
        'policy_id': policy_id, 'version_no': 'v1',
        'content_snapshot': json.dumps(snapshot, ensure_ascii=False, default=str),
        'effect_date': normalized.get('effect_date') or '',
        'expire_date': normalized.get('expire_date') or '',
        'updated_by': operator, 'updated_at': _now(),
        'note': '导入批次 %s 新增入库' % batch_id,
    })
    _replace_scope(conn, code, normalized.get('regions') or '',
                   _detail_regions(conn, policy_id))
    _enqueue_policy_events(conn, code, batch_id, 'create')
    _write_audit(conn, '导入新增', code, normalized.get('policy_name') or '',
                 '导入批次 %s 新增入库' % batch_id, operator)
    return policy_id


def _apply_update(conn, existing, normalized, batch_id, operator):
    code = normalized['policy_code']
    policy_id = existing['policy_id']
    if _supports_for_update(conn):
        # 事务内锁行：校验与提交之间若被其他写入改动，以这里的当前值为准写版本快照。
        conn.execute(text('SELECT "policy_id" FROM "policy" WHERE "policy_id" = :pid '
                          'FOR UPDATE'), {'pid': policy_id})
    old_version = existing.get('cur_version') or 'v1'
    new_version = _version_next(old_version)
    old_snapshot = _snapshot(conn, policy_id)
    _insert(conn, 'policy_version', {
        'policy_id': policy_id, 'version_no': old_version,
        'content_snapshot': json.dumps(old_snapshot, ensure_ascii=False, default=str),
        'effect_date': existing.get('effect_date') or '',
        'expire_date': existing.get('expire_date') or '',
        'updated_by': operator, 'updated_at': _now(),
        'note': '导入批次 %s 更新 %s→%s' % (batch_id, old_version, new_version),
    })
    values = {}
    for field in POLICY_FIELDS:
        if field in normalized:
            values[field] = normalized[field]
    values['cur_version'] = new_version
    values['updated_at'] = _now()
    values['updated_by'] = operator
    _update(conn, 'policy', values, {'policy_id': policy_id})

    if normalized.get('details_provided'):
        conn.execute(text('DELETE FROM "policy_detail" WHERE "policy_id" = :pid'),
                     {'pid': policy_id})
        _insert_details(conn, policy_id, normalized)
    final_regions = normalized.get('regions', existing.get('regions') or '')
    _replace_scope(conn, code, final_regions, _detail_regions(conn, policy_id))
    _enqueue_policy_events(conn, code, batch_id, 'update')
    _write_audit(conn, '导入更新', code, normalized.get('policy_name') or '',
                 '导入批次 %s 更新 %s→%s' % (batch_id, old_version, new_version), operator)
    return policy_id


def commit(conn, batch_id, operator=''):
    """提交批次（幂等）：同一批次二次调用返回 changed=0；调用方持有事务。"""
    ensure_schema(conn)
    batch = _load_batch(conn, batch_id, lock=True)
    if not batch:
        raise ValueError('批次不存在：%s' % batch_id)
    status = batch['status']
    if status == 'committed':
        report = _build_report([], {'batch_id': batch_id, 'status': 'committed',
                                    'mode': 'commit', 'dry_run': False})
        report.update({'changed': 0, 'created': 0, 'updated': 0, 'noop': 0,
                       'idempotent': True, 'committed_at': batch.get('committed_at')})
        return report
    if status in ('rolled_back', 'failed'):
        raise ValueError('批次状态为 %s，不能提交' % status)
    if status == 'staging':
        validate_batch(conn, batch_id)
        batch = _load_batch(conn, batch_id)
    if int(batch['dry_run']):
        raise ValueError('dry-run 批次不能提交，请用非 dry-run 重新建批次')
    if batch['status'] != 'validated':
        raise ValueError('批次状态为 %s，不能提交' % batch['status'])

    _advisory_lock(conn)
    rows = _load_rows(conn, batch_id)
    raw_rows, row_nos = _raw_rows_of(rows)
    # 提交前在事务内重新计算一次，防止校验后库内政策被改动导致计划过期。
    results = _plan_rows(conn, raw_rows, row_nos)
    _persist_plan(conn, results, rows)

    created = updated = noop = 0
    for item in results:
        if item['status'] != 'ok':
            continue
        action = item.get('action')
        if action == 'noop':
            noop += 1
            continue
        normalized = item['normalized']
        if action == 'create':
            _apply_create(conn, normalized, batch_id, operator, batch.get('source') or '')
            created += 1
        elif action == 'update':
            existing_rows = _find_policies_map(conn, [normalized['policy_code']]).get(
                normalized['policy_code'])
            if not existing_rows or len(existing_rows) != 1:
                raise ValueError('政策 %s 在提交时不存在或不唯一' % normalized['policy_code'])
            _apply_update(conn, existing_rows[0], normalized, batch_id, operator)
            updated += 1
    valid = sum(1 for item in results if item['status'] == 'ok')
    _update(conn, 'import_batch', {
        'total': len(results), 'valid': valid, 'invalid': len(results) - valid,
        'status': 'committed', 'committed_at': _now(),
    }, {'id': batch_id})
    _write_audit(conn, '批量导入',
                 batch_id, batch.get('file_name') or batch.get('source') or '',
                 '批次 %s 提交：新增 %d，更新 %d，无变化 %d' % (batch_id, created, updated, noop),
                 operator)
    report = _build_report(results, {
        'batch_id': batch_id, 'mode': 'commit', 'status': 'committed', 'dry_run': False,
        'changed': created + updated, 'created': created, 'updated': updated,
        'noop': noop, 'idempotent': False,
    })
    return report


# ---------------------------------------------------------------------------
# rollback：按批次版本链恢复
# ---------------------------------------------------------------------------
def _marker(batch_id):
    return '导入批次 ' + batch_id


def rollback(conn, batch_id, operator=''):
    """回滚已提交批次：恢复上一版本/删除本批新建；仅 committed 批次可回滚。"""
    ensure_schema(conn)
    batch = _load_batch(conn, batch_id, lock=True)
    if not batch:
        raise ValueError('批次不存在：%s' % batch_id)
    if batch['status'] != 'committed':
        raise ValueError('只有 committed 批次可回滚，当前状态：%s' % batch['status'])
    rows = _load_rows(conn, batch_id)
    restored = removed = 0
    skipped = []
    for row in reversed(rows):
        if row['status'] != 'ok' or row.get('action') not in ('create', 'update'):
            continue
        code = row.get('policy_code') or ''
        if not code:
            skipped.append({'row_no': row['row_no'], 'policy_code': '',
                            'reason': '缺少政策编号'})
            continue
        existing_rows = _find_policies_map(conn, [code]).get(code) or []
        if len(existing_rows) != 1:
            skipped.append({'row_no': row['row_no'], 'policy_code': code,
                            'reason': '政策已不存在或不唯一'})
            continue
        existing = existing_rows[0]
        if row['action'] == 'create':
            outcomes = _rollback_created_policy(conn, batch_id, existing, operator)
        else:
            outcomes = _rollback_updated_policy(conn, batch_id, existing, operator)
        if outcomes.get('ok'):
            if row['action'] == 'create':
                removed += 1
            else:
                restored += 1
        else:
            skipped.append({'row_no': row['row_no'], 'policy_code': code,
                            'reason': outcomes.get('reason') or '无法安全回滚'})
    _update(conn, 'import_batch', {'status': 'rolled_back'}, {'id': batch_id})
    _write_audit(conn, '批量回滚', batch_id, batch.get('file_name') or '',
                 '批次 %s 回滚：恢复 %d，删除新建 %d，跳过 %d'
                 % (batch_id, restored, removed, len(skipped)), operator)
    return {'batch_id': batch_id, 'mode': 'rollback', 'status': 'rolled_back',
            'restored': restored, 'removed': removed, 'skipped': skipped[:20],
            'skipped_count': len(skipped)}


def _rollback_created_policy(conn, batch_id, existing, operator):
    code = existing['policy_code']
    policy_id = existing['policy_id']
    versions = [dict(row) for row in conn.execute(
        text('SELECT "version_no", "note" FROM "policy_version" WHERE "policy_id" = :pid'),
        {'pid': policy_id}).mappings().all()]
    marker = _marker(batch_id)
    if len(versions) != 1 or marker not in (versions[0].get('note') or ''):
        return {'ok': False, 'reason': '政策在批次后有其他变更，跳过以免误删'}
    if (existing.get('cur_version') or 'v1') != 'v1':
        return {'ok': False, 'reason': '政策版本已变更，跳过以免误删'}
    conn.execute(text('DELETE FROM "policy_detail" WHERE "policy_id" = :pid'),
                 {'pid': policy_id})
    conn.execute(text('DELETE FROM "policy_version" WHERE "policy_id" = :pid'),
                 {'pid': policy_id})
    if _table_exists(conn, 'policy_scope'):
        conn.execute(text('DELETE FROM "policy_scope" WHERE "policy_code" = :code'),
                     {'code': code})
    conn.execute(text('DELETE FROM "policy" WHERE "policy_id" = :pid'), {'pid': policy_id})
    _enqueue_policy_events(conn, code, batch_id, 'rollback_delete')
    _write_audit(conn, '导入回滚删除', code, existing.get('policy_name') or '',
                 '导入批次 %s 回滚删除本批新建政策' % batch_id, operator)
    return {'ok': True}


def _rollback_updated_policy(conn, batch_id, existing, operator):
    code = existing['policy_code']
    policy_id = existing['policy_id']
    marker = _marker(batch_id)
    version = conn.execute(text(
        'SELECT "version_id", "version_no", "content_snapshot" FROM "policy_version" '
        'WHERE "policy_id" = :pid AND "note" LIKE :marker '
        'ORDER BY "version_id" DESC LIMIT 1'),
        {'pid': policy_id, 'marker': '%' + marker + '%'}).mappings().first()
    if not version or not version['content_snapshot']:
        return {'ok': False, 'reason': '未找到本批次的上一版本快照'}
    old_version = version['version_no'] or 'v1'
    expected_after = _version_next(old_version)
    if (existing.get('cur_version') or 'v1') != expected_after:
        return {'ok': False, 'reason': '批次后政策又有变更，跳过以免覆盖'}
    try:
        snapshot = json.loads(version['content_snapshot'])
    except (TypeError, ValueError):
        return {'ok': False, 'reason': '版本快照损坏，跳过'}
    values = {key: value for key, value in snapshot.items()
              if key not in ('policy_id', 'details')}
    values['updated_at'] = _now()
    values['updated_by'] = operator
    _update(conn, 'policy', values, {'policy_id': policy_id})
    conn.execute(text('DELETE FROM "policy_detail" WHERE "policy_id" = :pid'),
                 {'pid': policy_id})
    for detail in snapshot.get('details') or []:
        if isinstance(detail, dict):
            _insert(conn, 'policy_detail', detail)
    _replace_scope(conn, code, snapshot.get('regions') or '',
                   _detail_regions(conn, policy_id))
    _enqueue_policy_events(conn, code, batch_id, 'rollback_restore')
    _write_audit(conn, '导入回滚恢复', code, snapshot.get('policy_name') or '',
                 '导入批次 %s 回滚到 %s' % (batch_id, old_version), operator)
    return {'ok': True}


# ---------------------------------------------------------------------------
# 批次查询
# ---------------------------------------------------------------------------
def batch_report(conn, batch_id):
    ensure_schema(conn)
    batch = _load_batch(conn, batch_id)
    if not batch:
        raise ValueError('批次不存在：%s' % batch_id)
    rows = _load_rows(conn, batch_id)
    actions = {action: 0 for action in ACTIONS}
    errors, warnings, unresolved, changed = [], [], [], []
    for row in rows:
        if row.get('action') in actions:
            actions[row['action']] += 1
        try:
            info = json.loads(row.get('errors') or '{}')
        except (TypeError, ValueError):
            info = {}
        for item in (info.get('errors') or [])[:10]:
            errors.append({'row_no': row['row_no'], 'policy_code': row.get('policy_code'),
                           'errors': [item]})
        for item in (info.get('warnings') or [])[:10]:
            warnings.append({'row_no': row['row_no'], 'policy_code': row.get('policy_code'),
                             'warnings': [item]})
        for item in info.get('unresolved') or []:
            unresolved.append({'row_no': row['row_no'], 'policy_code': row.get('policy_code'),
                               'raw_text': item.get('raw_text'),
                               'source_field': item.get('source_field')})
        if info.get('changed_fields'):
            changed.append({'row_no': row['row_no'], 'policy_code': row.get('policy_code'),
                            'changed_fields': info['changed_fields']})
    return {
        'batch_id': batch['id'], 'source': batch.get('source'),
        'file_name': batch.get('file_name'), 'status': batch.get('status'),
        'dry_run': bool(int(batch.get('dry_run') or 0)),
        'total': batch.get('total'), 'valid': batch.get('valid'),
        'invalid': batch.get('invalid'), 'operator': batch.get('operator'),
        'created_at': batch.get('created_at'), 'committed_at': batch.get('committed_at'),
        'actions': actions,
        'errors': errors[:20], 'warnings': warnings[:20],
        'unresolved': unresolved[:20], 'changed': changed[:20],
        'rows': [{'row_no': row['row_no'], 'status': row['status'],
                  'action': row.get('action'), 'policy_code': row.get('policy_code')}
                 for row in rows[:200]],
    }


# 语义化别名，与任务书 commit(conn,batch_id)/rollback(conn,batch_id) 对齐
commit_batch = commit
rollback_batch = rollback
