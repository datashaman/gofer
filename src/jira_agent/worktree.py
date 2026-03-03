from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Worktree:
    issue_key: str
    repo_path: Path
    worktree_path: Path
    branch: str
    base_branch: str


async def _run_git(*args: str, cwd: Path) -> str:
    """Run a git command asynchronously and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode().strip()}"
        )
    return stdout.decode().strip()


async def worktree_exists(repo_path: str | Path, issue_key: str) -> bool:
    """Check whether a worktree for the given issue already exists."""
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
) -> Worktree:
    """Create a git worktree for the given issue. Idempotent — returns existing if present."""
    repo = Path(repo_path).resolve()
    wt_dir = repo / ".worktrees"
    wt_path = wt_dir / issue_key
    branch = f"ticket/{issue_key}"

    if await worktree_exists(repo, issue_key):
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
