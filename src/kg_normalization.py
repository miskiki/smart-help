"""Conservative graph label cleanup; policy source text is never rewritten."""
import re

INVALID_DOCUMENT_LABELS = {'桔柚', '会昌县2026年脐橙', '街办', '7号', '文号8',
    '赣财规', '九医保字', '赣卫基层字', '江西省人', '施行时间', '暂未下文', '资金的通知'}
DOCUMENT_ORGANIZATIONS = {'财政', '住建', '发改', '民政', '财政局', '财政部',
    '县医保局', '县卫健委', '县教体局', '县财政局', '发改委',
    '省住建厅', '省发改委', '省民政厅', '省财政厅'}
INCOMPLETE_CONDITIONS = {'经民政', '对因病', '为婚检', '二级）', '县（区',
    '接受中', '对农户', '由亲属'}
NORMALIZATION_VERSION = 'kg-normalization-v3'


def is_invalid_document(label):
    label = normalize_label('DOC', label)
    return label in INVALID_DOCUMENT_LABELS or bool(re.fullmatch(r'(?:文号)?\d+号?',label))


def document_numbers(source):
    """Only well formed citations; malformed OCR text stays a source note for review."""
    source = normalize_label('DOC', source)
    return sorted(set(prefix.split('通知')[-1].removeprefix('文号')+'〔'+year+'〕'+number+'号'
        for prefix,year,number in re.findall(
            r'([\u4e00-\u9fff]{2,15})〔((?:19|20)\d{2})〕(\d+)号',source)))


def is_document_organization(label):
    label = re.sub(r'\s+', '', str(label or ''))
    return label in DOCUMENT_ORGANIZATIONS or (
        len(label) <= 35 and '关于' not in label and bool(re.fullmatch(
            r'[\u4e00-\u9fff]+(?:厅|局|部|委员会|联合会|残联)', label)))


def normalize_label(node_type, label):
    label = str(label or '').strip()
    if node_type == 'POPULATION':
        label = label.replace('防止防贫致贫对象','防止返贫致贫对象')
        parts = re.split(r'[,，、;；]', label)
        return ','.join(sorted({re.sub(r'\s+', '', p) for p in parts if p.strip()}))
    if node_type == 'DOC':
        label = re.sub(r'\s+', '', label)
        return re.sub(r'[({（［\[〔【]\s*((?:19|20)\d{2})\s*[)}）］\]〕】]', r'〔\1〕', label)
    if node_type == 'CONDITION':
        label = label.replace('官颈癌','宫颈癌')
        # OCR line wrapping inside Chinese words is not a semantic separator.
        return re.sub(r'(?<=[\u4e00-\u9fff])\s*\n\s*(?=[\u4e00-\u9fff])', '', label)
    return label


def same_doc_sql():
    # Kingbase Oracle compatibility treats '' as NULL. Never put '' in NOT IN.
    return """SELECT e.src_id, e.dst_id FROM kg_policy_edge e
        WHERE e.edge_type=:et AND EXISTS (
          SELECT 1 FROM kg_edge a JOIN kg_edge b ON a.dst_id=b.dst_id
          JOIN kg_node n ON n.id=a.dst_id
          WHERE a.src_id=e.src_id AND b.src_id=e.dst_id
          AND a.edge_type='FROM_DOC' AND b.edge_type='FROM_DOC'
          AND n.node_type IN ('DOC','FILE')
          AND (SELECT count(DISTINCT d.src_id) FROM kg_edge d
               WHERE d.dst_id=n.id AND d.edge_type='FROM_DOC') <= :max_policies
          AND trim(n.name) IS NOT NULL AND length(trim(n.name)) > 0
          AND trim(n.name) NOT IN ('无','暂无','未提供','未知','不详','无文号','财政','住建','发改','桔柚'))"""


if __name__=='__main__':
    from kg_source_update import main
    main()
