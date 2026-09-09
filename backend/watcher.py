#!/usr/bin/env python3
"""Local state and CLI adapter. No third-party packages, credential reads or shell commands."""
import contextlib, datetime, fcntl, json, os, pathlib, re, sqlite3, subprocess, sys, time, uuid

HOME = pathlib.Path.home()
ROOT = pathlib.Path(os.environ.get('NIGHT_WATCHER_DATA_HOME', HOME / 'Library/Application Support/Night Watcher'))
CODEX_HOME = pathlib.Path(os.environ.get('CODEX_HOME', HOME / '.codex'))
DEFAULT_PROMPT = 'Continue from where you stopped. Review the existing changes first and continue the original task.'
DEFAULT_CLI = '/Applications/ChatGPT.app/Contents/Resources/codex'

def prepare():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ROOT, 0o700)

@contextlib.contextmanager
def transaction():
    prepare()
    with (ROOT / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = ROOT / 'registry.json'
        state = json.loads(path.read_text()) if path.exists() else {'tasks': {}, 'prompt': DEFAULT_PROMPT, 'cli': DEFAULT_CLI, 'events': []}
        yield state
        tmp = ROOT / 'registry.tmp'
        tmp.write_text(json.dumps(state, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

def event(state, title, body):
    state['events'] = (state.get('events', []) + [{'id': str(uuid.uuid4()), 'title': title, 'body': body}])[-40:]

def tail(path):
    with open(path, 'rb') as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - 2 * 1024 * 1024))
        if size > 2 * 1024 * 1024: f.readline()
        return f.read().decode('utf-8', errors='replace')

def epoch(value):
    if isinstance(value, (int, float)) and value > 1_000_000_000:
        return value / 1000 if value > 10_000_000_000 else value
    if isinstance(value, str):
        try: return epoch(float(value))
        except ValueError:
            try: return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except ValueError: pass
    return None

def reset_from(obj):
    """Only accept explicit reset timestamps; never invent a quota window."""
    values = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ('resets_at', 'resetsAt', 'reset_at', 'resetAt'):
                value = epoch(value)
                if value: values.append(value)
            elif isinstance(value, (dict, list)):
                value = reset_from(value)
                if value: values.append(value)
    elif isinstance(obj, list):
        values = [v for item in obj if (v := reset_from(item))]
    return max(values) if values else None

def quota_reset(quota):
    if not isinstance(quota, dict): return None
    windows = [v for v in quota.values() if isinstance(v, dict) and v.get('used_percent', v.get('usedPercent', 0)) >= 100]
    return reset_from(windows)

def rate_error(obj):
    text = json.dumps(obj).lower()
    return any(x in text for x in ('usage_limit_reached', 'usagelimitexceeded', 'rate_limit_exceeded', 'usage limit', 'rate limit', 'rate_limit_reached'))

def inspect_log(text):
    status, reset, message, quota = 'Idle', None, '', None
    for line in text.splitlines():
        try: e = json.loads(line)
        except ValueError: continue
        if not isinstance(e, dict): continue
        # Inspect protocol events only, never assistant/user prose or tool output.
        p = e.get('payload', {}) if e.get('type') == 'event_msg' else e
        if not isinstance(p, dict): continue
        t = p.get('type')
        if t == 'token_count': quota = p.get('rate_limits')
        if t in ('task_started', 'turn.started'):
            status, reset, message = 'Running', None, ''
        elif t in ('task_complete', 'turn.completed'):
            if status != 'Waiting': status, message = 'Completed', 'Latest Codex turn finished; this does not certify the whole project is complete.'
        elif t in ('turn_aborted', 'request_user_input', 'approval_request'):
            status, reset, message = 'Needs Input', None, 'Codex stopped or needs your input.'
        elif t in ('error', 'turn.failed'):
            message = str(p.get('message') or p.get('error') or p)[:1000]
            if rate_error(p):
                reset = reset_from(p) or quota_reset(quota)
                # Also accept a literal ISO timestamp in an error message.
                if not reset:
                    m = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})', message)
                    if m: reset = epoch(m.group())
                status = 'Waiting' if reset else 'Needs Input'
                if not reset: message += ' Reset time unavailable; set it manually.'
            else: status, reset = 'Needs Input', None
    return status, reset, message

def metadata():
    db = CODEX_HOME / 'state_5.sqlite'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2) as c:
        c.row_factory = sqlite3.Row
        return [dict(row) for row in c.execute("select id,title,cwd,rollout_path,updated_at from threads where archived=0 and (agent_path is null or agent_path='') order by updated_at desc limit 300")]

def worker_alive(task):
    run = task.get('run')
    if not run: return False
    # A held flock is authoritative; PIDs alone can be reused.
    with (ROOT / (task['id'] + '.run.lock')).open('a') as f:
        try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
    return time.time() - run['started'] < 10  # process startup grace

def refresh(state):
    rows = metadata()
    known = {r['id'] for r in rows}
    for row in rows:
        task = state['tasks'].get(row['id'])
        if task is None:
            task = dict(row, state='Idle', armed=False, reset=None, note='', fingerprint=None)
            state['tasks'][row['id']] = task
        task.update(row)
        task['title'] = row['title'].splitlines()[0][:160] or 'Untitled task'
        task['available'] = True
    for task in state['tasks'].values():
        if task['id'] not in known: task['available'] = False
        if task.get('run'):
            if worker_alive(task): continue
            task.update(state='Needs Input', reset=None, run=None, note='Watcher run was interrupted. Review the task before resuming.')
            event(state, 'Task needs attention', task['title'] + ': interrupted run')
        if not task.get('available'): continue
        try:
            st = os.stat(task['rollout_path'])
            fp = [st.st_mtime_ns, st.st_size]
            if fp != task.get('fingerprint'):
                status, reset, note = inspect_log(tail(task['rollout_path']))
                task['fingerprint'] = fp
                # A new turn always supersedes an old manually scheduled continuation.
                if task.get('manual') and status not in ('Running', 'Completed'): continue
                task.update(state=status, reset=reset, note=note, manual=False)
        except OSError as e:
            task.update(state='Needs Input', reset=None, note='Session history unavailable: ' + str(e))

