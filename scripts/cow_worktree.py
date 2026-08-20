#!/usr/bin/env python3
"""Create Git worktrees while sharing unchanged files with reflinks."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import errno
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl module.
    fcntl = None


class CowError(RuntimeError):
    pass


class PostCheckoutError(CowError):
    pass


FICLONE = getattr(fcntl, "FICLONE", None) if fcntl is not None else None
_ficlone_disabled = False
_ficlone_lock = threading.Lock()


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

    def preference(worktree: Worktree) -> tuple[int, int, int, str]:
        exact = 0 if worktree.head == target_rev else 1
        try:
            parts = git_text(repo, ["rev-list", "--left-right", "--count", f"{worktree.head}...{target_rev}"]).split()
            distance = sum(int(part) for part in parts)
        except (CowError, ValueError):
            distance = 1 << 60
        if cwd_root == worktree.path:
            priority = 0
        elif worktree.main:
            priority = 1
        else:
            priority = 2
        return exact, distance, priority, str(worktree.path)

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


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def copy_with_cp(source: Path, target: Path, target_mode: int) -> None:
    command = reflink_command(source, target)
    if command is None:
        raise CowError(f"copy-on-write is not supported on {platform.system()}")
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            detail = result.stderr.decode(errors="replace").strip()
            raise CowError(f"reflink failed: {detail}")
        os.chmod(target, target_mode)
    except BaseException:
        remove_file(target)
        raise


def copy_with_ficlone(source: Path, target: Path, source_stat: os.stat_result, target_mode: int) -> None:
    if FICLONE is None:
        raise OSError(errno.ENOSYS, "FICLONE is unavailable")
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    target_fd: int | None = None
    try:
        target_fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            target_mode,
        )
        fcntl.ioctl(target_fd, FICLONE, source_fd)
        os.fchmod(target_fd, target_mode)
        os.utime(
            target,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            follow_symlinks=False,
        )
    except BaseException:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_fd)


def native_ficlone_enabled() -> bool:
    return platform.system() == "Linux" and FICLONE is not None and not _ficlone_disabled


def copy_reflink(source: Path, target: Path, source_stat: os.stat_result, target_mode: int) -> str:
    global _ficlone_disabled
    if native_ficlone_enabled():
        try:
            copy_with_ficlone(source, target, source_stat, target_mode)
            return "ficlone"
        except OSError as error:
            unsupported = {
                errno.EINVAL,
                errno.ENOSYS,
                errno.ENOTTY,
                errno.EOPNOTSUPP,
                errno.EXDEV,
            }
            if error.errno not in unsupported:
                raise
            with _ficlone_lock:
                _ficlone_disabled = True
            try:
                target.unlink()
            except FileNotFoundError:
                pass
    copy_with_cp(source, target, target_mode)
    return "cp"


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


def build_git_add_args(
    args: argparse.Namespace,
    target: Path,
    commit: str | None,
    *,
    no_checkout: bool = False,
) -> list[str]:
    command = ["worktree", "add"]
    if args.force:
        command.append("--force")
    if args.branch:
        command.extend(["-b", args.branch])
    if args.reset_branch:
        command.extend(["-B", args.reset_branch])
    if args.detach:
        command.append("--detach")
    if args.orphan:
        command.append("--orphan")
    if args.lock:
        command.append("--lock")
    if args.reason is not None:
        command.extend(["--reason", args.reason])
    if args.track:
        command.append("--track")
    if args.guess_remote:
        command.append("--guess-remote")
    if args.relative_paths:
        command.append("--relative-paths")
    if no_checkout or args.no_checkout:
        command.append("--no-checkout")
    elif args.checkout:
        command.append("--checkout")
    if args.quiet:
        command.append("--quiet")
    command.append(str(target))
    if commit is not None:
        command.append(commit)
    return command


def normal_add(repo: Path, args: argparse.Namespace, target: Path) -> int:
    result = run_git(repo, build_git_add_args(args, target, args.commit), check=False)
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    if args.verbose:
        print("fallback=yes reason=copy-on-write-unavailable")
    return result.returncode


def run_post_checkout(target: Path, new_revision: str) -> None:
    hook_value = git_text(target, ["rev-parse", "--git-path", "hooks/post-checkout"])
    hook = Path(hook_value)
    if not hook.is_absolute():
        hook = (target / hook).resolve()
    if not hook.is_file() or not os.access(hook, os.X_OK):
        return

    git_dir = git_text(target, ["rev-parse", "--git-dir"])
    environment = os.environ.copy()
    environment["GIT_DIR"] = git_dir
    environment["GIT_WORK_TREE"] = str(target)
    environment["GIT_PREFIX"] = ""
    old_revision = "0" * len(new_revision)
    result = subprocess.run(
        [str(hook), old_revision, new_revision, "1"],
        cwd=target,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise PostCheckoutError(f"post-checkout hook failed: {detail}")


def add_worktree(args: argparse.Namespace) -> int:
    try:
        repo = repository_root(Path.cwd())
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

    if args.orphan or args.no_checkout:
        return normal_add(repo, args, target)

    try:
        target_rev = git_text(repo, ["rev-parse", "--verify", f"{args.commit or 'HEAD'}^{{commit}}"])
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

    add_result = run_git(repo, build_git_add_args(args, target, args.commit, no_checkout=True), check=False)
    if add_result.returncode:
        if add_result.stdout:
            sys.stdout.buffer.write(add_result.stdout)
        if add_result.stderr:
            sys.stderr.buffer.write(add_result.stderr)
        return add_result.returncode

    try:
        target_rev = git_text(target, ["rev-parse", "--verify", "HEAD^{commit}"])
        source = choose_source(repo, args.source, target, common_dir, target_rev)
        if source is None:
            reset_worktree(target)
            run_post_checkout(target, target_rev)
            if args.verbose:
                print(f"source=none target={target} reflinked=0 fallback=yes reason=no-clean-source")
            return 0
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
        backends: set[str] = set()
        eligible: list[tuple[str, os.stat_result, int]] = []
        for path in candidates:
            mode = int(target_tree[path][0], 8)
            source_path = source.path / path
            target_path = target / path
            source_stat = source_path.lstat()
            if not stat.S_ISREG(source_stat.st_mode):
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            eligible.append((path, source_stat, mode))

        copy_cancelled = threading.Event()

        def copy_one(item: tuple[str, os.stat_result, int]) -> tuple[str, str]:
            path, source_stat, target_mode = item
            source_path = source.path / path
            target_path = target / path
            if copy_cancelled.is_set():
                raise CowError("copy cancelled after an earlier reflink failure")
            try:
                backend = copy_reflink(source_path, target_path, source_stat, target_mode)
                if blob_hash(target, target_path) != target_tree[path][2]:
                    raise CowError(f"copied file did not match Git blob: {path}")
                return path, backend
            except BaseException:
                copy_cancelled.set()
                remove_file(target_path)
                raise

        workers = min(8, max(4, os.cpu_count() or 4))
        if eligible:
            copied_path, backend = copy_one(eligible[0])
            copied.add(copied_path)
            backends.add(backend)
        if len(eligible) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = set()
                next_index = 1
                while pending or next_index < len(eligible):
                    while next_index < len(eligible) and len(pending) < workers:
                        pending.add(pool.submit(copy_one, eligible[next_index]))
                        next_index += 1
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        copied_path, backend = future.result()
                        copied.add(copied_path)
                        backends.add(backend)

        if not copied:
            raise CowError("no eligible files could be reflinked")
        finish_checkout(target, target_rev, copied)
        run_post_checkout(target, target_rev)
    except PostCheckoutError as error:
        print(f"cow-worktree: {error}", file=sys.stderr)
        return 1
    except (CowError, OSError, subprocess.SubprocessError) as error:
        reset_worktree(target)
        try:
            run_post_checkout(target, target_rev)
        except PostCheckoutError as hook_error:
            print(f"cow-worktree: {hook_error}", file=sys.stderr)
            return 1
        if args.verbose:
            print(f"source={source.path} target={target} reflinked=0 fallback=yes reason={error}")
        return 0

    if args.verbose:
        backend = "+".join(sorted(backends))
        print(f"source={source.path} target={target} reflinked={len(copied)} backend={backend} fallback=no")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="git cowtree", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add = subparsers.add_parser("add", help="create a worktree with copy-on-write files")
    add.add_argument("--from", dest="source", help="source worktree to use")
    add.add_argument("-v", "--verbose", action="store_true")
    add.add_argument("-f", "--force", action="store_true")
    branch = add.add_mutually_exclusive_group()
    branch.add_argument("-b", "--branch")
    branch.add_argument("-B", dest="reset_branch")
    add.add_argument("--detach", action="store_true")
    add.add_argument("--orphan", action="store_true")
    add.add_argument("--lock", action="store_true")
    add.add_argument("--reason")
    add.add_argument("--no-checkout", action="store_true")
    add.add_argument("--checkout", action="store_true")
    add.add_argument("--track", action="store_true")
    add.add_argument("--guess-remote", action="store_true")
    add.add_argument("--relative-paths", action="store_true")
    add.add_argument("-q", "--quiet", action="store_true")
    add.add_argument("target")
    add.add_argument("commit", nargs="?", default=None)
    add.set_defaults(handler=add_worktree)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
