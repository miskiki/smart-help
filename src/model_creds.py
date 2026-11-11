# -*- coding: utf-8 -*-
"""模型凭证加密（docs/redesign/30-custom-model-registry.md §4.2）。

api_key 用 Fernet（AES-128-CBC + HMAC）加密落库 models.api_key_enc，
任何接口不回显明文。加解密失败降级空串并告警（对齐参考项目行为，不阻断问答）。

密钥来源：
  1. 环境变量 MODEL_CRED_KEY（base64 Fernet key，推荐，deploy/web.env 注入）；
  2. 未设置时从 Kingbase app_config 派生一个稳定 key（首次生成后持久化，
     保证重启不丢凭证），同时打告警日志提示运维显式配置。
"""
import base64
import logging
import os
import threading

_LOG = logging.getLogger(__name__)
_lock = threading.Lock()
_fernet = None
_KEY_CFG = 'model_cred_key'


def _derive_key_from_db():
    """从 app_config 取/建稳定 key。返回 base64 Fernet key 或 None。"""
    try:
        from sqlalchemy import create_engine, text
        import config
        engine = create_engine(config.DATABASE_URL)
        with engine.connect() as c:
            row = c.execute(text(
                "SELECT cfg_value FROM app_config WHERE cfg_key=:k"),
                {'k': _KEY_CFG}).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as exc:
        _LOG.warning('model_creds: 读取 app_config 派生 key 失败：%s', exc)
        return None
    # 首次生成并持久化
    try:
        from cryptography.fernet import Fernet
        new_key = Fernet.generate_key().decode('ascii')
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO app_config(cfg_key,cfg_value,updated_at) VALUES(:k,:v,now()) "
                "ON CONFLICT (cfg_key) DO UPDATE SET cfg_value=:v, updated_at=now()"),
                {'k': _KEY_CFG, 'v': new_key})
        return new_key
    except Exception as exc:
        _LOG.warning('model_creds: 生成/持久化派生 key 失败：%s', exc)
        return None


def _get_fernet():
    global _fernet
    with _lock:
        if _fernet is None:
            from cryptography.fernet import Fernet
            raw = (os.environ.get('MODEL_CRED_KEY') or '').strip()
            if raw:
                _fernet = Fernet(raw.encode('ascii'))
            else:
                key = _derive_key_from_db()
                if key:
                    _fernet = Fernet(key.encode('ascii'))
                    _LOG.warning('model_creds: 未设置 MODEL_CRED_KEY，使用 Kingbase '
                                 'app_config 派生 key（重启稳定但密钥在库中明文，'
                                 '建议运维显式配置 env）')
                else:
                    _LOG.error('model_creds: 无可用密钥来源，凭证功能降级为明文空串')
    return _fernet


def encrypt(plain):
    """加密明文 api_key。空串/EMPTY/无密钥时返回空串（不存）。"""
    plain = (plain or '').strip()
    if not plain or plain.upper() == 'EMPTY':
        return ''
    f = _get_fernet()
    if f is None:
        _LOG.error('model_creds: 加密失败（无密钥），凭证不落库')
        return ''
    return f.encrypt(plain.encode('utf-8')).decode('ascii')


def decrypt(enc):
    """解密密文。失败降级空串（不阻断调用链）。"""
    enc = (enc or '').strip()
    if not enc:
        return ''
    f = _get_fernet()
    if f is None:
        return ''
    try:
        return f.decrypt(enc.encode('ascii')).decode('utf-8')
    except Exception as exc:
        _LOG.warning('model_creds: 解密失败（密钥变更？），降级空串：%s', exc)
        return ''


def is_configured(enc):
    """前端展示用：凭证是否已配置（不回显明文）。"""
    return bool((enc or '').strip())
