"""Local administrator recovery. Passwords are entered without echo."""
import getpass
import argparse
import time
from flask import Flask
from werkzeug.security import generate_password_hash
from sqlalchemy import text
from models import db, AdminUser, LoginSession, AuthAudit, QaLog
import config


def main():
    parser = argparse.ArgumentParser(description='管理员本机初始化/恢复；不提供默认密码')
    parser.add_argument('command', choices=['admin', 'recover-interrupted'])
    args = parser.parse_args()
    if not config.AUTH_DATABASE_URL:
        raise SystemExit('账号数据库尚未配置')
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=config.DATABASE_URL,
                      SQLALCHEMY_BINDS={'auth': config.AUTH_DATABASE_URL})
    db.init_app(app)
    with app.app_context():
        if args.command == 'recover-interrupted':
            # Run ONLY while server is stopped, before a single local server restart.
            count = QaLog.query.filter_by(status='generating').update({'status':'interrupted'})
            db.session.commit()
            print('已标记中断记录:', count)
            return
        admins = AdminUser.query.filter_by(role='admin').all()
        if len(admins) > 1:
            raise SystemExit('发现多个管理员，请先确定唯一管理员，未作修改')
        user = admins[0] if admins else None
        if user:
            print('恢复现有管理员:', user.username)
            username = user.username
        else:
            import re
            username = input('管理员用户名: ').strip()
            if not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username):
                raise SystemExit('用户名格式不正确')
        name = input('真实姓名（必填）: ').strip()
        pw = getpass.getpass('临时密码（8–128 位，输入不显示）: ')
        confirm = getpass.getpass('再次输入临时密码: ')
        if not name or len(name) > 64 or not 8 <= len(pw) <= 128 or pw != confirm:
            raise SystemExit('姓名或密码不符合要求，未修改')
        if not user:
            if AdminUser.query.filter_by(username=username).first():
                raise SystemExit('用户名已存在，未修改')
            user=AdminUser(username=username,role='admin',auth_version=0)
            db.session.add(user)
        user.real_name=name
        user.password_hash=generate_password_hash(pw)
        user.must_change_password=True
        user.active=True
        user.failed_logins=0
        user.locked_until=0
        user.auth_version+=1
        db.session.flush()
        LoginSession.query.filter_by(user_id=user.id).update({'revoked':True})
        db.session.add(AuthAudit(action='local_admin_recovery',target=str(user.id),happened_at=int(time.time())))
        db.session.commit()
        print('管理员已恢复，全部旧登录已撤销；首次登录必须修改密码。')


if __name__ == '__main__':
    main()
