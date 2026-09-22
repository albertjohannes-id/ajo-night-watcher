#!/usr/bin/env python3
"""Local OpenCode session monitor. No third-party packages, credential reads or shell commands.

Reads only the session/message/project tables of ~/.local/share/opencode/opencode.db
over a read-only SQLite connection. Never touches auth.json, account.json or any
credential store. Covers every model under the OpenCode provider (including Zen
free models) at the session layer; there is no account-wide Zen quota endpoint,
so rate-limit resets fall back to manual scheduling unless an explicit timestamp
is present in the recorded error.
"""
import contextlib, datetime, fcntl, json, os, pathlib, re, shutil, sqlite3, subprocess, sys, time, uuid

HOME = pathlib.Path.home()
ROOT = pathlib.Path(os.environ.get('AJO_NIGHT_WATCHER_DATA_HOME', HOME / 'Library/Application Support/Ajo Night Watcher'))
OPENCODE_HOME = pathlib.Path(os.environ.get('OPENCODE_HOME', HOME / '.local/share/opencode'))
DEFAULT_PROMPT = 'Continue from where you stopped. Review the existing changes first and continue the original task.'
DEFAULT_CLI = 'opencode'

REGISTRY = 'opencode_registry.json'
STATE_LOCK = 'opencode_state.lock'
WORKER_ERROR_LOG = 'opencode-worker-errors.log'


def prepare():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ROOT, 0o700)


@contextlib.contextmanager
def transaction():
    prepare()
    with (ROOT / STATE_LOCK).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = ROOT / REGISTRY
        state = json.loads(path.read_text()) if path.exists() else {'tasks': {}, 'prompt': DEFAULT_PROMPT, 'cli': DEFAULT_CLI, 'events': []}
        yield state
        tmp = ROOT / (REGISTRY + '.tmp')
        tmp.write_text(json.dumps(state, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def event(state, title, body):
    state['events'] = (state.get('events', []) + [{'id': str(uuid.uuid4()), 'title': title, 'body': body}])[-40:]


def epoch(value):
    if isinstance(value, (int, float)) and value > 1_000_000_000:
        return value / 1000 if value > 10_000_000_000 else value
    if isinstance(value, str):
        try:
            return epoch(float(value))
        except ValueError:
            try:
                return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
    return None


def reset_from_text(message):
    """Only accept explicit reset timestamps; never invent a quota window."""
    m = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})', message)
    if m:
        value = epoch(m.group())
        if value:
            return value
    m = re.search(r'(?i)(?:retry|reset|wait)[^\d]{0,20}(\d+)\s*(?:seconds?|secs?|s\b)', message)
    if m:
        try:
            seconds = int(m.group(1))
        except ValueError:
            seconds = 0
        if 0 < seconds <= 7 * 24 * 3600:
            return time.time() + seconds
    return None


def rate_error_text(text):
    lowered = text.lower()
    return ('429' in lowered or 'rate_limit' in lowered or 'rate limit' in lowered
            or 'quota' in lowered or 'credits_exhausted' in lowered
            or 'insufficient' in lowered and 'credit' in lowered
            or 'usage limit' in lowered or 'too many requests' in lowered)


def tail(path, limit=2 * 1024 * 1024):
    with open(path, 'rb') as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - limit))
        if size > limit:
            f.readline()
        return f.read().decode('utf-8', errors='replace')


def cli_executable(cli):
    if '/' in cli:
        return os.access(cli, os.X_OK)
    return shutil.which(cli) is not None


def discovery():
    """Read-only session list. Never reads auth/account/credential data."""
    db = OPENCODE_HOME / 'opencode.db'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2) as c:
        c.row_factory = sqlite3.Row
        projects = {}
        try:
            if c.execute("select 1 from sqlite_master where type='table' and name='project'").fetchone():
                projects = {r['id']: r['name'] for r in c.execute('select id,name from project')}
        except sqlite3.Error:
            projects = {}
        rows = c.execute(
            "select id,project_id,directory,title,model,agent,time_created,time_updated,time_archived"
            " from session where time_archived is null order by time_updated desc limit 300")
        sessions = []
        for row in rows:
            item = dict(row)
            model = item.get('model')
            if isinstance(model, str):
                try:
                    model = json.loads(model)
                except ValueError:
                    model = {}
            if not isinstance(model, dict):
                model = {}
            updated = epoch(item.get('time_updated')) or epoch(item.get('time_created')) or 0
            title = item.get('title')
            title = title.strip() if isinstance(title, str) and title.strip() else 'Untitled OpenCode task'
            sessions.append({
                'id': item['id'],
                'title': title[:160],
                'cwd': item.get('directory') or '',
                'project_name': projects.get(item.get('project_id')),
                'model': model.get('id'),
                'provider': model.get('providerID'),
                'agent': item.get('agent'),
                'updated_at': updated,
            })
        return sessions


