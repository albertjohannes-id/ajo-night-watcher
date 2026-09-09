# Validation — 9 September 2026

- Native app compiled and ad-hoc signature verified on arm64 macOS, targeting macOS 13+.
- 20 tests passed: limit/error parsing, exhausted-window selection, missing/expired reset handling, new-turn cancellation, persistence, duplicate dispatch protection, interrupted-run recovery, exact argument/prompt handling, and detached worker success/failure paths.
- Live CLI create/resume used a disposable session and returned the exact same thread ID. Recall response: `AJO_READY RESUMED`.
- Read-only app-server protocol loaded a Desktop task’s paginated history successfully; no user task was resumed by this probe.
- Real menu-bar timer dispatched a manually scheduled disposable session and received `AJO_WATCHER_OK`. Registry transitioned Waiting → Running → Completed; resume and completion notification events were saved.
- Saved run context confirmed sandbox `workspace-write`, network access false, approval policy `never`.
- UI inspected visually: registry, task details, disabled Resume Now for Running tasks, working-directory paths, IDs, reset picker and settings controls.
- launchd agent loaded and running. Single-instance application lock prevents a second menu-bar process when opening the app separately from launchd.
- Default continuation prompt restored, with all sessions unarmed after validation.

Limitations: no real account exhaustion was induced; automatic rate-limit error parsing is fixture-tested. OS notification delivery depends on the user’s notification settings. Sleep/wake delivery is wired to native wake events but was not tested by forcing this Mac to sleep. Desktop UI live-refresh and concurrent execution from another client are not guaranteed.

## Night Watcher rename

- App, executable, source folder, bundle ID, launch agent, and Application Support location renamed. Existing local registry retained.
- Original moon-and-clock icon generated from AppKit vector paths; PNG/ICNS bundled with the app.
- Rebuilt and reinstalled; all 20 tests passed after the rename.
