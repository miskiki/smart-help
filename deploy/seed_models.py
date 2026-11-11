#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 config/model_settings.yaml 的对话平台一次性导入 models 表（§4.5 seed 脚本）。

只做 chat 行（P1 范围）；embed/rerank 行在 P2/P3 各阶段按需 seed。
幂等：按稳定 id（seed-<平台>-<模型>）跳过已存在行。
默认模型：取当前 app_config.qa_llm_platform 对应的行置 is_default；
找不到则第一个 chat 行成为默认。

用法：
  python deploy/seed_models.py            # 只读检查（列出将 seed 的行）
  python deploy/seed_models.py --apply    # 写入（幂等）
"""
import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)


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


def _slug(s):
    s = re.sub(r'[^\w.-]+', '-', (s or '').strip(), flags=re.UNICODE).strip('-')
    return s or 'model'


def planned_rows(yaml_path):
    """chat + embed + rerank 行（P1 seed chat，P2 补 embed，P3 补 rerank）。

    embed 行：dim 取平台 profile.embed_dim，query_instruction 取 profile.embed_query_instruction。
    rerank 行：provider 恒 dashscope_rerank（本地 serve_qwen3_rerank 兼容层与 DashScope 原生网关
    都说 {model,input:{query,documents}}→output.results[]）；base_url 取 profile.rerank_endpoint
    （本地完整端点），留空则回落 DashScope 原生重排网关。dim 恒 None（重排无向量维度）。"""
    import yaml
    from model_creds import encrypt
    with open(yaml_path, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    rows = []
    for plat in data.get('MODEL_PLATFORMS', []):
        profile = plat.get('profile') or {}
        base_url = (plat.get('api_base_url') or '').rstrip('/')
        key_enc = encrypt(plat.get('api_key') or '')
        for model in plat.get('llm_models') or []:
            rows.append({
                'id': 'seed-%s-%s' % (_slug(plat.get('platform_name')), _slug(model)),
                'type': 'chat',
                'name': '%s / %s' % (plat.get('platform_name'), model),
                'source': 'remote',
                'provider': 'openai',
                'base_url': base_url,
                'api_key_enc': key_enc,
                'model_name': model,
                'dim': None,
                'query_instruction': '',
                'json_mode': bool(profile.get('json_mode')),
                'platform_name': plat.get('platform_name'),
            })
        for model in plat.get('embed_models') or []:
            dim = profile.get('embed_dim')
            if dim is not None:
                dim = int(dim)
            rows.append({
                'id': 'seed-%s-emb-%s' % (_slug(plat.get('platform_name')), _slug(model)),
                'type': 'embed',
                'name': '%s / %s' % (plat.get('platform_name'), model),
                'source': 'remote',
                'provider': 'openai',
                'base_url': base_url,
                'api_key_enc': key_enc,
                'model_name': model,
                'dim': dim,
                'query_instruction': profile.get('embed_query_instruction') or '',
                'json_mode': False,
                'platform_name': plat.get('platform_name'),
            })
        for model in plat.get('rerank_models') or []:
            endpoint = (profile.get('rerank_endpoint') or '').strip() \
                or 'https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank'
            rows.append({
                'id': 'seed-%s-rr-%s' % (_slug(plat.get('platform_name')), _slug(model)),
                'type': 'rerank',
                'name': '%s / %s' % (plat.get('platform_name'), model),
                'source': 'remote',
                'provider': 'dashscope_rerank',
                'base_url': endpoint.rstrip('/'),
                'api_key_enc': key_enc,
                'model_name': model,
                'dim': None,
                'query_instruction': '',
                'json_mode': False,
                'platform_name': plat.get('platform_name'),
            })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='写入 models 表（幂等）')
    parser.add_argument('--json', action='store_true', help='机器可读输出')
    args = parser.parse_args()
    load_env()

    yaml_path = os.environ.get('MODEL_SETTINGS_PATH') or os.path.join(BASE, 'config', 'model_settings.yaml')
    rows = planned_rows(yaml_path)

    # 默认模型选择：当前 qa_llm_platform 对应行优先
    default_platform = None
    try:
        import config
        from sqlalchemy import create_engine, text
        engine = create_engine(config.DATABASE_URL)
        with engine.connect() as c:
            row = c.execute(text(
                "SELECT cfg_value FROM app_config WHERE cfg_key=:k"),
                {'k': 'qa_llm_platform'}).fetchone()
            default_platform = row[0] if row else None
    except Exception:
        default_platform = None
    if not default_platform:
        default_platform = 'bailian'
    default_row = next((r for r in rows if r['type'] == 'chat'
                        and r['platform_name'] == default_platform), None)
    if default_row is None:
        default_row = next((r for r in rows if r['type'] == 'chat'), None)

    # 嵌入默认：对齐运行期向量平台（EMBED_PLATFORM > app_config.qa_embed_platform >
    # yaml DEFAULT_EMBED_PLATFORM），保证 seed 后默认 embed 与现网在跑的一致。
    default_embed_platform = default_platform
    try:
        from model_registry import get_embed_platform
        default_embed_platform = get_embed_platform()
    except Exception:
        pass
    default_embed_row = next((r for r in rows if r['type'] == 'embed'
                              and r['platform_name'] == default_embed_platform), None)
    if default_embed_row is None:
        default_embed_row = next((r for r in rows if r['type'] == 'embed'), None)

    # 重排默认：对齐运行期向量平台（现网 source-ranker 用的正是该平台的 rerank_endpoint）。
    default_rerank_row = next((r for r in rows if r['type'] == 'rerank'
                               and r['platform_name'] == default_embed_platform), None)
    if default_rerank_row is None:
        default_rerank_row = next((r for r in rows if r['type'] == 'rerank'), None)

    if not args.apply:
        report = {'yaml': yaml_path,
                  'default_chat': default_row and default_row['id'],
                  'default_embed': default_embed_row and default_embed_row['id'],
                  'default_rerank': default_rerank_row and default_rerank_row['id'],
                  'rows': [{k: r[k] for k in ('id', 'type', 'name', 'base_url', 'model_name',
                                              'dim', 'json_mode')} for r in rows]}
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print('seed 计划（只读检查，--apply 写入）：默认 chat=%s  默认 embed=%s  默认 rerank=%s'
                  % (default_row and default_row['id'], default_embed_row and default_embed_row['id'],
                     default_rerank_row and default_rerank_row['id']))
            for r in rows:
                dim = ('  dim=%s' % r['dim']) if r['type'] == 'embed' else ''
                print('  - [%s] %s  %s → %s%s' % (r['type'], r['id'], r['name'], r['base_url'], dim))
        return 0

    import config
    from models import db, Model
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(config.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    inserted, skipped = [], []
    try:
        for r in rows:
            exists = session.get(Model, r['id'])
            if exists:
                skipped.append(r['id'])
                continue
            is_default = ((default_row is not None and r['id'] == default_row['id']) or
                          (default_embed_row is not None and r['id'] == default_embed_row['id']) or
                          (default_rerank_row is not None and r['id'] == default_rerank_row['id']))
            if is_default:
                session.query(Model).filter(
                    Model.type == r['type'], Model.id != r['id']).update(
                    {Model.is_default: False}, synchronize_session=False)
            session.add(Model(id=r['id'], type=r['type'], name=r['name'],
                              source=r['source'], provider=r['provider'],
                              base_url=r['base_url'], api_key_enc=r['api_key_enc'],
                              model_name=r['model_name'], dim=r['dim'],
                              query_instruction=r['query_instruction'],
                              json_mode=r['json_mode'], is_default=is_default))
            inserted.append(r['id'])
        session.commit()
    except Exception as exc:
        session.rollback()
        print('seed 失败：%s' % exc)
        return 1
    finally:
        session.close()
    report = {'inserted': inserted, 'skipped': skipped,
              'default_chat': default_row and default_row['id'],
              'default_embed': default_embed_row and default_embed_row['id'],
              'default_rerank': default_rerank_row and default_rerank_row['id']}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('seed 完成：新增 %d，跳过已存在 %d，默认 chat=%s  默认 embed=%s  默认 rerank=%s'
              % (len(inserted), len(skipped), report['default_chat'],
                 report['default_embed'], report['default_rerank']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
