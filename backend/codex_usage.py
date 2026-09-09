"""Read-only Codex usage adapter. Never starts a turn or consumes a reset credit."""
import json, math, os, selectors, subprocess, time


def read_limits(cli, timeout=12):
    p = subprocess.Popen([cli, 'app-server', '--stdio'], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(p.stdout, selectors.EVENT_READ)
    pending = b''
    deadline = time.monotonic() + timeout
    def send(value):
        p.stdin.write((json.dumps(value) + '\n').encode()); p.stdin.flush()
    def receive(request_id):
        nonlocal pending
        while time.monotonic() < deadline:
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                try: response = json.loads(line)
                except ValueError: continue
                if isinstance(response, dict) and response.get('id') == request_id:
                    if 'error' in response: raise RuntimeError('Codex could not read usage. Check that the CLI is signed in with your ChatGPT account.')
                    return response.get('result', {})
            if selector.select(max(0, deadline - time.monotonic())):
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk: raise RuntimeError('Codex usage connection closed.')
                pending += chunk
                if len(pending) > 2 * 1024 * 1024: raise RuntimeError('Unexpected Codex usage response.')
        raise TimeoutError('Codex usage request timed out.')
    try:
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'ajo_night_watcher', 'version': '0.1.0'}}})
        receive(1)
        send({'method': 'initialized'})
        send({'id': 2, 'method': 'account/rateLimits/read'})
        return receive(2)
    finally:
        selector.close()
        p.terminate()
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
        p.stdin.close(); p.stdout.close()


def normalize(response):
    buckets = response.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        legacy = response.get('rateLimits')
        buckets = {'codex': legacy} if isinstance(legacy, dict) else {}
    result = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict): continue
        windows = []
        for kind in ('primary', 'secondary'):
            window = bucket.get(kind)
            if not isinstance(window, dict): continue
            used = window.get('usedPercent')
            remaining = max(0, min(100, 100 - used)) if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) else None
            duration = window.get('windowDurationMins')
            duration = duration if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else None
            reset = window.get('resetsAt')
            reset = reset if isinstance(reset, (int, float)) and math.isfinite(reset) and reset > 0 else None
            windows.append({'id': kind, 'remaining': remaining, 'minutes': duration, 'reset': reset})
        result.append({'id': key, 'name': bucket.get('limitName') or ('Codex' if key == 'codex' else key), 'plan': bucket.get('planType'), 'windows': windows})
    return result


def snapshot(cli, cache):
    previous = {}
    try:
        previous = json.loads(cache.read_text())
        if previous.get('cli') != cli: previous = {}
    except (OSError, ValueError): pass
    try:
        buckets = normalize(read_limits(cli))
        value = {'cli': cli, 'buckets': buckets, 'updated': time.time(), 'stale': False,
                 'error': None if any(b['windows'] for b in buckets) else 'Usage windows are not available for this Codex account.'}
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = cache.with_suffix('.tmp')
        temporary.write_text(json.dumps(value)); temporary.chmod(0o600); os.replace(temporary, cache)
        return value
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        return dict(previous, buckets=previous.get('buckets', []), stale=True, error=str(error))
