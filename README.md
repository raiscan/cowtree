# cowtree

Copy-on-write Git worktrees for Claude Code and Codex.

`cowtree` creates a new worktree with Git's `--no-checkout` setup, reflinks
unchanged tracked files from a clean existing worktree, refreshes the index,
and lets Git materialize everything that cannot be safely shared. Unsupported
filesystems, cross-device paths, dirty sources, and partial failures fall back
to a normal checkout.

The implementation is a portable Python helper with no third-party runtime
dependencies. Linux uses `cp --reflink=always`; macOS uses APFS clone support.

## Use

Run the skill helper from a repository:

```sh
python3 scripts/cow_worktree.py add --verbose \
  --from /path/to/clean-source /path/to/new-worktree <commit-ish>
```

The skill instructions are in [`SKILL.md`](SKILL.md). Install the repository
as a user-scoped skill by cloning it into `~/.agents/skills/cow-git-worktree`,
then link that directory into the Claude and Codex skill paths if needed.

## Test

```sh
bash tests/test_cow_worktree.sh
```
