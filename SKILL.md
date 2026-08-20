---
name: cowtree
description: Use when creating a Git worktree and disk usage matters, especially when several worktrees share a large checkout on Linux or macOS.
---

# Copy-on-write Git worktrees

Use the bundled `scripts/cow_worktree.py` helper for new worktrees. It uses
Git's `--no-checkout` setup, reflinks unchanged tracked files, refreshes the
worktree index, and checks out everything that cannot be safely shared.

## Workflow

1. Confirm the repository and target path. The target must not already exist.
2. Prefer a clean existing worktree as the source. Pass it with `--from` when
   the choice matters; otherwise the helper chooses a clean nearby worktree.
3. Put the skill's `bin` directory on `PATH` and use `git cowtree`, or invoke
   the helper directly. Provide `--from` when the source matters:

   ```sh
   git cowtree add --verbose --from /path/to/clean-source \
     /path/to/new-worktree <commit-ish>
   ```

   Add `-b <branch>` or `--detach` when needed. The helper supports those
   common creation modes; use ordinary `git worktree add` for other flags.
4. Verify `git -C /path/to/new-worktree status --short` is empty and inspect
   the helper's report. `fallback=no` means reflinks were used; `fallback=yes`
   means the helper deliberately completed a normal checkout for correctness.

## Safety contract

- Never use hard links (`ln`) for worktree files. Reflinks preserve independent
  writes; hard links do not.
- Only clean, committed source content is eligible. Dirty, untracked, ignored,
  symlink, submodule, filtered-content, changed, or mode-mismatched paths are
  not copied from the source.
- Copy-on-write is attempted only when source and target are on the same
  filesystem and the platform supports native reflinks. Unsupported or
  cross-device cases must fall back to Git's normal checkout.
- This optimization applies to creating a worktree only. Use Git directly for
  `list`, `lock`, `move`, `remove`, and other worktree maintenance commands.

## Quick reference

| Need | Command |
| --- | --- |
| Optimized worktree | `git cowtree add --verbose TARGET COMMIT` |
| Force source selection | Add `--from SOURCE` |
| Ordinary fallback | `git worktree add TARGET COMMIT` |

## Inspiration

This skill was inspired by [`git-cow-worktree`](https://github.com/josharian/git-cow-worktree),
created by [Josh Bleecher Snyder (`@josharian`)](https://github.com/josharian).
The implementation here is independent and does not use code from that project.
