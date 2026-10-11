"""A Git repository as an SOP source.

SOP files are read from one branch, under one directory, through the `git`
command with a fixed argument list. The repository is kept as a bare mirror
in a cache directory; nothing from it is checked out or executed.

Verified against local repositories only. Fetching from a hosted Git
service with a token is written from Git's documentation.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.l1_agent.config.settings import ConfigError
from src.l1_agent.connectors.knowledge.base import MAX_ARTICLE_CHARS, Article
from src.l1_agent.connectors.sdk.manifest import (
    Capability,
    Family,
    Kind,
    Manifest,
    RiskTier,
    SecretSlot,
)
from src.l1_agent.connectors.sdk.schema import closed
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets
from src.l1_agent.utils.logging import get_logger

logger = get_logger("git_knowledge")

PATTERNS = ("*.json", "*.md", "*.txt")
MAX_FILES = 2000
GIT_TIMEOUT_SECONDS = 120
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,200}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._/\- ]*$")
_URL_RE = re.compile(r"^(https://|ssh://)[A-Za-z0-9._~:/@\-]+$")


class GitError(RuntimeError):
    """A git command failed. Carries its exit code and the first line it printed."""


def _check_location(url: str, *, allow_local: bool) -> str:
    if _URL_RE.match(url):
        return url
    if allow_local and Path(url).is_absolute() and Path(url).exists():
        return str(Path(url))
    raise ConfigError("The Git source must be an https:// or ssh:// URL")


class GitKnowledge:
    manifest = Manifest(
        id="git_knowledge",
        version="1.0.0",
        family=Family.KNOWLEDGE,
        display_name="Git repository",
        notes="Needs the git command and read access to the repository.",
        config=closed(
            {
                "url": {"type": "string"},
                "ref": {"type": "string"},
                "path": {"type": "string"},
                "cache_dir": {"type": "string"},
            },
            required=["url"],
        ),
        secrets=(SecretSlot("token", "Access token for an https repository", required=False),),
        capabilities=(
            Capability(
                id="git_knowledge.files.list",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="SOP files changed since a commit",
                parameters=closed({"cursor": {"type": "string", "maxLength": 40}}),
                scopes=("repository.read",),
            ),
        ),
    )
    source_id = "git"

    def __init__(
        self,
        url: str,
        ref: str = "main",
        path: str = "",
        cache_dir: str = "",
        secrets: Optional[ConnectionSecrets] = None,
        *,
        demo_mode: bool = False,
    ) -> None:
        self._url = _check_location(url, allow_local=demo_mode)
        if not _REF_RE.match(ref) or ".." in ref:
            raise ConfigError("The Git branch name is not valid")
        clean = path.strip("/")
        if not _PATH_RE.match(clean) or ".." in clean.split("/"):
            raise ConfigError("The Git path is not valid")
        self._ref = ref
        self._path = clean
        self._secrets = secrets
        base = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "l1-agent-git"
        key = hashlib.sha256(f"{self._url}\n{ref}".encode("utf-8")).hexdigest()[:16]
        self._dir = base / key
        self._lock = asyncio.Lock()

    # ── git ───────────────────────────────────────────────────────────

    async def _env(self) -> Dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LC_ALL": "C",
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        config = [("protocol.ext.allow", "never"), ("core.hooksPath", os.devnull)]
        token = await self._secrets.get("token") if self._secrets else ""
        if token and self._url.startswith("https://"):
            # Through the environment, so the token is never on a command line.
            config.append(("http.extraHeader", f"Authorization: Bearer {token}"))
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        return env

    async def _git(self, *args: str, cwd: Optional[Path] = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd) if cwd else None,
            env=await self._env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            raise GitError(f"git {args[0]} timed out") from None
        if process.returncode != 0:
            first = (err.decode("utf-8", "replace").strip().splitlines() or [""])[0][:200]
            raise GitError(f"git {args[0]} failed ({process.returncode}): {first}")
        return out.decode("utf-8", "replace")

    async def _sync(self) -> str:
        """Bring the mirror up to date and return the commit the branch points at."""
        if not (self._dir / "HEAD").exists():
            self._dir.parent.mkdir(parents=True, exist_ok=True)
            await self._git("init", "--quiet", "--bare", str(self._dir))
        await self._git(
            "fetch", "--quiet", "--no-tags", "--force", "--", self._url,
            f"+refs/heads/{self._ref}:refs/heads/{self._ref}",
            cwd=self._dir,
        )  # fmt: skip
        return (await self._git("rev-parse", f"refs/heads/{self._ref}", cwd=self._dir)).strip()

    def _wanted(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name.rsplit("/", 1)[-1], p) for p in PATTERNS)

    async def _files(self, head: str, cursor: str) -> List[str]:
        scope = ["--", self._path] if self._path else []
        if cursor:
            try:
                listing = await self._git(
                    "diff", "--name-only", "--diff-filter=AM", "-z", cursor, head, *scope,
                    cwd=self._dir,
                )  # fmt: skip
            except GitError:
                cursor = ""  # the old commit is gone (history was rewritten): read everything
        if not cursor:
            listing = await self._git(
                "ls-tree", "-r", "--name-only", "-z", head, *scope, cwd=self._dir
            )
        names = [n for n in listing.split("\0") if n and self._wanted(n)]
        if len(names) > MAX_FILES:
            logger.warning(
                "Git source has %d SOP files; reading the first %d", len(names), MAX_FILES
            )
        return sorted(names)[:MAX_FILES]

    async def _article(self, head: str, name: str) -> Optional[Article]:
        try:
            blob = (await self._git("rev-parse", f"{head}:{name}", cwd=self._dir)).strip()
            body = await self._git("cat-file", "blob", blob, cwd=self._dir)
        except GitError:
            return None
        return Article(
            id=name,
            title=name.rsplit("/", 1)[-1].rsplit(".", 1)[0],
            body=body[:MAX_ARTICLE_CHARS],
            revision=blob,
            metadata={"commit": head, "ref": self._ref},
        )

    # ── Source ────────────────────────────────────────────────────────

    async def changed_since(self, cursor: str) -> Tuple[List[Article], str]:
        if cursor and not _SHA_RE.match(cursor):
            raise ValueError("The Git cursor is not a commit id")
        async with self._lock:
            head = await self._sync()
            if cursor == head:
                return [], head
            found = [await self._article(head, name) for name in await self._files(head, cursor)]
        return [a for a in found if a], head

    async def fetch(self, article_id: str) -> Optional[Article]:
        if not _PATH_RE.match(article_id) or ".." in article_id.split("/"):
            return None
        async with self._lock:
            head = await self._sync()
            return await self._article(head, article_id)

    async def close(self) -> None:
        return None
