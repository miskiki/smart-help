"""Field-level source binding. This module never invents eligibility rules or writes DBs.

The build tool reads all original columns into a content-addressed bundle. Runtime
checks original-file hashes before using it. Missing/stale/ambiguous sources are
gaps, never permission to borrow a neighbouring county's rules.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import threading

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BUNDLE = Path(__file__).resolve().parents[1] / 'data/policy_evidence/bundle.json'
_lock = threading.RLock()
_cache = {}


def compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_bundle():
    path = Path(os.environ.get('POLICY_EVIDENCE_BUNDLE', str(DEFAULT_BUNDLE)))
    root = Path(os.environ.get('POLICY_SOURCE_ROOT', str(ROOT / 'data'))).resolve()
    with _lock:
        if not path.is_file():
            return None, ['local_evidence_bundle_missing']
        # Validate hashes on each catalog scan, including same-size file changes.
        stat = path.stat()
        signature = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, str(root))
        if _cache.get('signature') != signature:
            try:
                raw = path.read_bytes()
                revision = hashlib.sha256(raw).hexdigest()
                bundle = json.loads(raw)
                if not isinstance(bundle,dict) or bundle.get('schema_version') != 1 or not isinstance(bundle.get('original_files'),dict):
                    raise ValueError('unsupported_schema')
                _cache.clear(); _cache.update(revision=revision, root=str(root), signature=signature, bundle=bundle)
            except (ValueError, TypeError):
                return None, ['local_evidence_bundle_invalid']
        bundle = _cache['bundle']
        invalid = []
        for name, record in bundle['original_files'].items():
            target = (root / name).resolve()
            try:
                valid = target.parent == root and target.is_file() and fingerprint(target) == record['sha256']
            except OSError:
                valid = False
            if not valid:
                invalid.append(name)
        # Per-file rejection: an unrelated stale file must not poison good evidence.
        return dict(bundle, revision=_cache['revision'], invalid_files=invalid), []


def _extra(detail):
    extra = detail.get('extra') or {}
    if isinstance(extra, str):
        try: extra = json.loads(extra)
        except (ValueError, TypeError): return {}
    return extra if isinstance(extra, dict) else {}


def enrich(policy, detail, source, context=None):
    bundle, errors = context if context is not None else load_bundle()
    result = dict(source)
    blockers = list(errors)
    provenance = []
    warnings = []
    family = None
    if bundle is not None:
        metadata = _extra(detail).get('benefit_source') or {}
        filename = metadata.get('source_file')
        row_ids = metadata.get('source_rows') or ([metadata['source_row']] if metadata.get('source_row') else [])
        table = bundle.get('workbooks', {}).get(filename)
        if filename in bundle.get('invalid_files', []):
            blockers.append('original_file_changed_or_missing:' + filename)
        elif table:
            if metadata.get('source_sheet') != table['sheet']:
                blockers.append('original_sheet_mismatch')
            else:
                candidates = [table['rows'].get(str(i)) for i in row_ids]
                candidates = [r for r in candidates if r and compact(r['fields'].get('政策所属行政区域')) == compact(detail.get('region'))
                              and compact(r['fields'].get('政策名称')) == compact(policy.get('name'))]
                # Generic policy names may cover several independent benefits.
                # Existing exact detail conditions and standard disambiguate rows.
                if len(candidates) > 1:
                    candidates = [r for r in candidates if compact(r['fields'].get('政策适用条件')) == compact(detail.get('conditions'))
                                  and compact(r['fields'].get('补贴标准/元')) in {compact(detail.get('raw_benefit')), compact(detail.get('benefit'))}]
                if len(candidates) != 1:
                    blockers.append('original_row_binding_ambiguous_or_mismatched')
                else:
                    row = candidates[0]
                    for name, body in row['fields'].items():
                        # Counts, phone and issuer are retained in the bundle,
                        # not exposed as eligibility text to the compiler.
                        if name in bundle['compiler_columns'] and body not in (None, ''):
                            field = 'original_' + bundle['compiler_columns'][name]
                            result[field] = str(body)
                            provenance.append(dict(field=field, file=filename, sha256=bundle['original_files'][filename]['sha256'],
                                                   sheet=table['sheet'], row=row['row'], cell=row['cells'][name]))
                    if re.search(r'的\s*岁(?:视力|听力|言语|肢体|智力)', str(row['fields'].get('政策适用条件', ''))):
                        blockers.append('original_eligibility_age_missing')
                    if '民生实事' in str(row['fields'].get('政策文件名称及文号')) and '2023' in str(row['fields'].get('政策文件名称及文号')):
                        blockers.append('annual_2023_project_continuation_not_verified')
                    if len(row_ids) > 1:
                        warnings.append('merged_sources_bound_to_individual_benefit_row')
        elif filename:
            blockers.append('original_workbook_not_available:' + str(filename))

        title = result.get('original_title') or policy.get('name') or ''
        subtype = result.get('original_category3') or ''
        for group in bundle.get('benefit_links', []):
            if title in group['titles'] or subtype in group.get('subtypes', []):
                family = group['family']
                for link in group['links']:
                    if detail.get('region') not in link['regions']:
                        continue
                    doc = bundle['documents'][link['document']]
                    # Local derived summaries never become independent authority.
                    if doc.get('original_file') in bundle.get('invalid_files', []):
                        blockers.append('linked_original_file_changed:' + doc['id']); continue
                    field = 'linked_' + link['role'] + '_' + doc['id']
                    text = doc['text']
                    if link.get('paragraph'):
                        text = doc['paragraphs'][link['paragraph'] - 1]
                    spans=[]
                    if link.get('sections'):
                        compact_text=compact(text); selected=[]
                        try:
                            for section in link['sections']:
                                start=compact_text.index(compact(section['start']))
                                end=compact_text.index(compact(section['end']), start+len(compact(section['start']))) if section.get('end') else len(compact_text)
                                selected.append(compact_text[start:end]);spans.append(dict(start=start,end=end,normalization='remove_whitespace'))
                        except ValueError:
                            blockers.append('linked_document_section_not_found:'+doc['id']);continue
                        text='\n'.join(selected)
                    result[field] = text
                    provenance.append(dict(field=field, document=doc['id'], source_kind=doc['source_kind'],
                                           url=doc.get('url'), original_file=doc.get('original_file'),
                                           sha256=doc['sha256'], role=link['role'], source_version=doc['version'],
                                           paragraph=link.get('paragraph'), scope=link['regions']))
                    if spans:provenance[-1]['document_spans']=spans
                    warnings.extend(doc.get('warnings', []))
                break
        # A version certificate is produced by the source-verification job, not
        # the extraction model. Its hashes bind it to the exact current inputs.
        certificate = bundle.get('verified_coverage', {}).get(str(family) + '|' + str(detail.get('region')))
        linked = [x for x in provenance if x.get('role') == 'qualification']
        certified = bool(certificate and linked and certificate.get('checks') == {
            'scope': True, 'amendments': True, 'benefit_relation': True, 'full_qualification_source': True}
            and certificate.get('document_hashes') == {x['document']: x['sha256'] for x in linked})
        if not certified:
            blockers.append('current_version_and_exact_benefit_relation_not_fully_verified')
        result['evidence_revision'] = bundle['revision']
    result['source_provenance'] = json.dumps(provenance, ensure_ascii=False, sort_keys=True)
    messages = {
        'current_version_and_exact_benefit_relation_not_fully_verified': '基础办法、当前年度标准与本项待遇的适用关系尚未核齐',
        'original_eligibility_age_missing': '原始政策表缺失适用年龄，需要系统补查正式文件',
        'annual_2023_project_continuation_not_verified': '该项目引用2023年度材料，当前年度延续情况尚未核实',
        'original_row_binding_ambiguous_or_mismatched': '原表中具体待遇与当前明细的对应关系尚未核实',
    }
    result['source_quality'] = json.dumps(dict(family=family, qualification_blockers=sorted(set(blockers)),
                                              gap_messages=[messages.get(x, '政策原始资料缺失、变更或无法可靠关联，需要系统补查') for x in sorted(set(blockers))],
                                              warnings=sorted(set(warnings)), current_qualification_ready=not blockers), ensure_ascii=False, sort_keys=True)
    return result


def blockers(source):
    if not source.get('source_quality'):
        return []
    try:
        value = json.loads(source['source_quality'])
        return value['qualification_blockers'] if isinstance(value['qualification_blockers'], list) else ['invalid_source_quality']
    except (ValueError, TypeError, KeyError):
        return ['invalid_source_quality']


def source_roles():
    """字段级来源角色：区分可执行资格、适用人群陈述、分类标签和参考资料。"""
    return {
        'original_conditions': 'binding_condition',
        'original_content': 'binding_condition',
        'conditions': 'binding_condition',
        'original_subject': 'audience_statement',
        'target': 'audience_statement',
        'parent_target': 'audience_statement',
        'original_title': 'classification',
        'title': 'classification',
        'original_category3': 'classification',
        'scope_category1': 'classification',
        'scope_category2': 'classification',
        'scope_category3': 'classification',
        'parent_conditions': 'reference_only',
        'reference_text': 'reference_only',
    }


def evidence_role(field_name):
    """返回字段的来源角色。"""
    role = source_roles().get(field_name)
    if role is None and field_name.startswith('linked_'):
        role = 'binding_condition' if field_name.startswith('linked_qualification_') else 'reference_only'
    return role or 'unresolved_role'


def qualification_field(name):
    if name in {'scope_category1','scope_category2','scope_category3'}:return True
    if name.startswith('original_'):
        return name in {'original_title', 'original_subject', 'original_conditions', 'original_content', 'original_category3'}
    if name.startswith('linked_'):
        return name.startswith('linked_qualification_')
    return name in {'title','target','conditions','parent_target','parent_conditions','reference_text'}