def latest_messages(session_id, limit=10):
    db = OPENCODE_HOME / 'opencode.db'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "select data,time_updated from message where session_id=? order by time_updated desc limit ?",
            (session_id, limit))
        messages = []
        newest = 0
        for row in rows:
            newest = max(newest, row['time_updated'] or 0)
            try:
                data = json.loads(row['data'])
            except ValueError:
                continue
            if isinstance(data, dict):
                messages.append(data)
        return messages, epoch(newest)


def inspect_session(session_id):
    """Derive Idle/Waiting/Needs Input/Completed from recorded messages only."""
    try:
        messages, newest = latest_messages(session_id)
    except (sqlite3.Error, OSError) as e:
        return 'Needs Input', None, 'Session history unavailable: ' + str(e)
    if not messages:
        return 'Idle', None, ''
    latest = messages[0]
    role = latest.get('role')
    error = latest.get('error')
    if isinstance(error, dict) and error:
        data = error.get('data') if isinstance(error.get('data'), dict) else {}
        text = str(error.get('name') or '') + ' ' + str(data.get('message') or error.get('message') or '')
        status_code = data.get('statusCode')
        if status_code == 429 or rate_error_text(text):
            reset = reset_from_text(text)
            if reset:
                return 'Waiting', reset, text[:1000]
            return 'Needs Input', None, text[:1000] + ' Reset time unavailable; set it manually.'
        if 'abort' in text.lower():
            return 'Needs Input', None, 'Session was interrupted. Review it before resuming.'
        return 'Needs Input', None, text[:1000]
    if role == 'user':
        return 'Needs Input', None, 'Latest message has no recorded reply. Review the session before resuming.'
    return 'Completed', None, 'Latest OpenCode turn finished; this does not certify the whole goal is complete.'


def worker_alive(task):
    run = task.get('run')
    if not run:
        return False
    with (ROOT / (task['id'] + '.opencode.run.lock')).open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return time.time() - run['started'] < 10  # process startup grace


def refresh(state):
    try:
        rows = discovery()
    except (sqlite3.Error, OSError) as e:
        raise RuntimeError('Cannot read local OpenCode sessions: ' + str(e))
    known = {r['id'] for r in rows}
    for row in rows:
        task = state['tasks'].get(row['id'])
        if task is None:
            task = dict(row, state='Idle', armed=False, reset=None, note='', fingerprint=None)
            state['tasks'][row['id']] = task
        task.update(row)
        task['available'] = True
    for task in state['tasks'].values():
        if task['id'] not in known:
            task['available'] = False
        if task.get('run'):
            if worker_alive(task):
                continue
            task.update(state='Needs Input', reset=None, run=None,
                        note='Watcher run was interrupted. Review the task before resuming.')
            event(state, 'OpenCode task needs attention', task['title'] + ': interrupted run')
        if not task.get('available'):
            continue
        if task.get('manual') and task.get('state') not in ('Running', 'Completed'):
            continue
        status, reset, note = inspect_session(task['id'])
        if task.get('manual') and status not in ('Running', 'Completed'):
            continue
        _, newest = (None, None)
        try:
            _, newest = latest_messages(task['id'], limit=1)
        except (sqlite3.Error, OSError):
            pass
        fingerprint = [task.get('updated_at'), newest]
        if fingerprint != task.get('fingerprint'):
            task['fingerprint'] = fingerprint
            task.update(state=status, reset=reset, note=note, manual=False)


def launch(state, task):
    if task.get('run') or task['state'] == 'Running':
        raise ValueError('Task is already running. Finish or stop it in OpenCode first.')
    if not task.get('available'):
        raise ValueError('Task is archived or not available in the local registry.')
    if any(t['id'] != task['id'] and t['state'] == 'Running'
           and t.get('cwd') and task.get('cwd')
           and os.path.realpath(t['cwd']) == os.path.realpath(task['cwd'])
           for t in state['tasks'].values()):
        raise ValueError('Another OpenCode task is running in this working directory.')
    if not task.get('cwd') or not pathlib.Path(task['cwd']).is_dir():
        raise ValueError('Working directory no longer exists.')
    if not cli_executable(state['cli']):
        raise ValueError('OpenCode executable was not found. Update its path in Settings.')
    if any(t.get('run') for t in state['tasks'].values()):
        raise ValueError('Another watcher continuation is running.')
    token = str(uuid.uuid4())
    task.update(state='Running', reset=None, manual=False,
                run={'token': token, 'started': time.time()}, note='Starting OpenCode continuation…')
    try:
        with (ROOT / WORKER_ERROR_LOG).open('ab') as log:
            subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()),
                              'worker', task['id'], token],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except Exception:
        task.update(state='Needs Input', run=None, note='Could not launch the worker.')
        raise


