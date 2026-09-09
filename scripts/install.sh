#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/build.sh
mkdir -p "$HOME/Applications"
APP="$HOME/Applications/Ajo Night Watcher.app"
if pgrep -x AjoNightWatcher >/dev/null; then
  echo 'Quit Ajo Night Watcher before installing an update.' >&2
  exit 1
fi
ditto "dist/Ajo Night Watcher.app" "$APP"
if [[ "${1:-}" == '--login' ]]; then
  /usr/bin/python3 - <<'PY'
import pathlib,plistlib,subprocess,os,time,fcntl
home=pathlib.Path.home()
p=home/'Library/LaunchAgents/com.ajo.night-watcher.plist'
p.parent.mkdir(parents=True,exist_ok=True)
agent={'Label':'com.ajo.night-watcher','ProgramArguments':[str(home/'Applications/Ajo Night Watcher.app/Contents/MacOS/AjoNightWatcher'),'--background'],'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},'ThrottleInterval':30,'ProcessType':'Interactive','LimitLoadToSessionType':'Aqua'}
p.write_bytes(plistlib.dumps(agent))
subprocess.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}',str(p)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
subprocess.run(['/bin/launchctl','bootstrap',f'gui/{os.getuid()}',str(p)],check=True)
# Wait for the managed app to claim its instance lock before LaunchServices opens it.
lockpath=home/'Library/Application Support/Ajo Night Watcher/app.lock'
for _ in range(100):
    if lockpath.exists():
        with lockpath.open('a') as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: break
    time.sleep(0.1)
else: raise RuntimeError('The login app did not start within 10 seconds.')
PY
fi
open "$APP"
