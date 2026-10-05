# Claude native installer checks Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify the existing 20 Windows/Linux profiles on their native operating systems and publish any required installer fixes with clear Russian instructions.

**Architecture:** GitHub Actions downloads official Squirrel NUPKG / DEB packages and extracts only the patchable files into disposable directories. The test invokes the public launchers and portable core, checks translation bytes and Windows ASAR integrity, refuses unexpected changes, and restores every original byte. Existing interactive consent remains the default; any unattended Windows installation requires an explicit opt-in.

**Tech Stack:** Python standard library, PowerShell 5.1/7, Bash, dpkg-deb, GitHub-hosted x64/ARM64 runners.

## Chunk 1: Native lifecycle and publication

- [ ] Create `tests/fixtures.json` from previously recorded official package identities for all 20 profiles.
- [ ] Create `tests/portable_integration.py`: native OS/architecture guard, verified download, safe selected-file extraction, status/install/repeat install, restore/reinstall with changed dictionary, tamper and corrupt-backup refusal, byte-identical restore, standalone bootstrap, Windows consent refusal and PowerShell 7.
- [ ] On Linux also patch and restore root-owned resources through the public launcher's sudo handoff; verify original uid/gid/mode and user-owned private state.
- [ ] Create `.github/workflows/portable-integration.yml`: manual dispatch, smoke/full modes, four standard runner types, max parallel four, read-only permissions, text reports only.
- [ ] Review fixture coverage and harness; run AST, Bash syntax, workflow parsing and `git diff --check` before publishing the test commit.
- [ ] Run four smoke jobs against current production code. Capture concrete failures before changing the patcher.
- [ ] Fix only reproduced native installer failures in `portable/patch.py` / `windows/install.ps1`; preserve all app originals and existing rollback format.
- [ ] Run the full 20-profile matrix and inspect every job/report. Do not execute Claude or log into an account.
- [ ] Update `manifest.json`, `README.md` and `docs/native-installer-checks.md` with the exact verified commit/run, scope, limitations and installation commands; publish the final package. Keep application execution verification separate from installer lifecycle verification.

**Validation:** `python tests/portable_integration.py --profile PROFILE` must succeed only on the matching native runner. Full workflow must return exactly 20 successful profile reports, with application launch and account login explicitly marked untested.