def launch(state, task):
    if task.get('run') or task['state'] == 'Running': raise ValueError('Task is already running. Finish or stop it in Codex first.')
    if not task.get('available'): raise ValueError('Task is archived or not available in the local registry.')
    if any(t['id'] != task['id'] and t['state'] == 'Running' and os.path.realpath(t['cwd']) == os.path.realpath(task['cwd']) for t in state['tasks'].values()):
        raise ValueError('Another Codex task is running in this working directory.')
    if not pathlib.Path(task['cwd']).is_dir(): raise ValueError('Working directory no longer exists.')
    if not os.access(state['cli'], os.X_OK): raise ValueError('Codex executable was not found. Update its path in Settings.')
    # Serialize all watcher continuations; never launch two against the same workspace.
    if any(t.get('run') for t in state['tasks'].values()): raise ValueError('Another watcher continuation is running.')
    token = str(uuid.uuid4())
    task.update(state='Running', reset=None, manual=False, run={'token': token, 'started': time.time()}, note='Starting Codex continuation…')
    try:
        with (ROOT / 'worker-errors.log').open('ab') as log:
            subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), 'worker', task['id'], token], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except Exception:
        task.update(state='Needs Input', run=None, note='Could not launch the worker.')
        raise

def worker(task_id, token):
    prepare()
    with (ROOT / (task_id + '.run.lock')).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token: return
            cli, prompt, cwd = state['cli'], state['prompt'], task['cwd']
            event(state, 'Task resumed', task['title'])
        path = ROOT / (task_id + '.jsonl')
        status, reset, note = 'Needs Input', None, 'Codex did not finish successfully.'
        try:
            # Direct argv: paths, IDs and prompts never pass through a shell.
            args = [cli, '-a', 'never', 'exec', '-s', 'workspace-write', 'resume', '--skip-git-repo-check', '--json', task_id, '-']
            with path.open('wb') as output:
                p = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
                p.communicate(prompt.encode())
            status, reset, note = inspect_log(tail(path))
            if p.returncode != 0 and status not in ('Waiting', 'Needs Input'):
                status, reset, note = 'Needs Input', None, 'Codex exited with code %s. Open the run log.' % p.returncode
            if status in ('Idle', 'Running'):
                status, reset, note = 'Needs Input', None, 'No terminal Codex event was received. Open the run log.'
        except Exception as e: note = str(e)
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token: return
            # Block blind retry loops if the reported reset has already elapsed.
            if reset and reset <= time.time():
                status, reset, note = 'Needs Input', None, 'Codex reported a reset time that has already passed. Set a new time or Resume Now.'
            task.update(state=status, reset=reset, run=None, note=note)
            try:
                st = os.stat(task['rollout_path']); task['fingerprint'] = [st.st_mtime_ns, st.st_size]
            except OSError: pass
            event(state, 'Task ' + ('finished' if status == 'Completed' else 'needs attention'), task['title'] + ': ' + note[:200])

def command(request):
    with transaction() as state:
        op = request.get('op', 'snapshot')
        error = None
        try: refresh(state)
        except (sqlite3.Error, OSError) as e: error = 'Cannot read local Codex sessions: ' + str(e)
        if op in ('arm', 'schedule', 'resume', 'idle'):
            task = state['tasks'][request['id']]
            if op == 'arm':
                task['armed'] = bool(request['armed'])
            elif op == 'schedule':
                if task['state'] == 'Running': raise ValueError('Task is running. Stop it in Codex before scheduling.')
                reset = float(request['reset'])
                if reset <= time.time(): raise ValueError('Choose a future reset time.')
                task.update(state='Waiting', armed=True, reset=reset, manual=True, note='Reset time entered manually.')
            elif op == 'idle':
                if task.get('run'): raise ValueError('The watcher still has an active worker.')
                task.update(state='Idle', reset=None, manual=False, note='Marked idle by you. Ensure Codex is no longer running this task.')
            else:
                if error: raise ValueError(error)
                launch(state, task)
        elif op == 'settings':
            prompt = request['prompt'].strip()
            if not prompt: raise ValueError('Continuation prompt cannot be empty.')
            cli = os.path.expanduser(request['cli'])
            if not os.access(cli, os.X_OK): raise ValueError('Choose an executable Codex CLI path.')
            state.update(prompt=prompt, cli=cli)
        if op == 'tick' and not error and not any(t.get('run') for t in state['tasks'].values()):
            due = [t for t in state['tasks'].values() if t['armed'] and t['state'] == 'Waiting' and t.get('reset') and t['reset'] + 15 <= time.time()]
            if due:
                task = min(due, key=lambda t: t['reset'])
                try: launch(state, task)
                except ValueError as e:
                    task.update(state='Needs Input', reset=None, note=str(e))
                    event(state, 'Resume failed', task['title'] + ': ' + str(e))
        return dict(state, tasks=sorted(state['tasks'].values(), key=lambda t: (not t['armed'], -t['updated_at'])), error=error)

if __name__ == '__main__':
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == 'worker': worker(sys.argv[2], sys.argv[3])
    else:
        try: print(json.dumps(command(json.load(sys.stdin))))
        except Exception as e: print(json.dumps({'error': str(e)})); sys.exit(1)
