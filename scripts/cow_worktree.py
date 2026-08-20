#!/usr/bin/env python3
"""Create Git worktrees while sharing unchanged files with reflinks."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass


class CowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str
    main: bool


def run_git(cwd: Path, args: list[str], *, check: bool = True, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise CowError(f"git {' '.join(args)} failed: {detail}")
    return result


def git_text(cwd: Path, args: list[str]) -> str:
    return run_git(cwd, args).stdout.decode().strip()


def repository_root(cwd: Path) -> Path:
    return Path(git_text(cwd, ["rev-parse", "--show-toplevel"])).resolve()


def common_git_dir(cwd: Path) -> Path:
    value = git_text(cwd, ["rev-parse", "--git-common-dir"])
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def list_worktrees(repo: Path) -> list[Worktree]:
    result = run_git(repo, ["worktree", "list", "--porcelain"])
    entries: list[Worktree] = []
    path: Path | None = None
    head = ""
    bare = False
    for line in result.stdout.decode(errors="surrogateescape").splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line[9:]).resolve()
        elif line.startswith("HEAD "):
            head = line[5:]
        elif line == "bare":
            bare = True
        elif line == "" and path is not None:
            if not bare and head and path.is_dir():
                entries.append(Worktree(path, head, not entries))
            path = None
            head = ""
            bare = False
    return entries


def is_clean(worktree: Worktree, common_dir: Path) -> bool:
    try:
        if common_git_dir(worktree.path) != common_dir:
            return False
        result = run_git(
            worktree.path,
            [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            check=False,
        )
        return result.returncode == 0 and not result.stdout
    except (CowError, OSError):
        return False


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def choose_source(repo: Path, requested: str | None, target: Path, common_dir: Path, target_rev: str) -> Worktree | None:
    worktrees = list_worktrees(repo)
    if requested:
        source = Path(requested).expanduser()
        if not source.is_absolute():
            source = (Path.cwd() / source).resolve()
        else:
            source = source.resolve()
        matches = [worktree for worktree in worktrees if worktree.path == source]
        if not matches or path_is_within(target, source):
            return None
        return matches[0] if is_clean(matches[0], common_dir) else None

    cwd_root: Path | None = None
    try:
        cwd_root = repository_root(Path.cwd())
    except (CowError, OSError):
        pass

    candidates = [worktree for worktree in worktrees if not path_is_within(target, worktree.path)]
    clean = [worktree for worktree in candidates if is_clean(worktree, common_dir)]
    if not clean:
        return None

    def preference(worktree: Worktree) -> tuple[int, int, str]:
        if cwd_root == worktree.path:
            priority = 0
        elif worktree.main:
            priority = 1
        else:
            priority = 2
        try:
            parts = git_text(repo, ["rev-list", "--left-right", "--count", f"{worktree.head}...{target_rev}"]).split()
            distance = sum(int(part) for part in parts)
        except (CowError, ValueError):
            distance = 1 << 60
        return priority, distance, str(worktree.path)

    return min(clean, key=preference)


def tree_entries(repo: Path, revision: str) -> dict[str, tuple[str, str, str]]:
    raw = run_git(repo, ["ls-tree", "-r", "-z", revision]).stdout
    entries: dict[str, tuple[str, str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        path = os.fsdecode(path_bytes)
        entries[path] = (mode.decode(), kind.decode(), object_id.decode())
    return entries


def blob_hash(repo: Path, path: Path) -> str:
    return git_text(repo, ["hash-object", "--no-filters", "--", str(path)])


def reflink_command(source: Path, target: Path) -> list[str] | None:
    system = platform.system()
    if system == "Linux" and shutil.which("cp"):
        return ["cp", "--reflink=always", "--preserve=mode,timestamps", str(source), str(target)]
    if system == "Darwin" and shutil.which("cp"):
        return ["cp", "-c", "-p", str(source), str(target)]
    return None


def copy_reflink(source: Path, target: Path) -> None:
    command = reflink_command(source, target)
    if command is None:
        raise CowError(f"copy-on-write is not supported on {platform.system()}")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise CowError(f"reflink failed: {detail}")


def reset_worktree(target: Path) -> None:
    run_git(target, ["reset", "--hard", "HEAD"])


def finish_checkout(target: Path, target_rev: str, copied: set[str]) -> None:
    run_git(target, ["read-tree", target_rev])
    # Refresh stat data for copied files. Missing files make this command return
    # non-zero, but Git still refreshes entries that are already present.
    run_git(target, ["update-index", "--refresh"], check=False)
    target_tree = tree_entries(target, target_rev)
    missing = sorted(set(target_tree) - copied)
    if missing:
        stdin = b"".join(os.fsencode(path) + b"\0" for path in missing)
        run_git(target, ["checkout-index", "--force", "--stdin", "-z"], stdin=stdin)

    status = run_git(target, ["status", "--porcelain=v1", "--untracked-files=all"], check=False)
    if status.returncode or status.stdout:
        reset_worktree(target)
        raise CowError("index/worktree verification failed; used normal checkout")


def build_git_add_args(args: argparse.Namespace, target: Path) -> list[str]:
    command = ["worktree", "add"]
    if args.branch:
        command.extend(["-b", args.branch])
    if args.detach:
        command.append("--detach")
    command.append(str(target))
    command.append(args.commit)
    return command


def normal_add(repo: Path, args: argparse.Namespace, target: Path) -> int:
    result = run_git(repo, build_git_add_args(args, target), check=False)
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    if args.verbose:
        print("fallback=yes reason=copy-on-write-unavailable")
    return result.returncode


def add_worktree(args: argparse.Namespace) -> int:
    repo_location = Path(args.source).expanduser().resolve() if args.source else Path.cwd()
    try:
        repo = repository_root(repo_location)
    except (CowError, OSError) as error:
        print(f"cow-worktree: {error}", file=sys.stderr)
        return 2

    target = Path(args.target).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    else:
        target = target.resolve()
    if target.exists() or target.is_symlink():
        print(f"cow-worktree: target already exists: {target}", file=sys.stderr)
        return 2

    try:
        target_rev = git_text(repo, ["rev-parse", "--verify", f"{args.commit}^{{commit}}"])
        common_dir = common_git_dir(repo)
        source = choose_source(repo, args.source, target, common_dir, target_rev)
    except (CowError, OSError) as error:
        print(f"cow-worktree: {error}; using normal checkout", file=sys.stderr)
        return normal_add(repo, args, target)

    if source is None or not target.parent.exists():
        return normal_add(repo, args, target)
    try:
        if os.stat(source.path).st_dev != os.stat(target.parent).st_dev:
            return normal_add(repo, args, target)
    except OSError:
        return normal_add(repo, args, target)

    add_result = run_git(repo, ["worktree", "add", "--no-checkout", *build_git_add_args(args, target)[2:]], check=False)
    if add_result.returncode:
        if add_result.stdout:
            sys.stdout.buffer.write(add_result.stdout)
        if add_result.stderr:
            sys.stderr.buffer.write(add_result.stderr)
        return add_result.returncode

    try:
        source_tree = tree_entries(repo, source.head)
        target_tree = tree_entries(target, target_rev)
        candidates = [
            path
            for path, (mode, kind, object_id) in target_tree.items()
            if kind == "blob"
            and mode in {"100644", "100755"}
            and source_tree.get(path) == (mode, "blob", object_id)
        ]
        copied: set[str] = set()
        for path in candidates:
            source_path = source.path / path
            target_path = target / path
            source_stat = source_path.lstat()
            if not stat.S_ISREG(source_stat.st_mode):
                continue
            if blob_hash(source.path, source_path) != target_tree[path][2]:
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            copy_reflink(source_path, target_path)
            if blob_hash(target, target_path) != target_tree[path][2]:
                raise CowError(f"copied file did not match Git blob: {path}")
            copied.add(path)

        if not copied:
            raise CowError("no eligible files could be reflinked")
        finish_checkout(target, target_rev, copied)
    except (CowError, OSError, subprocess.SubprocessError) as error:
        reset_worktree(target)
        if args.verbose:
            print(f"source={source.path} target={target} reflinked=0 fallback=yes reason={error}")
        return 0

    if args.verbose:
        print(f"source={source.path} target={target} reflinked={len(copied)} fallback=no")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="git cowtree", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add = subparsers.add_parser("add", help="create a worktree with copy-on-write files")
    add.add_argument("--from", dest="source", help="source worktree to use")
    add.add_argument("-v", "--verbose", action="store_true")
    add.add_argument("-b", "--branch")
    add.add_argument("--detach", action="store_true")
    add.add_argument("target")
    add.add_argument("commit", nargs="?", default="HEAD")
    add.set_defaults(handler=add_worktree)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
