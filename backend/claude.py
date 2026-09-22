#!/usr/bin/env python3
"""Local Claude Code session monitor. No third-party packages, credential reads or shell commands.

Reads only ~/.claude/projects/*/[session-uuid].jsonl transcript files (bounded
tails) over plain file reads. Never touches settings.json, auth material or any
credential store. Usage limits are detected from recorded error text; explicit
reset timestamps are honored when present, otherwise manual scheduling is the
dependable fallback.
"""
import contextlib, datetime, fcntl, json, os, pathlib, re, shutil, subprocess, sys, time, uuid

HOME = pathlib.Path.home()
ROOT = pathlib.Path(os.environ.get('AJO_NIGHT_WATCHER_DATA_HOME', HOME / 'Library/Application Support/Ajo Night Watcher'))
CLAUDE_HOME = pathlib.Path(os.environ.get('CLAUDE_HOME', HOME / '.claude'))
DEFAULT_PROMPT = 'Continue from where you stopped. Review the existing changes first and continue the original task.'
DEFAULT_CLI = 'claude'

REGISTRY = 'claude_registry.json'
STATE_LOCK = 'claude_state.lock'
WORKER_ERROR_LOG = 'claude-worker-errors.log'


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


def tail(path, limit=2 * 1024 * 1024):
    with open(path, 'rb') as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - limit))
        if size > limit:
            f.readline()
        return f.read().decode('utf-8', errors='replace')


def head(path, limit=64 * 1024):
    with open(path, 'rb') as f:
        return f.read(limit).decode('utf-8', errors='replace')


def rate_error_text(text):
    lowered = text.lower()
    return ('429' in lowered or 'rate_limit' in lowered or 'rate limit' in lowered
            or 'usage limit' in lowered or 'usage_limit' in lowered or 'overloaded' in lowered
            or 'credit balance' in lowered or 'quota' in lowered or 'too many requests' in lowered)


def reset_from_text(message):
    """Only accept explicit reset timestamps; never invent a quota window."""
    m = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})', message)
    if m:
        value = epoch(m.group())
        if value:
            return value
    m = re.search(r'(?i)(?:resets?\s+(?:at|in)|retry[^\d]{0,20})(\d+)\s*(?:seconds?|secs?|s\b|minutes?|mins?|m\b|hours?|h\b)?', message)
    if m:
        try:
            amount = int(m.group(1))
        except ValueError:
            amount = 0
        unit = (m.group(2) or 's').lower() if len(m.groups()) > 1 and m.group(2) else 's'
        seconds = amount * (3600 if unit.startswith('h') else 60 if unit.startswith('m') else 1)
        if 0 < seconds <= 7 * 24 * 3600:
            return time.time() + seconds
    return None


def cli_executable(cli):
    if '/' in cli:
        return os.access(cli, os.X_OK)
    return shutil.which(cli) is not None


def session_meta(path):
    """cwd + title from bounded head read; session id is the file stem."""
    try:
        preview = head(path)
    except OSError as e:
        raise RuntimeError('Session history unavailable: ' + str(e))
    cwd, ai_title, first_text = '', None, None
    for line in preview.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if not cwd and isinstance(e.get('cwd'), str):
            cwd = e['cwd']
        if e.get('type') == 'ai-title' and isinstance(e.get('aiTitle'), str) and e['aiTitle'].strip():
            ai_title = e['aiTitle'].strip()
        if first_text is None and e.get('type') == 'user':
            content = (e.get('message') or {}).get('content') if isinstance(e.get('message'), dict) else None
            if isinstance(content, str) and content.strip():
                snippet = content.strip().splitlines()[0][:160]
                if not snippet.startswith('<command-message>') and not snippet.startswith('You have access to'):
                    first_text = snippet
        if cwd and (ai_title or first_text):
            break
    title = ai_title or first_text
    if not title:
        title = (pathlib.Path(cwd).name + ' session') if cwd else 'Untitled Claude Code task'
    try:
        st = os.stat(path)
        updated = st.st_mtime
        fingerprint = [st.st_mtime_ns, st.st_size]
    except OSError as e:
        raise RuntimeError('Session history unavailable: ' + str(e))
    return {'id': path.stem, 'title': title[:160], 'cwd': cwd, 'updated_at': updated,
            'fingerprint': fingerprint, 'path': str(path)}


