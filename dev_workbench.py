"""
Lamplight Developer Workbench & Local Repository Intelligence Module
Provides guarded Git inspection, file browsing, diff review, patch application with rollbacks,
and bounded test execution for the Architect persona and Workbench UI.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import db

# Ignored directory names when scanning file trees
IGNORED_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "dist",
    "build",
    "backups",
    ".system_generated",
    ".egg-info",
}

# Blocked dangerous command substrings
DANGEROUS_COMMAND_PATTERNS = [
    r"rm\s+-r[fF]?\s+[/~]",
    r">\s*/dev/sd[a-z]",
    r">\s*/dev/nvme",
    r"mkfs",
    r":\(\)\s*\{",
    r"chmod\s+-R\s+777\s+/",
    r"shutdown",
    r"reboot",
    r"init\s+0",
]

PATCH_BACKUP_DIR = Path("backups/patches")


def resolve_repo_path(workspace_id: Optional[str] = None) -> Optional[Path]:
    """
    Resolves the directory to inspect.
    Prioritizes workspace-configured repo_path; falls back to current working directory if it is a git repo.
    """
    if workspace_id:
        ws = db.get_workspace(workspace_id)
        if ws and ws.get("repo_path"):
            p = Path(ws["repo_path"]).expanduser().resolve()
            if p.is_dir():
                return p

    # Fallback: check current working directory
    cwd = Path.cwd().resolve()
    if (cwd / ".git").exists() or cwd.is_dir():
        return cwd

    return None


def get_git_status(repo_dir: Path) -> Dict[str, Any]:
    """
    Returns real-time Git status, current branch, ahead/behind tracking,
    staged/unstaged files, and recent commit history.
    """
    if not (repo_dir / ".git").exists():
        return {
            "is_git_repo": False,
            "repo_name": repo_dir.name,
            "repo_path": str(repo_dir),
            "branch": None,
            "clean": True,
            "staged": [],
            "unstaged": [],
            "untracked": [],
            "recent_commits": [],
            "summary": "Not a Git repository.",
        }

    try:
        # 1. Branch and status porcelain
        res = subprocess.run(
            ["git", "status", "--porcelain=v1", "-b"],
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
            timeout=8,
        )
        lines = [line.rstrip() for line in res.stdout.splitlines() if line.strip()]

        branch = "HEAD"
        tracking = ""
        staged: List[Dict[str, str]] = []
        unstaged: List[Dict[str, str]] = []
        untracked: List[str] = []

        if lines and lines[0].startswith("## "):
            branch_info = lines[0][3:].strip()
            if "..." in branch_info:
                parts = branch_info.split("...")
                branch = parts[0]
                tracking = parts[1] if len(parts) > 1 else ""
            else:
                branch = branch_info

        for line in lines[1:]:
            if line.startswith("## "):
                continue
            if line.startswith("??"):
                untracked.append(line[3:].strip())
                continue

            status_x = line[0]
            status_y = line[1]
            filepath = line[3:].strip()

            if status_x != " " and status_x != "?":
                staged.append({"path": filepath, "status": status_x})
            if status_y != " " and status_y != "?":
                unstaged.append({"path": filepath, "status": status_y})

        # 2. Recent 5 commits
        log_res = subprocess.run(
            ["git", "log", "-n", "5", "--pretty=format:%h|%an|%ar|%s"],
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
            timeout=6,
        )
        recent_commits = []
        for line in log_res.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                recent_commits.append({
                    "sha": parts[0],
                    "author": parts[1],
                    "time": parts[2],
                    "message": parts[3],
                })

        clean = len(staged) == 0 and len(unstaged) == 0 and len(untracked) == 0

        summary = f"Branch: {branch} ({'Clean' if clean else f'{len(unstaged)} modified, {len(staged)} staged, {len(untracked)} untracked'})"

        return {
            "is_git_repo": True,
            "repo_name": repo_dir.name,
            "repo_path": str(repo_dir),
            "branch": branch,
            "tracking": tracking,
            "clean": clean,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "recent_commits": recent_commits,
            "summary": summary,
        }
    except Exception as e:
        return {
            "is_git_repo": True,
            "repo_name": repo_dir.name,
            "repo_path": str(repo_dir),
            "error": str(e),
            "clean": False,
            "staged": [],
            "unstaged": [],
            "untracked": [],
            "recent_commits": [],
            "summary": f"Git status error: {e}",
        }


def get_git_diff(
    repo_dir: Path, filepath: Optional[str] = None, staged: bool = False
) -> str:
    """
    Returns unified diff of working tree or staged changes.
    Optionally filters by a specific file.
    """
    if not (repo_dir / ".git").exists():
        return ""

    cmd = ["git", "diff"]
    if staged:
        cmd.append("--cached")
    if filepath:
        cmd.extend(["--", filepath])

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
            timeout=10,
        )
        diff = res.stdout
        # Limit diff to 250 KB
        if len(diff) > 250_000:
            diff = diff[:250_000] + "\n\n... [Diff truncated at 250 KB for safety] ..."
        return diff
    except Exception as e:
        return f"Error retrieving git diff: {e}"


def get_file_tree(
    repo_dir: Path, subpath: str = "", max_depth: int = 3
) -> List[Dict[str, Any]]:
    """
    Recursively scans the repository directory tree up to max_depth.
    Skips ignored folders like .git, node_modules, etc.
    """
    target = (repo_dir / subpath).resolve()
    if not str(target).startswith(str(repo_dir.resolve())):
        return []

    items: List[Dict[str, Any]] = []

    def _scan(current_dir: Path, depth: int) -> List[Dict[str, Any]]:
        if depth > max_depth or not current_dir.is_dir():
            return []

        result = []
        try:
            entries = sorted(
                current_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
            for entry in entries:
                if entry.name.startswith(".") and entry.name in IGNORED_DIR_NAMES:
                    continue
                if entry.name in IGNORED_DIR_NAMES:
                    continue

                rel = entry.relative_to(repo_dir).as_posix()
                if entry.is_dir():
                    children = _scan(entry, depth + 1)
                    result.append({
                        "name": entry.name,
                        "path": rel,
                        "is_dir": True,
                        "children": children,
                    })
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    result.append({
                        "name": entry.name,
                        "path": rel,
                        "is_dir": False,
                        "size": size,
                        "ext": entry.suffix.lstrip(".").lower(),
                    })
        except PermissionError:
            pass
        return result

    return _scan(target, 1)


def read_repo_file(
    repo_dir: Path, rel_path: str, max_bytes: int = 300_000
) -> Dict[str, Any]:
    """
    Safely reads a text file within repo_dir with traversal protection and size limits.
    """
    target = (repo_dir / rel_path).resolve()
    if not str(target).startswith(str(repo_dir.resolve())):
        return {"error": "Path traversal prohibited: target is outside repository root."}

    if not target.exists():
        return {"error": f"File '{rel_path}' does not exist."}
    if target.is_dir():
        return {"error": f"Target '{rel_path}' is a directory, not a file."}

    try:
        size = target.stat().st_size
        if size > max_bytes:
            content = target.read_text(encoding="utf-8", errors="replace")[:max_bytes]
            return {
                "path": rel_path,
                "content": content,
                "size": size,
                "truncated": True,
                "lines": content.count("\n") + 1,
                "ext": target.suffix.lstrip(".").lower(),
            }

        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "path": rel_path,
            "content": content,
            "size": size,
            "truncated": False,
            "lines": content.count("\n") + 1,
            "ext": target.suffix.lstrip(".").lower(),
        }
    except Exception as e:
        return {"error": f"Failed to read file: {e}"}


def apply_patch(repo_dir: Path, patch_text: str) -> Dict[str, Any]:
    """
    Applies a unified patch safely.
    First runs `git apply --check`. If clean, backups original files before applying.
    """
    if not (repo_dir / ".git").exists():
        return {"success": False, "error": "Target directory is not a Git repository."}

    patch_cleaned = patch_text.strip()
    if not patch_cleaned:
        return {"success": False, "error": "Patch content is empty."}

    # 1. Dry run with git apply --check
    check_res = subprocess.run(
        ["git", "apply", "--check", "-"],
        input=patch_cleaned,
        text=True,
        capture_output=True,
        cwd=str(repo_dir),
        timeout=8,
    )
    if check_res.returncode != 0:
        return {
            "success": False,
            "error": f"Patch check failed: {check_res.stderr or check_res.stdout or 'Invalid patch format'}",
        }

    # 2. Extract filenames touched to create rollback backup
    touched_files = re.findall(r"^--- a/(.*?)$", patch_cleaned, re.MULTILINE)
    backup_id = f"patch_{int(time.time())}_{os.urandom(3).hex()}"
    backup_path = PATCH_BACKUP_DIR / backup_id
    backup_path.mkdir(parents=True, exist_ok=True)

    backed_up_count = 0
    for f in touched_files:
        src = repo_dir / f
        if src.exists() and src.is_file():
            dst = backup_path / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            backed_up_count += 1

    # 3. Apply the patch
    apply_res = subprocess.run(
        ["git", "apply", "-"],
        input=patch_cleaned,
        text=True,
        capture_output=True,
        cwd=str(repo_dir),
        timeout=8,
    )
    if apply_res.returncode != 0:
        # Rollback automatically if applying failed
        rollback_patch(repo_dir, backup_id)
        return {
            "success": False,
            "error": f"Patch application failed: {apply_res.stderr}",
        }

    return {
        "success": True,
        "backup_id": backup_id,
        "files_modified": touched_files,
        "backed_up_count": backed_up_count,
        "message": f"Successfully applied patch to {len(touched_files)} file(s).",
    }


def rollback_patch(repo_dir: Path, backup_id: str) -> Dict[str, Any]:
    """
    Rolls back files from a previous patch backup snapshot.
    """
    backup_path = PATCH_BACKUP_DIR / backup_id
    if not backup_path.exists() or not backup_path.is_dir():
        return {"success": False, "error": f"Backup snapshot '{backup_id}' not found."}

    restored = []
    try:
        for root, _, files in os.walk(backup_path):
            for file in files:
                bk_file = Path(root) / file
                rel = bk_file.relative_to(backup_path)
                target = repo_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bk_file, target)
                restored.append(str(rel))

        return {
            "success": True,
            "restored_files": restored,
            "message": f"Successfully rolled back {len(restored)} file(s).",
        }
    except Exception as e:
        return {"success": False, "error": f"Rollback failed: {e}"}


def execute_guarded_command(
    repo_dir: Path, command: str, timeout: int = 15
) -> Dict[str, Any]:
    """
    Executes a bounded command within repo_dir with safety guards and timeouts.
    Disallows root manipulation, recursive deletions outside bounds, and fork bombs.
    """
    cmd_clean = command.strip()
    if not cmd_clean:
        return {"success": False, "error": "Command is empty."}

    # Safety check against dangerous patterns
    for pat in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pat, cmd_clean, re.IGNORECASE):
            return {
                "success": False,
                "error": f"Command rejected by safety guard: matched forbidden pattern '{pat}'",
                "blocked": True,
            }

    # Restrict timeout
    safe_timeout = min(max(1, timeout), 30)

    start_time = time.perf_counter()
    try:
        env = os.environ.copy()
        # Ensure PATH is available
        env["PYTHONUNBUFFERED"] = "1"

        res = subprocess.run(
            cmd_clean,
            shell=True,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=safe_timeout,
            env=env,
        )
        elapsed_ms = round((time.perf_counter() - start_time) * 1000)

        stdout = res.stdout
        stderr = res.stderr

        # Limit output length to prevent browser freeze
        max_out = 150_000
        if len(stdout) > max_out:
            stdout = stdout[:max_out] + "\n... [Output truncated at 150 KB] ..."
        if len(stderr) > max_out:
            stderr = stderr[:max_out] + "\n... [Stderr truncated at 150 KB] ..."

        return {
            "success": res.returncode == 0,
            "command": cmd_clean,
            "exit_code": res.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "elapsed_ms": elapsed_ms,
            "cwd": str(repo_dir),
        }
    except subprocess.TimeoutExpired:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000)
        return {
            "success": False,
            "command": cmd_clean,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {safe_timeout} seconds.",
            "elapsed_ms": elapsed_ms,
            "timed_out": True,
            "cwd": str(repo_dir),
        }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000)
        return {
            "success": False,
            "command": cmd_clean,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Execution error: {e}",
            "elapsed_ms": elapsed_ms,
            "cwd": str(repo_dir),
        }


def get_architect_repo_summary(repo_dir: Path) -> str:
    """
    Generates a concise 3-4 line live context summary of the repo for Architect's system prompt.
    """
    if not (repo_dir / ".git").exists():
        return f"Working Directory: `{repo_dir}` (Non-git directory)"

    status = get_git_status(repo_dir)
    branch = status.get("branch") or "HEAD"
    clean = status.get("clean", True)
    unstaged_count = len(status.get("unstaged", []))
    staged_count = len(status.get("staged", []))
    untracked_count = len(status.get("untracked", []))

    recent = status.get("recent_commits", [])
    last_commit_str = ""
    if recent:
        last = recent[0]
        last_commit_str = f"Last Commit: `{last['sha']}` ({last['message'][:60]}) by {last['author']}, {last['time']}"

    state_str = "Working tree clean" if clean else f"Dirty ({unstaged_count} modified, {staged_count} staged, {untracked_count} untracked)"

    return (
        f"Active Repository: `{repo_dir.name}` ({repo_dir})\n"
        f"Git Branch: `{branch}` | Status: {state_str}\n"
        f"{last_commit_str}"
    )
