"""Preview a vector backup; --apply restores exactly one policy in a transaction."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from sqlalchemy import create_engine, text
import config
from vector_safety import backup_vectors, _vector_table_name


def restore(path, apply=False):
    path = Path(path)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != path.with_suffix('.sha256').read_text().strip():
        raise ValueError('Backup checksum mismatch')
    data = json.loads(payload)
    code, rows = data['policy_code'], data['rows']
    if data.get('format') != 1 or any(row.get('policy_code') != code for row in rows):
        raise ValueError('Invalid backup policy scope')
    table = _vector_table_name(data.get('table'))
    print(json.dumps({'policy_code':code,'rows':len(rows),'apply':apply}, ensure_ascii=False))
    if not apply:
        return
    engine = create_engine(config.DATABASE_URL)
    with engine.begin() as c:
        p = c.execute(text('SELECT cur_version,status FROM policy WHERE policy_code=:pc FOR UPDATE'),
                      {'pc':code}).fetchone()
        if not p:
            raise ValueError('Policy no longer exists')
        if any(str(r.get('policy_version') or '') != str(p[0] or '') or r.get('status') != p[1] for r in rows):
            raise ValueError('Backup does not match current policy version/status; restore source first')
        # Fixed schema-derived identifiers; never execute arbitrary SQL from the backup.
        columns = set(c.execute(text('SELECT * FROM ' + table + ' WHERE 1=0')).keys())
        for row in rows:
            if set(row) != columns:
                raise ValueError('Backup schema differs from current vector table')
        recovery = backup_vectors(c, code, table=table)
        c.execute(text('DELETE FROM ' + table + ' WHERE policy_code=:pc'), {'pc':code})
        for row in rows:
            names = list(row)
            values = {k:base64.b64decode(v['base64']) if isinstance(v,dict) and set(v)=={'base64'} else v
                      for k,v in row.items()}
            quoted = ','.join('"'+k.replace('"','""')+'"' for k in names)
            c.execute(text('INSERT INTO '+table+' ('+quoted+') VALUES ('+
                           ','.join(':'+k for k in names)+')'),values)
    print('Restored. Pre-restore backup: '+recovery)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    restore(args.backup,args.apply)
