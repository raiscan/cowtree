#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/cow_worktree.py"
BIN="$REPO_ROOT/bin"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local needle=$1
  local haystack=$2
  [[ $haystack == *"$needle"* ]] || fail "expected output to contain: $needle\n$haystack"
}

assert_clean() {
  local repo=$1
  [[ -z "$(git -C "$repo" status --porcelain=v1 --untracked-files=all)" ]] || {
    git -C "$repo" status --short >&2
    fail "worktree is not clean: $repo"
  }
}

new_repo() {
  local repo=$1
  git init -q -b main "$repo"
  git -C "$repo" config user.email test@example.invalid
  git -C "$repo" config user.name 'Cow Worktree Test'
}

test_same_tree_uses_reflinks() {
  local root source target output
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  new_repo "$source"
  mkdir -p "$source/src"
  printf 'shared content\n' >"$source/src/shared.txt"
  printf '#!/bin/sh\nprintf target\\n\n' >"$source/src/executable.sh"
  chmod +x "$source/src/executable.sh"
  git -C "$source" add .
  git -C "$source" commit -q -m initial

  output=$(python3 "$SCRIPT" add --from "$source" --verbose "$target" HEAD)
  assert_contains 'reflinked=' "$output"
  assert_contains 'fallback=no' "$output"
  assert_clean "$target"
  [[ $(cat "$target/src/shared.txt") == 'shared content' ]] || fail 'shared file content differs'
  [[ -x "$target/src/executable.sh" ]] || fail 'executable mode was not preserved'
}

test_divergent_tree_checks_out_changed_files() {
  local root source target output
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  new_repo "$source"
  printf 'unchanged\n' >"$source/unchanged.txt"
  printf 'before\n' >"$source/changed.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial
  printf 'after\n' >"$source/changed.txt"
  git -C "$source" commit -qam changed

  output=$(python3 "$SCRIPT" add --from "$source" --verbose "$target" HEAD~1)
  assert_contains 'reflinked=' "$output"
  assert_clean "$target"
  [[ $(cat "$target/unchanged.txt") == unchanged ]] || fail 'unchanged file content differs'
  [[ $(cat "$target/changed.txt") == before ]] || fail 'changed file was not checked out'
}

test_dirty_source_falls_back() {
  local root source target output
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  new_repo "$source"
  printf 'committed\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial
  printf 'dirty\n' >"$source/file.txt"

  output=$(python3 "$SCRIPT" add --from "$source" --verbose "$target" HEAD)
  assert_contains 'fallback=yes' "$output"
  assert_clean "$target"
  [[ $(cat "$target/file.txt") == committed ]] || fail 'fallback did not use committed content'
}

test_git_dispatches_to_launcher() {
  local root source target output
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  new_repo "$source"
  printf 'launcher content\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial

  output=$(cd "$source" && PATH="$BIN:$PATH" git cowtree add --verbose "$target" HEAD)
  assert_contains 'reflinked=' "$output"
  assert_clean "$target"
  [[ $(cat "$target/file.txt") == 'launcher content' ]] || fail 'launcher content differs'
}

test_git_launcher_forwards_branch_flag() {
  local root source target
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  new_repo "$source"
  printf 'branch content\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial

  PATH="$BIN:$PATH" git cowtree add -b topic --from "$source" "$target" HEAD >/dev/null
  [[ $(git -C "$target" branch --show-current) == topic ]] || fail 'branch flag was not forwarded'
  assert_clean "$target"
}

test_git_launcher_uses_cowtree_help_name() {
  local output
  output=$(PATH="$BIN:$PATH" git cowtree -h)
  assert_contains 'usage: git cowtree' "$output"
}

test_same_tree_uses_reflinks
test_divergent_tree_checks_out_changed_files
test_dirty_source_falls_back
test_git_dispatches_to_launcher
test_git_launcher_forwards_branch_flag
test_git_launcher_uses_cowtree_help_name
printf 'PASS: cow worktree behavior\n'
