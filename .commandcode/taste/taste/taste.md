# Taste
- When starting a new feature, expects a short assessment of what is actually feasible from the available data/support before (and then alongside) the implementation — e.g., "assess and implement it" — rather than code written on assumptions about what exists. Confidence: 0.4
- Gates publishing on a credential audit: before committing/pushing, expects the repo to be checked for secrets (no committed keys, tokens, `.env`/`auth.json`/`credentials*`/`*.pem`/SSH keys) and only commits if it comes back clean. Confidence: 0.65
- Once a change is verified, expects it committed and pushed straight to `main` — not left on a branch or opened as a PR. Confidence: 0.5
- Wants documentation (README, validation/notes docs) updated as part of the change, before committing. Confidence: 0.5
- Uses his personal account (albertjohannes.id@gmail.com) as the commit author identity, applied as repo-local git config rather than touching the global identity. Confidence: 0.5
- Wants new conditions surfaced as their own distinct, clearly-labeled state (e.g., a dedicated "Rate limited" state with its own color and a summary count) rather than folded into existing states. Confidence: 0.5
- Expects task states to be consistent across provider tabs, judging one provider's states by their equivalents in another (e.g., asking whether OpenCode's "Needs Input" maps to Codex's "Completed"). Confidence: 0.4
- Prefers not inventing or guessing unknown values in the UI (e.g., a quota reset window): when the real value isn't available locally, prompt the user to enter it manually instead of assuming a default. Confidence: 0.6
