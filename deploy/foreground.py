"""Own two service process groups; stream logs and stop both on Ctrl+C."""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / 'src'
children = []
workers = []      # 规则库 worker 单独记账：尽力而为组件，退出不应拖垮整套服务
stopping = False

def interrupted(signum, frame):
    global stopping
    stopping = True

def occupied(port):
    with socket.socket() as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


LLAMA_BIN = os.environ.get('LOCAL_LLM_SERVER', '/home/adminroot/Downloads/llama-src/build/bin/llama-server')
EMBED_GGUF = os.environ.get('LOCAL_EMBED_GGUF', '/home/adminroot/Downloads/llama-b11753/models/Qwen3-Embedding-4B-Q4_K_M.gguf')
RERANK_GGUF = os.environ.get('LOCAL_RERANK_GGUF', '/home/adminroot/Downloads/llama-b11753/models/Qwen3-Reranker-4B.Q4_K_M.gguf')


def ensure_qwen3_tokenizer():
    """Qwen3 词表（vector_safety 精确 token 预算用）：缺失自动下载。"""
    import urllib.request as _u
    d = ROOT / 'models' / 'qwen3-embedding-4b'
    d.mkdir(parents=True, exist_ok=True)
    tok = d / 'tokenizer.json'
    if not (tok.exists() and tok.stat().st_size > 1_000_000):
        print('下载 Qwen3 tokenizer（约 11MB）...', flush=True)
        for url in ('https://hf-mirror.com/Qwen/Qwen3-Embedding-4B/resolve/main/tokenizer.json',
                    'https://huggingface.co/Qwen/Qwen3-Embedding-4B/resolve/main/tokenizer.json'):
            try:
                _u.urlretrieve(url, tok)
                if tok.exists() and tok.stat().st_size > 1_000_000:
                    break
            except Exception:
                try: tok.unlink()
                except OSError: pass
        if not (tok.exists() and tok.stat().st_size > 1_000_000):
            raise RuntimeError('Qwen3 tokenizer 缺失且下载失败，请手动放到 %s' % tok)
    cfg = d / 'sentence_bert_config.json'
    if not cfg.exists():
        cfg.write_text('{"max_seq_length": 32768}\n', encoding='utf8')


def start_llama(port, args, timeout=240):
    log = open(ROOT / ('runtime/logs/service_%d.log' % port), 'w')
    process = subprocess.Popen([LLAMA_BIN] + args, start_new_session=True,
                               stdout=log, stderr=subprocess.STDOUT)
    children.append(process)
    if not wait_ready(port, '/health', process, timeout):
        return None
    return process


def start_qwen3_vector_stack():
    """2560 维嵌入(8095) + Qwen3 重排(8097) + DashScope 兼容层(8096)。"""
    for f in (EMBED_GGUF, RERANK_GGUF):
        if not Path(f).is_file():
            raise RuntimeError('缺少实验向量模型文件：%s' % f)
    ensure_qwen3_tokenizer()
    emb = start_llama(8095, ['-m', EMBED_GGUF, '--alias', 'qwen3-embedding-4b', '--embeddings',
                             '--pooling', 'last', '-ngl', '99', '-c', '8192', '-b', '2048',
                             '-ub', '2048', '--port', '8095', '--host', '127.0.0.1'])
    if emb is None:
        return False
    rr = start_llama(8097, ['-m', RERANK_GGUF, '--alias', 'qwen3-reranker-4b', '--jinja',
                            '-ngl', '99', '-c', '8192', '-b', '2048', '-ub', '2048',
                            '--parallel', '8', '--port', '8097', '--host', '127.0.0.1'])
    if rr is None:
        return False
    py = '/home/adminroot/miniconda3/envs/smart-help-web/bin/python'
    log = open(ROOT / 'runtime/logs/service_8096.log', 'w')
    shim = subprocess.Popen([py, '-u', 'serve_qwen3_rerank.py', '--port', '8096',
                             '--llm', 'http://127.0.0.1:8097', '--workers', '8'],
                            cwd=str(ADMIN), start_new_session=True,
                            stdout=log, stderr=subprocess.STDOUT)
    children.append(shim)
    if not wait_ready(8096, '/health', shim, 90):
        return False
    return True

def launch_worker():
    """规则库懒编译 worker（C3/C4 依赖：补编译缺失县区的规则包，并做每日影子巡检）。"""
    python = '/home/adminroot/miniconda3/envs/smart-help-web/bin/python'
    interval = os.environ.get('RULE_WORKER_INTERVAL', '60')
    command = [python, '-u', 'rule_worker.py', '--watch', '--interval', interval]
    process = subprocess.Popen(command, cwd=str(ADMIN), start_new_session=True)
    children.append(process)
    workers.append(process)
    return process

