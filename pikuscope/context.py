"""Repository context providers: local git checkout (preferred) or GitHub API."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .gh import Repo


class RepoContext:
    """Interface: file access + code search at the PR head commit."""

    def file_content(self, path: str) -> str | None:
        raise NotImplementedError

    def search(self, pattern: str, max_results: int = 40) -> list[tuple[str, int, str]]:
        raise NotImplementedError

    def list_files(self, prefix: str = "") -> list[str]:
        raise NotImplementedError


class GitRepoContext(RepoContext):
    """Context over a local checkout at the PR head SHA."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._files: list[str] | None = None

    def file_content(self, path: str) -> str | None:
        p = self.root / path
        if not p.is_file():
            return None
        try:
            if p.stat().st_size > 400_000:
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def search(self, pattern: str, max_results: int = 40) -> list[tuple[str, int, str]]:
        try:
            proc = subprocess.run(
                ["rg", "--line-number", "--no-heading", "--max-count", "4",
                 "--max-columns", "300", "-e", pattern,
                 "-g", "!node_modules", "-g", "!dist", "-g", "!*.lock", "-g", "!*.min.*"],
                cwd=self.root, capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        out: list[tuple[str, int, str]] = []
        for line in proc.stdout.splitlines():
            m = re.match(r"^(.+?):(\d+):(.*)$", line)
            if m:
                out.append((m.group(1), int(m.group(2)), m.group(3)))
            if len(out) >= max_results:
                break
        return out

    def list_files(self, prefix: str = "") -> list[str]:
        if self._files is None:
            proc = subprocess.run(
                ["git", "ls-files"], cwd=self.root, capture_output=True, text=True
            )
            self._files = proc.stdout.splitlines()
        if prefix:
            return [f for f in self._files if f.startswith(prefix)]
        return self._files


class ApiRepoContext(RepoContext):
    """Context via GitHub contents API at a fixed ref (no clone needed)."""

    def __init__(self, repo: Repo, ref: str):
        self.repo = repo
        self.ref = ref
        self._files: list[str] | None = None

    def file_content(self, path: str) -> str | None:
        return self.repo.file_at(path, self.ref)

    def search(self, pattern: str, max_results: int = 40) -> list[tuple[str, int, str]]:
        return []  # code search API is ref-less; skip for API mode

    def list_files(self, prefix: str = "") -> list[str]:
        if self._files is None:
            try:
                self._files = self.repo.tree(self.ref)
            except Exception:  # noqa: BLE001
                self._files = []
        if prefix:
            return [f for f in self._files if f.startswith(prefix)]
        return self._files


def ensure_checkout(clone_dir: str | Path, full_name: str, sha: str,
                    pr_number: int | None = None) -> GitRepoContext:
    """Maintain a bare-ish clone and produce a worktree at `sha`."""
    clone_dir = Path(clone_dir)
    repo_dir = clone_dir / "repo.git"
    if not repo_dir.exists():
        clone_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--bare", "--filter=blob:none",
             f"https://github.com/{full_name}.git", str(repo_dir)],
            check=True, capture_output=True,
        )
    wt = clone_dir / "wt" / sha[:12]
    if not (wt / ".git").exists() and not (wt.exists() and any(wt.iterdir()) if wt.exists() else False):
        have = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                              cwd=repo_dir, capture_output=True)
        if have.returncode != 0:
            refspec = f"refs/pull/{pr_number}/head" if pr_number else sha
            subprocess.run(["git", "fetch", "origin", refspec],
                           cwd=repo_dir, check=True, capture_output=True)
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "--detach", str(wt), sha],
                       cwd=repo_dir, check=True, capture_output=True)
    return GitRepoContext(wt)


def file_tree_summary(ctx: RepoContext, max_entries: int = 400) -> str:
    """Compact directory tree for orientation prompts."""
    files = ctx.list_files()
    dirs: dict[str, int] = {}
    for f in files:
        parts = f.split("/")
        for depth in (1, 2, 3):
            if len(parts) > depth:
                dirs["/".join(parts[:depth]) + "/"] = dirs.get("/".join(parts[:depth]) + "/", 0) + 1
    top = [f for f in files if "/" not in f]
    lines = [f"{d} ({n} files)" for d, n in sorted(dirs.items()) if n >= 3]
    lines += top
    if len(lines) > max_entries:
        lines = lines[:max_entries] + ["..."]
    return "\n".join(lines)
