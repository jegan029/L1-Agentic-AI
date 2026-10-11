"""The Git SOP source against real local repositories.

Fetching from a hosted Git service with a token is not covered: there is no
such service here. What is covered is the command lines, the change cursor
and what the source refuses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.l1_agent.config.settings import ConfigError
from src.l1_agent.connectors.knowledge.git import GitError, GitKnowledge
from src.l1_agent.connectors.sdk.secrets import LiteralSecrets

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

SOP = {"sop_id": "SOP-GIT-1", "title": "From Git", "steps": []}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "sops-repo"
    (path / "sops").mkdir(parents=True)
    git(path, "init", "--quiet", "-b", "main")
    (path / "sops" / "mq.json").write_text(json.dumps(SOP), encoding="utf-8")
    (path / "sops" / "disk.md").write_text("# Disk space\nCheck free space.\n", encoding="utf-8")
    (path / "sops" / "tool.py").write_text("print('not an SOP')\n", encoding="utf-8")
    (path / "README.md").write_text("outside the SOP directory\n", encoding="utf-8")
    commit(path, "first")
    return path


def source(repo: Path, tmp_path, **kwargs) -> GitKnowledge:
    values = dict(ref="main", path="sops", cache_dir=str(tmp_path / "cache"), demo_mode=True)
    values.update(kwargs)
    return GitKnowledge(str(repo), **values)


class TestChanges:
    async def test_first_read_returns_every_sop_file_under_the_path(self, repo, tmp_path):
        articles, cursor = await source(repo, tmp_path).changed_since("")
        assert [a.id for a in articles] == ["sops/disk.md", "sops/mq.json"]
        assert cursor == git(repo, "rev-parse", "HEAD")
        mq = next(a for a in articles if a.id == "sops/mq.json")
        assert json.loads(mq.body) == SOP and len(mq.revision) == 40
        assert mq.metadata["commit"] == cursor

    async def test_nothing_new_returns_nothing(self, repo, tmp_path):
        src = source(repo, tmp_path)
        _, cursor = await src.changed_since("")
        assert await src.changed_since(cursor) == ([], cursor)

    async def test_only_added_and_modified_files_come_back(self, repo, tmp_path):
        src = source(repo, tmp_path)
        before, cursor = await src.changed_since("")
        (repo / "sops" / "mq.json").write_text(json.dumps({**SOP, "title": "v2"}), encoding="utf-8")
        (repo / "sops" / "new.txt").write_text("A new procedure.\n", encoding="utf-8")
        (repo / "sops" / "disk.md").unlink()
        (repo / "README.md").write_text("still outside\n", encoding="utf-8")
        head = commit(repo, "second")

        articles, new_cursor = await src.changed_since(cursor)
        assert [a.id for a in articles] == ["sops/mq.json", "sops/new.txt"]
        assert new_cursor == head
        old_revision = next(a.revision for a in before if a.id == "sops/mq.json")
        assert articles[0].revision != old_revision, "a changed file has a new revision"

    async def test_a_cursor_that_no_longer_exists_reads_everything(self, repo, tmp_path):
        articles, _ = await source(repo, tmp_path).changed_since("f" * 40)
        assert len(articles) == 2

    async def test_fetch_returns_one_file_and_nothing_outside_the_tree(self, repo, tmp_path):
        src = source(repo, tmp_path)
        assert (await src.fetch("sops/disk.md")).title == "disk"
        assert await src.fetch("sops/absent.md") is None
        assert await src.fetch("../../etc/passwd") is None

    async def test_an_unknown_branch_is_an_error(self, repo, tmp_path):
        with pytest.raises(GitError):
            await source(repo, tmp_path, ref="no-such-branch").changed_since("")

    async def test_a_cursor_that_is_not_a_commit_id_is_refused(self, repo, tmp_path):
        with pytest.raises(ValueError):
            await source(repo, tmp_path).changed_since("--output=/tmp/x")


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "url",
        [
            "ext::sh -c 'touch /tmp/pwned'",
            "--upload-pack=touch /tmp/pwned",
            "file:///etc",
            "http://git.example/repo.git",
            "git://git.example/repo.git",
            "",
        ],
    )
    def test_only_https_and_ssh_locations_are_accepted(self, url):
        with pytest.raises(ConfigError):
            GitKnowledge(url)

    def test_a_local_path_is_accepted_only_in_demo_mode(self, repo):
        with pytest.raises(ConfigError):
            GitKnowledge(str(repo))

    @pytest.mark.parametrize("ref", ["-main", "a..b", "main;id", ""])
    def test_branch_names_are_checked(self, ref):
        with pytest.raises(ConfigError):
            GitKnowledge("https://git.example/repo.git", ref=ref)

    @pytest.mark.parametrize("path", ["../other", "sops/../../x", "sops;id"])
    def test_paths_are_checked(self, path):
        with pytest.raises(ConfigError):
            GitKnowledge("https://git.example/repo.git", path=path)


class TestToken:
    async def test_the_token_goes_through_the_environment_never_the_command_line(
        self, tmp_path, monkeypatch
    ):
        seen = []

        async def fake_exec(*argv, **kwargs):
            seen.append((argv, kwargs["env"]))
            raise FileNotFoundError("stop here")

        monkeypatch.setattr(
            "src.l1_agent.connectors.knowledge.git.asyncio.create_subprocess_exec", fake_exec
        )
        src = GitKnowledge(
            "https://git.example/sops.git",
            cache_dir=str(tmp_path),
            secrets=LiteralSecrets({"token": "git-token-value"}),
        )
        with pytest.raises(FileNotFoundError):
            await src.changed_since("")
        argv, env = seen[0]
        assert all("git-token-value" not in part for part in argv)
        assert "Authorization: Bearer git-token-value" in env.values()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "protocol.ext.allow" in env.values() and "never" in env.values()
