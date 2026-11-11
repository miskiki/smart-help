"""JWT authentication; live database sessions make logout/reset/disable immediate."""
import hashlib
import secrets
import time
from functools import wraps
from urllib.parse import urlsplit

import jwt
from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash
from models import db, AdminUser, LoginSession, AuthAudit

auth_bp = Blueprint('auth', __name__, url_prefix='/api')
COOKIE = 'helpdesk_access'
CSRF = 'helpdesk_csrf'
TTL = 8 * 60 * 60


class ApiError(Exception):
    def __init__(self, message, status=400, code=None):
        self.message, self.status, self.code = message, status, code


def public_user(u):
    return dict(id=u.id, username=u.username, real_name=u.real_name or '',
                role=u.role, active=u.active, must_change_password=u.must_change_password,
                created_at=u.created_at)


def audit(action, target=None):
    db.session.add(AuthAudit(actor_id=getattr(getattr(g, 'auth_user', None), 'id', None),
                            action=action, target=str(target or ''), happened_at=int(time.time())))

#双通道令牌传递
def current_user():
    if hasattr(g, 'auth_user'):
        return g.auth_user
    g.auth_user = None
    #API 客户端：用 Authorization: Bearer <token>
    #浏览器：用 HttpOnly Cookie（helpdesk_access）
    header = request.headers.get('Authorization', '')
    token = header[7:] if header.startswith('Bearer ') else request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        claims = jwt.decode(token, current_app.config['AUTH_JWT_SECRET'], algorithms=['HS256'],
                            issuer='smart-help', audience='smart-help-web',
                            options={'require': ['exp', 'iat', 'sub', 'jti', 'ver']})
    except (jwt.PyJWTError, TypeError, ValueError):
        return None
    try:
        uid = int(claims['sub'])
    except (TypeError, ValueError):
        return None
    login = db.session.get(LoginSession, claims['jti'])
    u = db.session.get(AdminUser, uid)
    if not login or not u or not u.active or login.user_id != u.id or login.revoked:
        return None
    if login.expires_at <= int(time.time()) or u.auth_version != claims['ver']:
        return None
    if not header.startswith('Bearer ') and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        supplied = request.headers.get('X-CSRF-Token', '')
        if not secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), login.csrf_hash):
            raise ApiError('请求校验失败，请刷新页面重试', 403)
    g.auth_user, g.auth_session = u, login
    return u


def require_user(allow_setup=False):
    u = current_user()
    if not u:
        raise ApiError('登录已失效，请重新登录', 401)
    if not allow_setup and (u.must_change_password or not u.real_name):
        raise ApiError('请先修改密码并补全真实姓名', 403, 'setup_required')
    return u


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        require_user()
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if require_user().role != 'admin':
            raise ApiError('仅管理员可执行此操作', 403)
        return fn(*args, **kwargs)
    return wrapped


def validate_password(pw):
    if not isinstance(pw, str) or not 8 <= len(pw) <= 128:
        raise ApiError('密码长度需为 8–128 位')


def issue(u):
    now, sid, csrf = int(time.time()), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.session.add(LoginSession(id=sid, user_id=u.id, expires_at=now + TTL,
                                revoked=False, csrf_hash=hashlib.sha256(csrf.encode()).hexdigest()))
    db.session.commit()
    
    token = jwt.encode(dict(sub=str(u.id), jti=sid, ver=u.auth_version, iat=now, exp=now + TTL,
                            iss='smart-help', aud='smart-help-web'),
                       current_app.config['AUTH_JWT_SECRET'], algorithm='HS256')
    response = jsonify(ok=True, user=public_user(u), access_token=token, token_type='Bearer', expires_in=TTL)
    for key, value, httponly in [(COOKIE, token, True), (CSRF, csrf, False)]:
        response.set_cookie(key, value, max_age=TTL, httponly=httponly, samesite='Strict',
                            secure=current_app.config.get('AUTH_COOKIE_SECURE', False), path='/')
    response.delete_cookie('session')
    return response


def revoke(u):
    u.auth_version += 1
    LoginSession.query.filter_by(user_id=u.id, revoked=False).update({'revoked': True})


@auth_bp.post('/login')
def login():
    data = request.get_json(silent=True) or {}
    username, pw = data.get('username', ''), data.get('password', '')
    if not isinstance(username, str) or not isinstance(pw, str) or len(pw) > 128:
        raise ApiError('用户名或密码错误', 401)
    # Per-user cooldown is stored, so it survives restarts and worker processes.
    u = AdminUser.query.filter_by(username=username.strip()).first()
    now = int(time.time())
    if u and u.locked_until > now:
        raise ApiError('尝试过于频繁，请稍后再试', 429)
    if not u or not u.active or not check_password_hash(u.password_hash, pw):
        if u:
            u.failed_logins += 1
            if u.failed_logins >= 10:
                u.locked_until, u.failed_logins = now + 300, 0
            db.session.commit()
        raise ApiError('用户名或密码错误，或账号已停用', 401)
    g.auth_user = u
    u.failed_logins, u.locked_until = 0, 0
    audit('login', u.id)
    return issue(u)


