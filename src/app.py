# -*- coding: utf-8 -*-
"""管理后台入口：Flask 服务 + 静态前端"""
import logging
import os
from datetime import timedelta
from flask import Flask, send_from_directory
from config import DATABASE_URL, SECRET_KEY, UPLOAD_DIR, MAX_CONTENT_LENGTH
from models import db

_LOG = logging.getLogger(__name__)
_ON_VALUES = ('1', 'on', 'true', 'yes')


def kg_worker_in_full_model_enabled():
    """KG_WORKER_IN_FULL_MODEL=1/on/true/yes 时，full_model 下也启动图谱更新 worker。

    默认关闭：full_model 是生产主链路，图谱重建会长时间持有 policy/kg 表锁并整表重写，
    必须等积压与向量链路验证完成后由运维显式打开，保证现网零行为变化。
    """
    return str(os.environ.get('KG_WORKER_IN_FULL_MODEL', '') or '').strip().lower() in _ON_VALUES


def _start_kg_worker(full_model):
    """启动图谱更新 worker 并打印一行启动原因；线程内部仍受 KG_AUTO_UPDATE 门控。"""
    from kg_source_update import start_worker
    start_worker()
    _LOG.info('KG 图谱更新 worker 已启动：%s',
              'full_model（KG_WORKER_IN_FULL_MODEL 已开启）' if full_model else '传统规则模式')


def create_app():
    app = Flask(__name__, static_folder='static', static_url_path='')
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
    # JWT 固定有效期由 authentication.TTL 管理。
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    from config import AUTH_DATABASE_URL, AUTH_JWT_SECRET
    if not AUTH_DATABASE_URL or len(AUTH_JWT_SECRET) < 32:
        raise RuntimeError('请先运行账号数据库迁移初始化命令')
    app.config['SQLALCHEMY_BINDS'] = {'auth': AUTH_DATABASE_URL}
    app.config['AUTH_JWT_SECRET'] = AUTH_JWT_SECRET
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
    db.init_app(app)

    from api import bp
    app.register_blueprint(bp)

    @app.route('/')
    def index():
        return send_from_directory('static', 'index.html')

    @app.route('/ask')
    def ask_page():
        """普通用户独立问答页（登录后直达；管理后台设按钮跳转）"""
        from pathlib import Path
        from policy_core.showcase import enabled as showcase_enabled
        from policy_core.full_settings import enabled as full_enabled

        def _html(page):
            # 政务内网浏览器与代理缓存旧版页面会导致"改版不生效/整页滚动"等旧问题复发，
            # 问答页必须每次回源：no-store + must-revalidate
            resp = app.response_class(page, mimetype='text/html')
            resp.headers['Cache-Control'] = 'no-store, must-revalidate'
            resp.headers['Pragma'] = 'no-cache'
            resp.headers['Expires'] = '0'
            return resp

        page = (Path(app.static_folder) / 'ask.html').read_text(encoding='utf8')
        if full_enabled():
            page = page.replace('回答内容来自最新政策文件，请以官方发布为准', '原全库模型分析；部分满足不等于完整资格')
            page = page.replace('我会自动匹配适用政策，生成匹配表格、办理指引，并导出 Excel。', '我会检索原政策库，分析已满足条件与缺项，按需追问，并生成有依据的辅助建议和 Excel。')
            page = page.replace('回答基于知识库最新政策，具体以主管部门解释为准', '全库模型分析；来源缺项如实说明，不推测补贴金额')
            page = page.replace('@click="openSurvey"', 'v-if="false" @click="openSurvey"')
            return _html(page)
        if showcase_enabled():
            page = page.replace('回答内容来自最新政策文件，请以官方发布为准', '莲花县7项政策资料展示；相关线索不等于资格结论')
            page = page.replace('“脱贫人口小额信贷贴息怎么申请？”', '“请分析莲花县两名老人，其中一人二级肢体残疾的相关政策”')
            page = page.replace('我会自动匹配适用政策，生成匹配表格、办理指引，并导出 Excel。', '我会整理相关政策线索、按缺项追问，并提供依据卡片及 Excel；本版不确认现行资格和金额。')
            page = page.replace('回答基于知识库最新政策，具体以主管部门解释为准', '展示范围：莲花县7项来源资料；完整资格和现行标准待核验')
            page = page.replace('@click="openSurvey"', 'v-if="false" @click="openSurvey"')
            return _html(page)
        return _html(page)

    @app.route('/admin')
    def admin_page():
        """政策管理后台页面：只返回静态页面，所有数据由前端显式调用 API。

        与问答页同样关闭缓存：政务内网浏览器/代理缓存旧页面会让后台改版不生效。
        本路由不读库、不写库；登录态、管理员权限与功能开关全部由后端 API 各自校验。
        """
        from pathlib import Path
        resp = app.response_class(
            (Path(app.static_folder) / 'admin.html').read_text(encoding='utf8'),
            mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        return resp

    from authentication import install
    from conversations import chat_bp
    install(app)
    app.register_blueprint(chat_bp)
    from model_admin import model_bp
    app.register_blueprint(model_bp)
    from chain_trace import install as install_trace
    install_trace(app)
    import config
    from policy_core.showcase import enabled as showcase_enabled
    from policy_core.full_settings import enabled as full_enabled
    if config.KG_GRAPH_ENABLED and not showcase_enabled():
        # KG 图谱更新 worker：传统规则链路保持现网无条件启动；full_model 原先恒不启动，
        # 导致 kg_update_queue 长期积压、kg_node 停止更新。这里用独立开关灰度放行，
        # 默认关闭，现网行为不变。
        if not full_enabled():
            _start_kg_worker(full_model=False)
        elif kg_worker_in_full_model_enabled():
            _start_kg_worker(full_model=True)
        else:
            _LOG.info('KG 图谱更新 worker 未启动：full_model 模式下 KG_WORKER_IN_FULL_MODEL 未开启（默认关闭）')
    elif not config.KG_GRAPH_ENABLED:
        _LOG.info('KG 图谱更新 worker 未启动：KG_GRAPH_ENABLED 关闭')
    else:
        _LOG.info('KG 图谱更新 worker 未启动：展示模式（showcase）不启动')
    from policy_core import enabled
    from policy_core.routes import bp as core_bp
    app.register_blueprint(core_bp)
    if enabled() and not showcase_enabled() and not full_enabled():
        from policy_core.publishing import start
        start()
    # 政策生命周期 outbox 消费者：默认关（POLICY_WORKER=1 才启动），
    # 与 full_model/showcase 无关 —— 生产 full_model 下也要能消费导入/停用事件。
    from policy_core import policy_worker
    policy_worker.start()
    # 政策自动管理定时体检 worker：默认关（POLICY_AUTOPILOT_WORKER=1 才启动），
    # 关闭时不建线程、不连库，现网零行为变化；apply 仍由 POLICY_AUTOPILOT 单独控制。
    from policy_core import autopilot
    autopilot.start()
    return app


app = create_app()

if __name__ == '__main__':
    import socket
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', 5000)) == 0:
            raise SystemExit('5000 端口已被占用，请勿重复启动')
    # No server is listening: unfinished turns from a previous crash cannot resume.
    with app.app_context():
        from models import QaLog
        QaLog.query.filter_by(status='generating').update({'status': 'interrupted'})
        db.session.commit()
    # WEB_BIND_HOST=0.0.0.0 时对局域网开放；默认只监听本机回环。
    host = os.environ.get('WEB_BIND_HOST') or '127.0.0.1'
    print(f'管理后台已启动: http://{host}:5000')
    app.run(host=host, port=5000, debug=False)