def lan_ip():
    """尽力探测本机局域网地址，仅用于启动提示；失败不阻塞启动。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('10.255.255.255', 1))   # UDP connect 不发包，仅让内核选源地址
            return probe.getsockname()[0]
    except OSError:
        return None

def launch(environment, port, application, extra):
    executable = f'/home/adminroot/miniconda3/envs/{environment}/bin/gunicorn'
    # WEB_BIND_HOST=0.0.0.0 时对局域网开放；默认只监听本机回环。
    host = os.environ.get('WEB_BIND_HOST') or '127.0.0.1'
    command = [executable, '--chdir', str(ADMIN), '--bind', f'{host}:{port}',
               '--workers', '1', '--timeout', '600', '--graceful-timeout', '15',
               '--error-logfile', '-', '--access-logfile', '-', '--capture-output',
               '--control-socket', str(ROOT / f'runtime/foreground-{port}.ctl')]
    process = subprocess.Popen(command + extra + [application], start_new_session=True)
    children.append(process)
    return process

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def wait_ready(port, path, process, timeout, model=False):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if stopping:
            return False
        if process.poll() is not None:
            raise RuntimeError(f'{port} 服务提前退出，退出码 {process.returncode}')
        try:
            with opener.open(f'http://127.0.0.1:{port}{path}', timeout=2) as response:
                if not model:
                    return True
                data = json.load(response)
                if all(data.get(k) is True for k in ['ok', 'embedder', 'reranker']):
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f'{port} 服务就绪超时，请查看上方日志')

def check_llm_context(min_ctx=16384):
    """自检单请求可用上下文：--ctx-size 会被 --parallel 整除，配错会让所有问答 400。"""
    with opener.open('http://127.0.0.1:8081/props', timeout=5) as response:
        data = json.load(response)
    n_ctx = (data.get('default_generation_settings') or {}).get('n_ctx')
    slots = data.get('total_slots')
    if not isinstance(n_ctx, int) or n_ctx < min_ctx:
        raise RuntimeError(
            f'单请求上下文仅 {n_ctx} token（并发槽 {slots}），低于应用所需 {min_ctx}。'
            f'请把 local_llm.py 的 --ctx-size 设为 parallel×{min_ctx}（当前 parallel=4 → 65536）。')
    print(f'上下文自检通过：单请求 {n_ctx} token，并发槽 {slots}', flush=True)

def main():
    os.chdir(ROOT)
    lock = open(ROOT / 'runtime/foreground.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('前台启动器已运行，请在原终端操作。', flush=True)
        return 1
    ports = [8081, 8095, 8096, 8097, 5000]
    for port in ports:
        if occupied(port):
            print(f'{port} 已被占用；请先停止已有服务。本脚本不会接管或终止未知进程。', flush=True)
            return 1
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        from local_llm import command
        llm=subprocess.Popen(command(),start_new_session=True)
        children.append(llm)
        if not wait_ready(8081, '/health', llm, 240):
            return 0
        check_llm_context()
        if not start_qwen3_vector_stack():
            return 0
        web = launch('smart-help-web', 5000, 'app:app', ['--worker-class', 'gthread', '--threads', '2'])
        if not wait_ready(5000, '/ask', web, 60):
            return 0
        worker = launch_worker()
        time.sleep(3)
        worker_failures = 0
        if worker.poll() is not None:
            print(f'警告：规则库懒编译 worker 未能常驻（退出码 {worker.returncode}），'
                  'Web 与规则引擎仍可用，仅新县区规则不会自动补编译。', flush=True)
            worker_failures = 1
        (ROOT/'runtime/foreground.pid').write_text(str(os.getpid()))
        bind_host = os.environ.get('WEB_BIND_HOST') or '127.0.0.1'
        access_line = f'\n服务已就绪：http://127.0.0.1:5000/ask\n'
        if bind_host == '0.0.0.0':
            ip = lan_ip()
            access_line += (f'局域网访问：http://{ip}:5000/ask' if ip else
                            '局域网访问：已监听 0.0.0.0:5000（未探测到本机地址）')
            access_line += '（管理后台 /admin 同样可用，请确认网络环境可信）\n'
        print(access_line +
              '本地对话模型：本地Qwen3.6-35B / qwen3.6-35b-a3b（8081，MTP n-max 2）\n'
              '向量：Qwen3-Embedding-4B（2560 维，8095）｜重排：Qwen3-Reranker-4B（8097 + 8096 兼容层）\n' +
              '规则库懒编译 worker：--watch 模式（RULE_WORKER_INTERVAL）\n'
              '日志在此终端显示，链路日志同时保存在 runtime/logs。按 Ctrl+C 停止本次启动的全部服务。\n', flush=True)
        while not stopping:
            # 致命组件（LLM/嵌入/重排/Web）任一退出就整体停服；
            # 规则库 worker 是尽力而为组件：退出只影响新县区补编译与巡检，退避重启，不停服。
            for process in children:
                if process in workers:
                    continue
                if process.poll() is not None:
                    raise RuntimeError(f'服务进程 {process.pid} 退出：{process.returncode}')
            if worker is None or worker.poll() is not None:
                code = None if worker is None else worker.returncode
                worker_failures += 1
                delay = min(60, 3 * worker_failures)
                print(f'警告：规则库 worker 未在运行（退出码 {code}），{delay}s 后重启（第 {worker_failures} 次）；'
                      'Web 与规则引擎不受影响。', flush=True)
                deadline = time.monotonic() + delay
                while not stopping and time.monotonic() < deadline:
                    time.sleep(0.5)
                if stopping:
                    break
                try:
                    worker = launch_worker()
                except Exception as exc:
                    print(f'规则库 worker 重启失败：{exc}', flush=True)
                    worker = None
            time.sleep(0.25)
        return 0
    finally:
        print('\n正在停止本地LLM、检索模型、规则库worker和 Web 服务……', flush=True)
        for process in reversed(children):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 20
        for process in reversed(children):
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        print('本次启动的服务已停止；Kingbase 保持运行。', flush=True)
        pidfile=ROOT/'runtime/foreground.pid'
        if pidfile.exists() and pidfile.read_text().strip()==str(os.getpid()):
            pidfile.unlink()

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'启动失败：{exc}', file=sys.stderr)
        sys.exit(1)
