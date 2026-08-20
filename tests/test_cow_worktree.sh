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

run_helper() {
  local cwd=$1
  shift
  (cd "$cwd" && python3 "$SCRIPT" "$@")
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

  output=$(run_helper "$source" add --from "$source" --verbose "$target" HEAD)
  assert_contains 'reflinked=' "$output"
  assert_contains 'fallback=no' "$output"
  assert_contains 'backend=' "$output"
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

  output=$(run_helper "$source" add --from "$source" --verbose "$target" HEAD~1)
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

  output=$(run_helper "$source" add --from "$source" --verbose "$target" HEAD)
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

  (cd "$source" && PATH="$BIN:$PATH" git cowtree add -b topic --from "$source" "$target" HEAD >/dev/null)
  [[ $(git -C "$target" branch --show-current) == topic ]] || fail 'branch flag was not forwarded'
  assert_clean "$target"
}

test_git_launcher_uses_cowtree_help_name() {
  local output
  output=$(PATH="$BIN:$PATH" git cowtree -h)
  assert_contains 'usage: git cowtree' "$output"
}

test_omitted_commit_preserves_native_branch_default() {
  local root source target
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/feature
  new_repo "$source"
  printf 'branch default\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial

  run_helper "$source" add --from "$source" "$target" >/dev/null
  [[ $(git -C "$target" branch --show-current) == feature ]] || fail 'omitted commit did not create the native default branch'
  assert_clean "$target"
}

test_native_creation_flags_are_forwarded() {
  local root source locked reset orphan no_checkout topic forced
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  locked=$root/locked
  reset=$root/reset
  orphan=$root/orphan
  no_checkout=$root/no-checkout
  topic=$root/topic
  forced=$root/forced
  new_repo "$source"
  printf 'native flags\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial

  run_helper "$source" add --from "$source" --lock --reason 'agent task' "$locked" HEAD >/dev/null
  assert_contains 'locked agent task' "$(git -C "$source" worktree list --porcelain)"

  run_helper "$source" add --from "$source" -B reset-branch "$reset" HEAD >/dev/null
  [[ $(git -C "$reset" branch --show-current) == reset-branch ]] || fail '-B was not forwarded'

  run_helper "$source" add --from "$source" -b reusable "$topic" HEAD >/dev/null
  run_helper "$source" add --from "$source" --force "$forced" reusable >/dev/null
  [[ $(git -C "$forced" branch --show-current) == reusable ]] || fail '--force was not forwarded'

  run_helper "$source" add --from "$source" --orphan -b orphan-branch "$orphan" >/dev/null
  [[ $(git -C "$orphan" branch --show-current) == orphan-branch ]] || fail '--orphan was not forwarded'
  [[ ! -e "$orphan/file.txt" ]] || fail '--orphan unexpectedly checked out a file'

  run_helper "$source" add --from "$source" --no-checkout "$no_checkout" HEAD >/dev/null
  [[ ! -e "$no_checkout/file.txt" ]] || fail '--no-checkout was not forwarded'
}

