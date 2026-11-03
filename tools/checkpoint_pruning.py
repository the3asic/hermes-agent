"""Checked history rewrites shared by snapshot limits and store maintenance."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable


class PruneError(RuntimeError):
    """Pruning stopped without permission to discard another snapshot."""


GC_PENDING_NAME = ".gc-pending"


def store_lock_path(base: Path) -> Path:
    base = base.resolve()
    return base.with_name(f".{base.name}.lock")


@contextmanager
def store_lock(base: Path, *, repair_refs: bool = True):
    """Serialize whole operations, including GC and clear, across processes."""
    from hermes_cli.runtime_state import _lock

    base.parent.mkdir(parents=True, exist_ok=True)
    # Outside base so clear_all and legacy migration cannot replace its inode.
    fd = os.open(store_lock_path(base), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not _lock(fd, wait=False):
            raise PruneError(f"checkpoint store is busy: {base}")
        if repair_refs:
            from tools.checkpoint_manager import _store_path

            _repair_invalid_loose_refs(_store_path(base))
        yield
    finally:
        os.close(fd)


def _repair_invalid_loose_refs(store: Path) -> int:
    """Expose an intact packed predecessor, preserving each damaged loose ref.

    The caller holds the store fence; Git's own ref lock also excludes an
    external ``update-ref`` while the loose file is moved. Never infer a fresh
    project from corruption when Git cannot resolve a surviving packed commit.
    """
    import logging
    import re
    import time

    from tools.checkpoint_manager import _REFS_PREFIX, _ref_tip, _run_git, _store_has_head

    logger = logging.getLogger(__name__)
    if not _store_has_head(store):
        return 0
    ok, _, err = _run_git(["show-ref"], store, str(store.parent), allowed_returncodes={1, 128})
    if (ok and "broken ref" not in err.lower()) or not err:
        return 0
    if not any(message in err.lower() for message in ("bad ref", "broken ref")):
        raise PruneError(f"Cannot inspect checkpoint refs: {err}")

    repaired = 0
    for path in (store / _REFS_PREFIX).rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name.endswith(".lock"):
            continue
        ref = path.relative_to(store).as_posix()
        lock_path = path.with_name(path.name + ".lock")
        handle = lock_path.open("xb")  # An existing Git writer owns its lock; leave it untouched.
        try:
            with handle:
                original = path.read_bytes()
                oid = original.decode("ascii", errors="replace").strip()
                if oid.startswith("ref: "):
                    continue  # Symbolic refs are resolved by Git, never rewritten here.
                if re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", oid):
                    valid, _, object_err = _run_git(["cat-file", "-e", oid + "^{commit}"],
                                                    store, str(store.parent), allowed_returncodes={128})
                    if valid:
                        continue
                    if not any(message in object_err.lower() for message in
                               ("not a valid object name", "expected commit type")):
                        raise PruneError(f"Cannot inspect checkpoint object {oid}: {object_err}")
                quarantine_root = store.parent / "corrupt-refs"
                quarantine_root.mkdir(mode=0o700, exist_ok=True)
                quarantine = quarantine_root / f"{time.time_ns()}-{os.getpid()}" / ref
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, quarantine)
                try:
                    if _ref_tip(store, str(store.parent), ref) is None:
                        raise PruneError(f"No intact packed checkpoint history for {ref}")
                except Exception:
                    os.replace(quarantine, path)
                    # Failed recovery can be retried every turn; keep only the
                    # original ref, rather than growing empty quarantine trees.
                    for directory in quarantine.parents:
                        if directory == quarantine_root:
                            break
                        directory.rmdir()
                    raise
                repaired += 1
                logger.warning("Preserved damaged checkpoint ref %s in %s; reusing packed history", ref, quarantine)
        finally:
            lock_path.unlink(missing_ok=True)
    return repaired


@dataclass(frozen=True)
class Pruner:
    run_git: Callable
    store: Path
    cwd: str
    timeout: int
    dir_size: Callable[[Path], int]
    refs_prefix: str

    def _git(self, args: list[str], **kwargs) -> str:
        ok, output, error = self.run_git(args, self.store, self.cwd, **kwargs)
        if not ok:
            raise PruneError(f"checkpoint {args[0]} failed: {error}")
        return output

    def reclaim(self) -> None:
        """Full ``git gc`` — tens of seconds on a GB store, so only the periodic prune calls it.
        A ref rewrite that ran without it leaves a marker so the next prune reclaims."""
        self._git(["reflog", "expire", "--expire=now", "--all"])
        self._git(["gc", "--prune=now", "--quiet"], timeout=self.timeout * 3)
        # Bare stores must remain usable when gc packs all refs.
        for name in ("refs/heads", "branches"):
            (self.store / name).mkdir(parents=True, exist_ok=True)
        try:
            (self.store / GC_PENDING_NAME).unlink()
        except OSError:
            pass

    def gc_pending(self) -> bool:
        return (self.store / GC_PENDING_NAME).exists()

    def _mark_gc_pending(self) -> None:
        try:
            (self.store / GC_PENDING_NAME).touch()
        except OSError:
            pass

    def _commits(self, ref: str) -> list[str]:
        commits = self._git(["rev-list", "--reverse", ref]).splitlines()
        if not commits:
            raise PruneError(f"checkpoint ref has no readable history: {ref}")
        return commits

    def _rewrite(self, ref: str, original: list[str], keep: list[str], *, gc: bool) -> None:
        parent = None
        for sha in keep:
            metadata = self._git(["log", "-1", "--format=%T%x00%aI%x00%cI%x00%s", sha])
            fields = metadata.split("\x00", 3)
            if len(fields) != 4:
                raise PruneError(f"invalid checkpoint metadata: {sha}")
            tree, author_date, committer_date, message = fields
            args = ["commit-tree", tree, "--no-gpg-sign", "-m", message]
            if parent is not None:
                args += ["-p", parent]
            parent = self._git(args, extra_env={"GIT_AUTHOR_DATE": author_date, "GIT_COMMITTER_DATE": committer_date})
            if not parent:
                raise PruneError("commit-tree returned no checkpoint identity")
        if parent is None:
            raise PruneError("cannot remove a project's last snapshot")
        # Never overwrite a snapshot published since we read this history.
        self._git(["update-ref", ref, parent, original[-1]])
        if gc:
            self.reclaim()
        else:
            self._mark_gc_pending()

    def trim(self, ref: str, keep_count: int) -> None:
        """Snapshot-count budget on the checkpoint-take path: rewrites the ref, defers the gc."""
        commits = self._commits(ref)
        keep_count = max(1, keep_count)
        if len(commits) > keep_count:
            self._rewrite(ref, commits, commits[-keep_count:], gc=False)

    def drop_one_round(self, cap_bytes: int) -> bool:
        """Over the cap on the take path: drop the oldest snapshot of every project once and
        defer the gc. One round per checkpoint converges over turns; the full loop measured
        against an unchanging pack once flattened every project to a single snapshot."""
        if cap_bytes <= 0 or self.dir_size(self.store) <= cap_bytes:
            return False
        changed = False
        for ref in self._git(["for-each-ref", "--format=%(refname)", self.refs_prefix]).splitlines():
            commits = self._commits(ref)
            if len(commits) > 1:
                self._rewrite(ref, commits, commits[1:], gc=False)
                changed = True
        return changed

    def enforce_size(self, cap_bytes: int) -> bool:
        """Periodic prune: gc per round (the measurement only moves after a repack) until the
        store fits. Returns whether it did; a failed Git step raises immediately."""
        if cap_bytes <= 0 or self.dir_size(self.store) <= cap_bytes:
            return True
        self.reclaim()
        if self.dir_size(self.store) <= cap_bytes:
            return True
        refs = self._git(["for-each-ref", "--format=%(refname)", self.refs_prefix]).splitlines()
        # Keep the existing round-robin policy, remeasure after EACH rewrite.
        # Twenty rounds bound maintenance when many large histories remain.
        for _ in range(20):
            changed = False
            for ref in refs:
                commits = self._commits(ref)
                if len(commits) <= 1:
                    continue
                self._rewrite(ref, commits, commits[1:], gc=True)
                changed = True
                if self.dir_size(self.store) <= cap_bytes:
                    return True
            if not changed:
                break
        return False
