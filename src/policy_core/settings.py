import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('POLICY_CORE_DATA_DIR',str(ROOT.parent/'data'/'policy_core')))
ENGINE_VERSION='core-1.1'
PUBLISHER_VERSION='compiler-1.8'

def settings():
    return json.loads((ROOT/'settings.json').read_text('utf8'))

def enabled():
    value=os.environ.get('POLICY_CORE_ENABLED')
    return value.lower() in ('1','true','yes') if value is not None else settings()['enabled']
