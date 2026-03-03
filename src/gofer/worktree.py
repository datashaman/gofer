from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from .events import validate_issue_key

logger = logging.getLogger(__name__)


@dataclass
class ExistingWork:
    commits: list[str]
    has_uncommitted: bool
    has_remote_branch: bool
    pr_url: str | None

    @property
    def has_prior_work(self) -> bool:
        return bool(self.commits) or self.has_uncommitted or self.has_remote_branch


@dataclass
class Worktree:
    issue_key: str
    repo_path: Path
    worktree_path: Path
    branch: str
    base_branch: str


_GIT_TIMEOUT = 60  # seconds


async def _run_cmd(*args: str, cwd: Path, timeout: int = _GIT_TIMEOUT) -> str:
    """Run an arbitrary command asynchronously and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{' '.join(args)} timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed (rc={proc.returncode}): {stderr.decode().strip()}"
        )
    return stdout.decode().strip()


async def _run_git(*args: str, cwd: Path, timeout: int = _GIT_TIMEOUT) -> str:
    """Run a git command asynchronously and return stdout."""
    return await _run_cmd("git", *args, cwd=cwd, timeout=timeout)


async def worktree_exists(repo_path: str | Path, issue_key: str) -> bool:
    """Check whether a worktree for the given issue already exists."""
    validate_issue_key(issue_key)
    wt_path = Path(repo_path) / ".worktrees" / issue_key
    if not wt_path.exists():
        return False
    # Verify git also knows about it
    try:
        raw = await _run_git("worktree", "list", "--porcelain", cwd=Path(repo_path))
    except RuntimeError:
        return False
    return str(wt_path) in raw


async def create_worktree(
    repo_path: str | Path,
    issue_key: str,
    base_branch: str = "main",
    force_new: bool = False,
) -> Worktree:
    """Create a git worktree for the given issue. Idempotent — returns existing if present.

    When *force_new* is True, any existing worktree is removed first so the
    branch starts fresh from ``origin/{base_branch}``.
    """
    validate_issue_key(issue_key)
    repo = Path(repo_path).resolve()
    wt_dir = repo / ".worktrees"
    wt_path = wt_dir / issue_key
    branch = f"ticket/{issue_key}"

    if await worktree_exists(repo, issue_key):
        if force_new:
            logger.info("force_new: removing existing worktree for %s", issue_key)
            await remove_worktree(
                Worktree(
                    issue_key=issue_key,
                    repo_path=repo,
                    worktree_path=wt_path,
                    branch=branch,
                    base_branch=base_branch,
                )
            )
        else:
            logger.info("Worktree already exists for %s at %s", issue_key, wt_path)
            return Worktree(
                issue_key=issue_key,
                repo_path=repo,
                worktree_path=wt_path,
                branch=branch,
                base_branch=base_branch,
            )

    wt_dir.mkdir(parents=True, exist_ok=True)

    # Fetch latest from origin
    logger.debug("Fetching latest from origin in %s", repo)
    try:
        await _run_git("fetch", "origin", cwd=repo)
    except RuntimeError:
        logger.warning("Failed to fetch from origin — proceeding with local state")

    # Create worktree with a new branch based on the base branch
    logger.info("Creating worktree for %s at %s (branch %s from %s)", issue_key, wt_path, branch, base_branch)
    await _run_git(
        "worktree",
        "add",
        "-b",
        branch,
        str(wt_path),
        f"origin/{base_branch}",
        cwd=repo,
    )

    return Worktree(
        issue_key=issue_key,
        repo_path=repo,
        worktree_path=wt_path,
        branch=branch,
        base_branch=base_branch,
    )


async def remove_worktree(worktree: Worktree) -> None:
    """Remove a worktree and its branch."""
    logger.info("Removing worktree for %s at %s", worktree.issue_key, worktree.worktree_path)

    try:
        await _run_git("worktree", "remove", str(worktree.worktree_path), "--force", cwd=worktree.repo_path)
    except RuntimeError:
        logger.warning("git worktree remove failed — falling back to shutil + prune")
        if worktree.worktree_path.exists():
            shutil.rmtree(worktree.worktree_path)
        try:
            await _run_git("worktree", "prune", cwd=worktree.repo_path)
        except RuntimeError:
            logger.warning("git worktree prune failed")

    # Delete the branch
    try:
        await _run_git("branch", "-D", worktree.branch, cwd=worktree.repo_path)
    except RuntimeError:
        logger.debug("Could not delete branch %s (may not exist)", worktree.branch)


async def detect_existing_work(worktree: Worktree) -> ExistingWork:
    """Detect prior work on a worktree branch. All checks are best-effort."""
    cwd = worktree.worktree_path

    # Commits since base branch
    commits: list[str] = []
    try:
        raw = await _run_git(
            "log", f"origin/{worktree.base_branch}..HEAD", "--oneline", cwd=cwd
        )
        if raw:
            commits = raw.splitlines()
    except RuntimeError:
        logger.debug("Could not read commit log for %s", worktree.branch)

    # Uncommitted changes
    has_uncommitted = False
    try:
        raw = await _run_git("status", "--porcelain", cwd=cwd)
        has_uncommitted = bool(raw)
    except RuntimeError:
        logger.debug("Could not check git status for %s", worktree.branch)

    # Remote branch exists
    has_remote_branch = False
    try:
        raw = await _run_git(
            "branch", "-r", "--list", f"origin/{worktree.branch}", cwd=cwd
        )
        has_remote_branch = bool(raw.strip())
    except RuntimeError:
        logger.debug("Could not check remote branch for %s", worktree.branch)

    # Open PR
    pr_url: str | None = None
    try:
        raw = await _run_cmd(
            "gh", "pr", "list", "--head", worktree.branch,
            "--json", "url", "--jq", ".[0].url",
            cwd=cwd,
        )
        if raw:
            pr_url = raw
    except RuntimeError:
        logger.debug("Could not check PR status for %s", worktree.branch)

    return ExistingWork(
        commits=commits,
        has_uncommitted=has_uncommitted,
        has_remote_branch=has_remote_branch,
        pr_url=pr_url,
    )