def discovery(limit=300):
    projects = CLAUDE_HOME / 'projects'
    try:
        files = sorted(projects.glob('*/*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError as e:
        raise RuntimeError('Cannot read local Claude Code sessions: ' + str(e))
    sessions = []
    for path in files:
        if path.name.startswith('.'):
            continue
        try:
            sessions.append(session_meta(path))
        except RuntimeError:
            continue
    return sessions


def inspect_session(path):
    """Derive Idle/Waiting/Needs Input/Completed from the bounded tail only."""
    try:
        text = tail(path)
    except OSError as e:
        return 'Needs Input', None, 'Session history unavailable: ' + str(e)
    last_type = None
    messages = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        t = e.get('type')
        if t in ('user', 'assistant'):
            last_type = t
        messages.append(json.dumps(e))
    blob = '\n'.join(messages[-40:])
    if rate_error_text(blob):
        reset = reset_from_text(blob)
        excerpt = blob[-1000:]
        if reset:
            return 'Waiting', reset, excerpt
        return 'Needs Input', None, excerpt + ' Reset time unavailable; set it manually.'
    if last_type == 'user':
        return 'Needs Input', None, 'Latest message has no recorded reply. Review the session before resuming.'
    if last_type == 'assistant':
        return 'Completed', None, 'Latest Claude Code turn finished; this does not certify the whole goal is complete.'
    return 'Idle', None, ''


def worker_alive(task):
    run = task.get('run')
    if not run:
        return False
    with (ROOT / (task['id'] + '.claude.run.lock')).open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return time.time() - run['started'] < 10  # process startup grace


def refresh(state):
    rows = discovery()
    known = {r['id'] for r in rows}
    for row in rows:
        task = state['tasks'].get(row['id'])
        if task is None:
            task = dict(row, state='Idle', armed=False, reset=None, note='', fingerprint=None)
            state['tasks'][row['id']] = task
        changed = task.get('fingerprint') != row.get('fingerprint')
        task.update(row)
        task['available'] = True
        task['_changed'] = changed
    for task in state['tasks'].values():
        if task['id'] not in known:
            task['available'] = False
        if task.get('run'):
            if worker_alive(task):
                continue
            task.update(state='Needs Input', reset=None, run=None,
                        note='Watcher run was interrupted. Review the task before resuming.')
            event(state, 'Claude Code task needs attention', task['title'] + ': interrupted run')
        if not task.get('available'):
            continue
        if task.get('manual') and task.get('state') not in ('Running', 'Completed'):
            continue
        status, reset, note = inspect_session(task['path'])
        if task.get('manual') and status not in ('Running', 'Completed'):
            continue
        if task.pop('_changed', True):
            task.update(state=status, reset=reset, note=note, manual=False)
    for task in state['tasks'].values():
        task.pop('_changed', None)


def launch(state, task):
    if task.get('run') or task['state'] == 'Running':
        raise ValueError('Task is already running. Finish or stop it in Claude Code first.')
    if not task.get('available'):
        raise ValueError('Task is archived or not available in the local registry.')
    if any(t['id'] != task['id'] and t['state'] == 'Running'
           and t.get('cwd') and task.get('cwd')
           and os.path.realpath(t['cwd']) == os.path.realpath(task['cwd'])
           for t in state['tasks'].values()):
        raise ValueError('Another Claude Code task is running in this working directory.')
    if not task.get('cwd') or not pathlib.Path(task['cwd']).is_dir():
        raise ValueError('Working directory no longer exists.')
    if not cli_executable(state['cli']):
        raise ValueError('Claude Code executable was not found. Update its path in Settings.')
    if any(t.get('run') for t in state['tasks'].values()):
        raise ValueError('Another watcher continuation is running.')
    token = str(uuid.uuid4())
    task.update(state='Running', reset=None, manual=False,
                run={'token': token, 'started': time.time()}, note='Starting Claude Code continuation…')
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
    with (ROOT / (task_id + '.claude.run.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token:
                return
            cli, prompt, cwd = state['cli'], state['prompt'], task['cwd']
            event(state, 'Claude Code task resumed', task['title'])
        path = ROOT / (task_id + '.claude.log')
        status, reset, note = 'Needs Input', None, 'Claude Code did not finish successfully.'
        try:
            # Direct argv: session IDs and prompts never pass through a shell.
            # `claude -p --resume <id> <prompt>` continues the session headless.
            args = [cli, '-p', '--resume', task_id, prompt]
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
                        status, reset, note = 'Needs Input', None, (logged[-1000:] or 'Claude Code exited with code %s. Open the run log.' % p.returncode)
                else:
                    status, reset, note = 'Needs Input', None, (logged[-1000:] or 'Claude Code exited with code %s. Open the run log.' % p.returncode)
            else:
                status, reset, note = inspect_session(task['path'])
                if status in ('Idle', 'Running'):
                    status, reset, note = 'Needs Input', None, 'No terminal Claude Code event was received. Open the run log.'
        except Exception as e:
            note = str(e)
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token:
                return
            if reset and reset <= time.time():
                status, reset, note = 'Needs Input', None, 'Claude Code reported a reset time that has already passed. Set a new time or Resume Now.'
            task.update(state=status, reset=reset, run=None, note=note)
            event(state, 'Claude Code task ' + ('finished' if status == 'Completed' else 'needs attention'),
                  task['title'] + ': ' + note[:200])


def command(request):
    with transaction() as state:
        op = request.get('op', 'snapshot')
        error = None
        try:
            refresh(state)
        except (OSError, RuntimeError) as e:
            error = str(e)
        if op in ('arm', 'schedule', 'resume', 'idle'):
            task = state['tasks'][request['id']]
            if op == 'arm':
                task['armed'] = bool(request['armed'])
            elif op == 'schedule':
                if task['state'] == 'Running':
                    raise ValueError('Task is running. Stop it in Claude Code before scheduling.')
                reset = float(request['reset'])
                if reset <= time.time():
                    raise ValueError('Choose a future reset time.')
                task.update(state='Waiting', armed=True, reset=reset, manual=True, note='Reset time entered manually.')
            elif op == 'idle':
                if task.get('run'):
                    raise ValueError('The watcher still has an active worker.')
                task.update(state='Idle', reset=None, manual=False,
                            note='Marked idle by you. Ensure Claude Code is no longer running this task.')
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
                raise ValueError('Choose an executable Claude Code CLI path.')
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
                    event(state, 'Claude Code resume failed', task['title'] + ': ' + str(e))
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
