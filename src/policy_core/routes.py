from flask import Blueprint,jsonify
from authentication import admin_required
from . import store
from .catalog import current
from .settings import ENGINE_VERSION,enabled

bp=Blueprint('policy_core','policy_core',url_prefix='/api/policy-core')

@bp.get('/status')
@admin_required
def status():
    catalog=current()
    with store.connection() as c:
        counts=dict(c.execute('SELECT p.status,count(*) FROM publication p JOIN active_source a ON a.digest=p.digest GROUP BY p.status').fetchall())
    return jsonify(ok=True,enabled=enabled(),engine_version=ENGINE_VERSION,catalog_version=catalog['revision'],inventory=catalog['inventory'],publication_states=counts,pilot=['医疗','残疾'],other_business='仅查询与线索，不作旧链路资格结论')
