"""Legacy automatic demo initialization has been retired."""
def init_db(app):
    raise RuntimeError('禁止自动生成演示账号；请使用已审查的数据库迁移和 manage_auth.py admin 命令')