test_post_checkout_hook_matches_native_arguments() {
  local root source target hook_args
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/hooked
  hook_args=$root/hook-args
  new_repo "$source"
  printf 'hook content\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial
  printf '%s\n' '#!/bin/sh' 'printf "%s\\n" "$@" > "$COWTREE_HOOK_ARGS"' 'printf hook > hook-marker' >"$source/.git/hooks/post-checkout"
  chmod +x "$source/.git/hooks/post-checkout"

  COWTREE_HOOK_ARGS="$hook_args" run_helper "$source" add --from "$source" "$target" >/dev/null
  [[ -e "$target/hook-marker" ]] || fail 'post-checkout hook did not run'
  mapfile -t hook_values <"$hook_args"
  [[ ${#hook_values[@]} == 3 ]] || fail 'post-checkout hook received the wrong number of arguments'
  [[ ${hook_values[0]} == 0000000000000000000000000000000000000000 ]] || fail 'post-checkout old revision was not zero'
  [[ ${hook_values[1]} == $(git -C "$source" rev-parse HEAD) ]] || fail 'post-checkout new revision differs from HEAD'
  [[ ${hook_values[2]} == 1 ]] || fail 'post-checkout branch flag was not 1'
}

test_no_checkout_skips_post_checkout_hook() {
  local root source target marker
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/no-checkout
  marker=$root/hook-marker
  new_repo "$source"
  printf 'no hook\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial
  printf '%s\n' '#!/bin/sh' 'printf hook > "$COWTREE_HOOK_MARKER"' >"$source/.git/hooks/post-checkout"
  chmod +x "$source/.git/hooks/post-checkout"

  COWTREE_HOOK_MARKER="$marker" run_helper "$source" add --from "$source" --no-checkout "$target" HEAD >/dev/null
  [[ ! -e "$marker" ]] || fail 'post-checkout hook ran for --no-checkout'
}

test_post_checkout_hook_failure_is_reported() {
  local root source target output rc
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/hook-failure
  new_repo "$source"
  printf 'hook failure\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial
  printf '%s\n' '#!/bin/sh' 'printf hook-failure >&2' 'exit 7' >"$source/.git/hooks/post-checkout"
  chmod +x "$source/.git/hooks/post-checkout"

  set +e
  output=$(run_helper "$source" add --from "$source" "$target" 2>&1)
  rc=$?
  set -e
  [[ $rc != 0 ]] || fail 'post-checkout hook failure was swallowed'
  assert_contains 'post-checkout hook failed' "$output"
  [[ -d "$target" ]] || fail 'hook failure removed the registered target unexpectedly'
}

test_unrelated_from_repository_is_rejected() {
  local root invocation unrelated target
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  invocation=$root/invocation
  unrelated=$root/unrelated
  target=$root/target
  new_repo "$invocation"
  printf 'invocation repository\n' >"$invocation/file.txt"
  git -C "$invocation" add .
  git -C "$invocation" commit -q -m invocation
  new_repo "$unrelated"
  printf 'unrelated repository\n' >"$unrelated/file.txt"
  git -C "$unrelated" add .
  git -C "$unrelated" commit -q -m unrelated

  run_helper "$invocation" add --from "$unrelated" "$target" HEAD >/dev/null
  [[ $(cat "$target/file.txt") == 'invocation repository' ]] || fail '--from selected an unrelated repository'
  assert_clean "$target"
}

test_target_mode_beats_source_physical_mode() {
  local root source target
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  new_repo "$source"
  printf '#!/bin/sh\nprintf executable\n' >"$source/run.sh"
  chmod +x "$source/run.sh"
  git -C "$source" add .
  git -C "$source" commit -q -m executable
  git -C "$source" config core.fileMode false
  chmod -x "$source/run.sh"
  assert_clean "$source"

  run_helper "$source" add --from "$source" "$target" HEAD >/dev/null
  [[ -x "$target/run.sh" ]] || fail 'target did not receive the tracked executable mode'
  assert_clean "$target"
}

test_exact_matching_worktree_beats_current_worktree() {
  local root source exact target
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  exact=$root/exact
  target=$root/target
  new_repo "$source"
  printf 'old\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m old
  git -C "$source" worktree add -q "$exact" HEAD
  printf 'new\n' >"$source/file.txt"
  git -C "$source" commit -qam new

  local output
  output=$(run_helper "$source" add --verbose "$target" HEAD~1)
  assert_contains "source=$exact" "$output"
  assert_clean "$target"
}

test_copy_failure_stops_before_retrying_every_file() {
  local root source target calls
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  calls=$root/calls
  new_repo "$source"
  printf 'one\n' >"$source/one.txt"
  printf 'two\n' >"$source/two.txt"
  printf 'three\n' >"$source/three.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial

  (
    cd "$source"
    python3 - "$SCRIPT" "$source" "$target" "$calls" <<'PY'
import importlib.util
import sys

script, source, target, calls = sys.argv[1:]
spec = importlib.util.spec_from_file_location("cow_worktree", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

def fail_copy(source_path, target_path, source_stat, target_mode):
    with open(calls, "a", encoding="utf-8") as handle:
        handle.write(str(source_path) + "\n")
    raise module.CowError("simulated unsupported reflink")

module.copy_reflink = fail_copy
sys.argv = [script, "add", "--from", source, "--verbose", target, "HEAD"]
raise SystemExit(module.main())
PY
  ) >/dev/null
  [[ $(wc -l <"$calls") == 1 ]] || fail 'copy failure retried every eligible file'
  assert_clean "$target"
  [[ $(cat "$target/one.txt") == one ]] || fail 'fallback did not materialize the first file'
  [[ $(cat "$target/two.txt") == two ]] || fail 'fallback did not materialize the second file'
  [[ $(cat "$target/three.txt") == three ]] || fail 'fallback did not materialize the third file'
}

test_sha256_and_unusual_paths() {
  local root source target unusual
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  unusual=$'--leading-dash\twith-newline\n.txt'
  if ! git init -q --object-format=sha256 "$source"; then
    return 0
  fi
  git -C "$source" config user.email test@example.invalid
  git -C "$source" config user.name 'Cow Worktree Test'
  printf 'sha256 content\n' >"$source/$unusual"
  git -C "$source" add -- .
  git -C "$source" commit -q -m unusual

  run_helper "$source" add --from "$source" "$target" HEAD >/dev/null
  [[ $(cat "$target/$unusual") == 'sha256 content' ]] || fail 'unusual path content differs'
  assert_clean "$target"
}

test_same_tree_uses_reflinks
test_divergent_tree_checks_out_changed_files
test_dirty_source_falls_back
test_git_dispatches_to_launcher
test_git_launcher_forwards_branch_flag
test_git_launcher_uses_cowtree_help_name

test_source_hash_is_not_recomputed() {
  local root source target trace hash_calls
  root=$(mktemp -d)
  trap 'rm -rf "$root"' RETURN
  source=$root/source
  target=$root/target
  trace=$root/git-trace
  new_repo "$source"
  printf 'one file\n' >"$source/file.txt"
  git -C "$source" add .
  git -C "$source" commit -q -m initial

  GIT_TRACE="$trace" run_helper "$source" add --from "$source" "$target" HEAD >/dev/null
  hash_calls=$(grep -c 'hash-object' "$trace" || true)
  [[ $hash_calls == 1 ]] || fail "expected one target hash-object call, got $hash_calls"
}

test_source_hash_is_not_recomputed
test_omitted_commit_preserves_native_branch_default
test_native_creation_flags_are_forwarded
test_post_checkout_hook_matches_native_arguments
test_no_checkout_skips_post_checkout_hook
test_post_checkout_hook_failure_is_reported
test_unrelated_from_repository_is_rejected
test_target_mode_beats_source_physical_mode
test_exact_matching_worktree_beats_current_worktree
test_copy_failure_stops_before_retrying_every_file
test_sha256_and_unusual_paths
printf 'PASS: cow worktree behavior\n'
