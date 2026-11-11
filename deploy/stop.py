"""Stop only the project foreground supervisor recorded by start.sh."""
import os
from pathlib import Path
import signal
import time

root=Path(__file__).resolve().parents[1]
record=root/'runtime/foreground.pid'
if not record.exists():
    raise SystemExit('没有运行中的项目启动器记录；请检查原启动终端。')
pid=int(record.read_text().strip())
proc=Path('/proc')/str(pid)
if not proc.exists():
    raise SystemExit('启动器进程已退出。')
args=(proc/'cmdline').read_bytes().split(b'\0')
expected=[b'deploy/foreground.py',os.fsencode(root/'deploy/foreground.py')]
if proc.stat().st_uid!=os.getuid() or (proc/'cwd').resolve()!=root or not any(x in args for x in expected):
    raise SystemExit('进程身份不匹配，未停止任何进程。')
os.kill(pid,signal.SIGTERM)
for _ in range(250):
    if not proc.exists():
        print('本地LLM、检索模型、规则库worker和Web均已停止，Kingbase保持运行。')
        break
    time.sleep(.1)
else:
    raise SystemExit('服务尚在退出，请查看原启动终端日志。')
