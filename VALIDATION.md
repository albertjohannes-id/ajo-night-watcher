# Validation — 9 September 2026

- Native app compiled and ad-hoc signature verified on arm64 macOS, targeting macOS 13+.
- 36 tests passed: limit/error parsing, exhausted-window selection, missing/expired reset handling, new-turn cancellation, persistence, duplicate dispatch protection, interrupted-run recovery, exact argument/prompt handling, and detached worker success/failure paths.
- OpenCode session monitor added: 10 new tests (rate-error parsing, ISO-reset waiting, manual schedule, due dispatch, executable-CLI guard, credential-touch audit). Full suite 46/46 green. Live read-only discovery returned all 24 local opencode sessions; headless `opencode run -s` resume is implemented but not yet smoke-tested against a disposable session.
- Claude Code monitor added: 10 new tests (usage-limit parsing, reset waiting, title preference for recorded `ai-title`, manual schedule, due dispatch, executable-CLI guard, credential-touch audit). Full suite 56/56 green with app rebuilt and re-signed. Live read-only discovery returned both local Claude transcripts with correct titles and directories; headless `claude -p --resume` is implemented but not yet smoke-tested against a disposable session.
- Live CLI create/resume used a disposable session and returned the exact same thread ID. Recall response: `AJO_READY RESUMED`.
- Read-only app-server protocol loaded a Desktop task’s paginated history successfully; no user task was resumed by this probe.
- Real menu-bar timer dispatched a manually scheduled disposable session and received `AJO_WATCHER_OK`. Registry transitioned Waiting → Running → Completed; resume and completion notification events were saved.
- Saved run context confirmed sandbox `workspace-write`, network access false, approval policy `never`.
- UI inspected visually: registry, task details, disabled Resume Now for Running tasks, working-directory paths, IDs, reset picker and settings controls.
- launchd agent loaded and running. Single-instance application lock prevents a second menu-bar process when opening the app separately from launchd.
- Default continuation prompt restored, with all sessions unarmed after validation.

Limitations: no real account exhaustion was induced; automatic rate-limit error parsing is fixture-tested. OS notification delivery depends on the user’s notification settings. Sleep/wake delivery is wired to native wake events but was not tested by forcing this Mac to sleep. Desktop UI live-refresh and concurrent execution from another client are not guaranteed.

## Pending validation and development

- OpenCode: install the CLI (`brew install anomalyco/tap/opencode`), then smoke-test headless resume (`opencode run -s`) against a disposable session before relying on auto-resume. Session monitoring works without the CLI.
- Claude Code: smoke-test headless resume (`claude -p --resume`) against a disposable session; keep the prompt permission-free since `-p` is non-interactive. No real usage-limit error observed yet, so limit detection stays fixture-tested.
- Reinstall the built app (`./scripts/install.sh`) on this Mac to pick up the OpenCode and Claude Code tabs.

## Ajo Night Watcher rename

- App, executable, source folder, bundle ID, launch agent, and Application Support location renamed. Existing local registry retained.
- Original moon-and-clock icon generated from AppKit vector paths; PNG/ICNS bundled with the app.
- Rebuilt and reinstalled; all 36 tests passed after the rename.
