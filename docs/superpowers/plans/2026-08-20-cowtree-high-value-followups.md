# Cowtree High-Value Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring cowtree's optimized worktree creation closer to native `git worktree add` semantics while making reflink fallback safe and efficient.

**Architecture:** Keep the existing Python helper and Git-backed final verification. Parse and forward the native `worktree add` creation flags, while retaining cowtree-only `--from` and verbose reporting. Validate the invocation repository and source worktree before creating the target, use the target tree's modes for copied files, invoke `post-checkout` after successful materialization, and cancel/clean up failed copy attempts before falling back to Git.

**Tech Stack:** Python 3 standard library, Git CLI, Bash integration tests.

**Spec:** User request: implement the high-value follow-ups identified by Sol's review of the cowtree implementation.

## Global Constraints

- Preserve independent file writes; never use hard links.
- Keep Python runtime dependencies at zero.
- Preserve normal Git checkout as the correctness fallback.
- Do not copy dirty, untracked, ignored, symlink, submodule, filtered, changed, or mode-mismatched source files.
- Do not switch the main workspace branch; integrate the feature branch into the existing `main` checkout.

---

### Task 1: Lock down native behavior and safety regressions

**Files:**
- Modify: `tests/test_cow_worktree.sh`
- Create: `docs/superpowers/plans/2026-08-20-cowtree-high-value-followups.md`

**Interfaces:**
- Tests exercise the public `git cowtree add` launcher and the direct helper.
- Later implementation tasks must make each new test pass without weakening existing reflink and fallback assertions.

- [x] **Step 1: Add tests for omitted commit and native creation flags**

  Verify that an omitted commit creates the requested branch with the same branch naming behavior as native Git, and that supported flags such as `--detach`, `--lock --reason`, `-B`, `--force`, and `--orphan` are either forwarded or rejected with the same documented behavior.

- [x] **Step 2: Add a post-checkout hook test**

  Install an executable hook in the test repository that records its three arguments and creates a marker in the new worktree. Assert that cowtree invokes it once after a successful optimized checkout and that `--no-checkout` does not invoke it.

- [x] **Step 3: Add tests for source validation and physical modes**

  Assert that a `--from` path in a different repository falls back to the invocation repository instead of operating on the unrelated repository, and that a tracked executable target receives mode `100755` even when the source worktree's `core.fileMode` hides its physical mode difference.

- [x] **Step 4: Add tests for unsupported reflinks and partial copy cleanup**

  Use a controlled test hook or environment-driven test seam to make the first copy report an unsupported backend. Assert that cowtree stops attempting subsequent files, removes partial targets, and completes with a clean normal checkout.

- [x] **Step 5: Run the focused suite and confirm the new tests fail for the intended behavior**

  Run `bash tests/test_cow_worktree.sh` from this worktree. Existing tests may pass, but each newly added assertion must expose the current implementation gap before production code is changed.

### Task 2: Implement native argument and repository semantics

**Files:**
- Modify: `scripts/cow_worktree.py`
- Modify: `tests/test_cow_worktree.sh`

**Interfaces:**
- `build_git_add_args(args, target, commit)` returns the native creation arguments without inventing `HEAD` when the commit was omitted.
- `repository_root()` and `common_git_dir()` remain the authority for repository identity.

- [x] **Step 1: Preserve omitted commit and forward creation flags**

  Keep the positional commit as `None` when omitted, pass no commit argument to native Git, and add parser support for the native branch/lock/force/orphan/no-checkout options needed by the tests. Keep `--from` and verbose mode private to cowtree.

- [x] **Step 2: Validate source and invocation repositories**

  Resolve the repository from the current invocation directory. When `--from` is supplied, require its worktree to belong to that same Git common directory; otherwise report the mismatch and use ordinary Git behavior from the invocation repository.

- [x] **Step 3: Resolve target revision after native creation**

  For branch/default modes, let Git perform branch selection first and then resolve `HEAD` in the no-checkout target. Use the resolved revision for tree comparison and final verification.

- [x] **Step 4: Run the focused CLI and source-validation tests**

  Run `bash tests/test_cow_worktree.sh` and confirm branch, flag, and repository-identity behavior against native Git.

### Task 3: Implement robust copy, mode, and hook behavior

**Files:**
- Modify: `scripts/cow_worktree.py`
- Modify: `tests/test_cow_worktree.sh`

**Interfaces:**
- `copy_reflink()` continues to return the backend name and removes any target it created when an attempt fails.
- `finish_checkout()` verifies the target and returns only after the target index and files match Git.

- [x] **Step 1: Derive destination permissions from the target tree**

  Create copied files with `stat.S_IMODE(int(target_tree[path][0], 8))`, then preserve timestamps from the source without allowing a source worktree's physical mode to override Git's tracked mode.

- [x] **Step 2: Add a shared unsupported-backend probe and cancellation**

  Probe the selected reflink backend before submitting the full file set, mark unsupported backends once, stop scheduling new copies after the first unsupported error, and remove failed or partially-created targets before normal checkout.

- [x] **Step 3: Invoke `post-checkout` with native arguments**

  After final verification, invoke the repository's hook with `oldrev`, `newrev`, and `1` for a branch checkout, respecting `--no-checkout`. Propagate hook failures through the normal Git error path while leaving the registered target available for inspection, matching native Git behavior.

- [x] **Step 4: Run focused regression tests and the complete shell suite**

  Run the individual new behavior tests while iterating, then run `bash tests/test_cow_worktree.sh` and `python3 -m py_compile scripts/cow_worktree.py`.

### Task 4: Document the compatibility boundary and verify delivery

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `tests/test_cow_worktree.sh`

**Interfaces:**
- Documentation describes `git cowtree add` as a native-compatible creation command with the additional `--from` and verbose options.
- The test suite is the release gate for the helper and launcher.

- [x] **Step 1: Document native flags, hooks, and fallback behavior**

  Explain that omitted commits retain Git's branch/default semantics, hooks run like native worktree creation, and unsupported filesystems or copy failures fall back to a normal checkout.

- [x] **Step 2: Add portability and differential checks that do not require reflinks**

  Ensure the suite explicitly covers the normal-checkout path and uses Git's observed branch/status output rather than assuming a reflink-capable filesystem.

- [x] **Step 3: Run all verification commands**

  Run `bash tests/test_cow_worktree.sh`, `python3 -m py_compile scripts/cow_worktree.py`, `git diff --check`, the skill validator, and the installed user-scoped skill test suite.

- [x] **Step 4: Commit the implementation branch**

  Review `git diff` and `git status`, stage only the plan, helper, tests, and documentation, and commit with `feat: improve cowtree compatibility and fallback safety`.