def worker(task_id, token):
    prepare()
    with (ROOT / (task_id + '.opencode.run.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token:
                return
            cli, prompt, cwd = state['cli'], state['prompt'], task['cwd']
            event(state, 'OpenCode task resumed', task['title'])
        path = ROOT / (task_id + '.opencode.log')
        status, reset, note = 'Needs Input', None, 'OpenCode did not finish successfully.'
        try:
            # Direct argv: session IDs and prompts never pass through a shell.
            # `opencode run -s <id> <prompt>` continues the session headless.
            args = [cli, 'run', '-s', task_id, prompt]
            with path.open('wb') as output:
                p = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT)
                p.communicate(timeout=6 * 3600)
            try:
                logged = tail(path)
            except OSError:
                logged = ''
            if p.returncode != 0:
                if rate_error_text(logged):
                    reset = reset_from_text(logged)
                    if reset and reset > time.time():
                        status, note = 'Waiting', logged[-1000:]
                    else:
                        status, reset, note = 'Needs Input', None, (logged[-1000:] or 'OpenCode exited with code %s. Open the run log.' % p.returncode)
                else:
                    status, reset, note = 'Needs Input', None, (logged[-1000:] or 'OpenCode exited with code %s. Open the run log.' % p.returncode)
            else:
                status, reset, note = inspect_session(task_id)
                if status in ('Idle', 'Running'):
                    status, reset, note = 'Needs Input', None, 'No terminal OpenCode event was received. Open the run log.'
        except Exception as e:
            note = str(e)
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token:
                return
            if reset and reset <= time.time():
                status, reset, note = 'Needs Input', None, 'OpenCode reported a reset time that has already passed. Set a new time or Resume Now.'
            task.update(state=status, reset=reset, run=None, note=note)
            event(state, 'OpenCode task ' + ('finished' if status == 'Completed' else 'needs attention'),
                  task['title'] + ': ' + note[:200])


def command(request):
    with transaction() as state:
        op = request.get('op', 'snapshot')
        error = None
        try:
            refresh(state)
        except (sqlite3.Error, OSError, RuntimeError) as e:
            error = str(e)
        if op in ('arm', 'schedule', 'resume', 'idle'):
            task = state['tasks'][request['id']]
            if op == 'arm':
                task['armed'] = bool(request['armed'])
            elif op == 'schedule':
                if task['state'] == 'Running':
                    raise ValueError('Task is running. Stop it in OpenCode before scheduling.')
                reset = float(request['reset'])
                if reset <= time.time():
                    raise ValueError('Choose a future reset time.')
                task.update(state='Waiting', armed=True, reset=reset, manual=True, note='Reset time entered manually.')
            elif op == 'idle':
                if task.get('run'):
                    raise ValueError('The watcher still has an active worker.')
                task.update(state='Idle', reset=None, manual=False,
                            note='Marked idle by you. Ensure OpenCode is no longer running this task.')
            else:
                if error:
                    raise ValueError(error)
                launch(state, task)
        elif op == 'settings':
            prompt = request['prompt'].strip()
            if not prompt:
                raise ValueError('Continuation prompt cannot be empty.')
            cli = os.path.expanduser(request['cli'])
            if not cli_executable(cli):
                raise ValueError('Choose an executable OpenCode CLI path.')
            state.update(prompt=prompt, cli=cli)
        if op == 'tick' and not error and not any(t.get('run') for t in state['tasks'].values()):
            due = [t for t in state['tasks'].values()
                   if t['armed'] and t['state'] == 'Waiting' and t.get('reset') and t['reset'] + 15 <= time.time()]
            if due:
                task = min(due, key=lambda t: t['reset'])
                try:
                    launch(state, task)
                except ValueError as e:
                    task.update(state='Needs Input', reset=None, note=str(e))
                    event(state, 'OpenCode resume failed', task['title'] + ': ' + str(e))
        return dict(state, tasks=sorted(state['tasks'].values(), key=lambda t: (not t['armed'], -(t.get('updated_at') or 0))), error=error)


if __name__ == '__main__':
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == 'worker':
        worker(sys.argv[2], sys.argv[3])
    else:
        try:
            print(json.dumps(command(json.load(sys.stdin))))
        except Exception as e:
            print(json.dumps({'error': str(e)}))
            sys.exit(1)