@auth_bp.get('/me')
def me():
    u = current_user()
    return jsonify(ok=True, user=public_user(u) if u else None)


@auth_bp.post('/logout')
def logout():
    u = current_user()
    if u:
        g.auth_session.revoked = True
        audit('logout', u.id)
        db.session.commit()
    response = jsonify(ok=True)
    response.delete_cookie(COOKIE)
    response.delete_cookie(CSRF)
    response.delete_cookie('session')
    return response


@auth_bp.post('/password')
def change_password():
    u = require_user(allow_setup=True)
    d = request.get_json(silent=True) or {}
    if not check_password_hash(u.password_hash, d.get('old_password', '')):
        raise ApiError('当前密码不正确')
    pw = d.get('new_password', '')
    validate_password(pw)
    if check_password_hash(u.password_hash, pw):
        raise ApiError('新密码不能与当前密码相同')
    name = (d.get('real_name') or u.real_name or '').strip()
    if not 1 <= len(name) <= 64:
        raise ApiError('请填写真实姓名（最多 64 字）')
    u.real_name, u.password_hash = name, generate_password_hash(pw)
    u.must_change_password = False
    revoke(u)
    audit('password_changed', u.id)
    db.session.commit()
    response = jsonify(ok=True, msg='密码已修改，请重新登录')
    response.delete_cookie(COOKIE)
    response.delete_cookie(CSRF)
    return response


@auth_bp.get('/users')
@admin_required
def users():
    return jsonify(ok=True, items=[public_user(u) for u in AdminUser.query.order_by(AdminUser.id)])


@auth_bp.post('/users')
@admin_required
def create_user():
    import re
    d = request.get_json(silent=True) or {}
    username, name, pw = d.get('username', '').strip(), d.get('real_name', '').strip(), d.get('password', '')
    if not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username):
        raise ApiError('用户名需 3–32 位字母、数字或下划线')
    if not 1 <= len(name) <= 64:
        raise ApiError('请填写真实姓名（最多 64 字）')
    validate_password(pw)
    if AdminUser.query.filter_by(username=username).first():
        raise ApiError('用户名已存在', 409)
    u = AdminUser(username=username, real_name=name, password_hash=generate_password_hash(pw),
                  role='user', active=True, must_change_password=True)
    db.session.add(u)
    db.session.flush()
    audit('account_created', u.id)
    db.session.commit()
    return jsonify(ok=True, item=public_user(u))


@auth_bp.post('/users/<int:uid>/status')
@admin_required
def user_status(uid):
    u = db.session.get(AdminUser, uid)
    if not u or u.role == 'admin':
        raise ApiError('不能停用管理员或不存在的账号')
    active = (request.get_json(silent=True) or {}).get('active')
    if not isinstance(active, bool):
        raise ApiError('缺少启用状态')
    u.active = active
    revoke(u)
    audit('account_enabled' if active else 'account_disabled', uid)
    db.session.commit()
    return jsonify(ok=True)


@auth_bp.post('/users/<int:uid>/reset-password')
@admin_required
def reset_password(uid):
    u = db.session.get(AdminUser, uid)
    if not u or u.role == 'admin':
        raise ApiError('管理员密码请通过本机恢复命令重置')
    pw = (request.get_json(silent=True) or {}).get('password', '')
    validate_password(pw)
    u.password_hash, u.must_change_password = generate_password_hash(pw), True
    u.failed_logins, u.locked_until = 0, 0
    revoke(u)
    audit('password_reset', uid)
    db.session.commit()
    return jsonify(ok=True)


def install(app):
    app.register_blueprint(auth_bp)

    @app.before_request
    def protect_api():
        g.pop('auth_user', None)
        g.pop('auth_session', None)
        if not request.path.startswith('/api/'):
            return
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                raise ApiError('不允许跨站请求', 403)
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise ApiError('不允许跨站请求', 403)
        if request.path not in ('/api/login', '/api/me', '/api/logout', '/api/password'):
            require_user()

    @app.after_request
    def headers(response):
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.errorhandler(ApiError)
    def api_error(e):
        return jsonify(ok=False, msg=e.message, code=e.code), e.status

    @app.errorhandler(SQLAlchemyError)
    def database_error(e):
        db.session.rollback()
        import traceback
        current_app.logger.error('Database operation failed (%s)\n%s', type(e).__name__, traceback.format_exc())
        return jsonify(ok=False, msg='数据库不可用或保存失败，操作未确认完成，请稍后重试'), 503
