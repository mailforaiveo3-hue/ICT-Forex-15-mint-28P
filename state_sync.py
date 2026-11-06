"""Persist the SQLite state on an orphan git branch ('state') with optimistic locking.

* restore(): download state.db.gz from the branch (or report 'no state yet'); remembers the branch SHA.
* push():   snapshot the DB (SQLite online-backup API => consistent), gzip, commit as a single orphan
            commit and push with --force-with-lease=<remembered SHA>. If another run pushed in between,
            the push is REJECTED (StateSyncError) instead of silently overwriting its state.
CLI:  python state_sync.py restore | push
"""
import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from logger import get_logger, redact

log = get_logger()
BRANCH = "state"


class StateSyncError(Exception):
    pass


class StateSync:
    def __init__(self, repo_url, db_path, branch=BRANCH, sha_file=".state_sha", runner=None):
        self.repo, self.db_path, self.branch, self.sha_file = repo_url, db_path, branch, sha_file
        self.run = runner or subprocess.run

    @staticmethod
    def from_env():
        url = os.environ.get("STATE_REPO_URL")
        if not url:
            tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
            if not (tok and repo):
                raise StateSyncError("need STATE_REPO_URL or GITHUB_TOKEN + GITHUB_REPOSITORY")
            url = f"https://x-access-token:{tok}@github.com/{repo}.git"
        return StateSync(url, os.environ.get("STATE_DB_PATH", "state/state.db"),
                         sha_file=os.environ.get("STATE_SHA_FILE", ".state_sha"))

    def _git(self, *args, cwd=None):
        r = self.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if r.returncode != 0:
            raise StateSyncError(redact(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}"))
        return r.stdout

    def remote_sha(self):
        out = self._git("ls-remote", "--heads", self.repo, self.branch).strip()
        return out.split()[0] if out else None

    def _read_sha(self):
        return open(self.sha_file).read().strip() if os.path.exists(self.sha_file) else ""

    def restore(self) -> bool:
        sha = self.remote_sha()
        if not sha:
            open(self.sha_file, "w").write("")
            log.info("state branch not found - first run, starting with empty state")
            return False
        tmp = tempfile.mkdtemp(prefix="po3state_")
        try:
            self._git("clone", "--quiet", "--branch", self.branch, "--single-branch", "--depth", "1", self.repo, tmp)
            gz = os.path.join(tmp, "state.db.gz")
            if not os.path.exists(gz):
                raise StateSyncError("state branch exists but state.db.gz is missing")
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            with gzip.open(gz, "rb") as f, open(self.db_path, "wb") as o:
                shutil.copyfileobj(f, o)
            sqlite3.connect(self.db_path).execute("PRAGMA integrity_check").fetchone()
            open(self.sha_file, "w").write(sha)
            log.info(f"state restored from branch '{self.branch}' ({sha[:8]})")
            return True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def push(self, db=None):
        tmp = tempfile.mkdtemp(prefix="po3push_")
        try:
            snap = os.path.join(tmp, "snap.db")
            if db is not None:
                db.snapshot_to(snap)
            else:
                src = sqlite3.connect(self.db_path); dst = sqlite3.connect(snap)
                src.backup(dst); dst.close(); src.close()
            work = os.path.join(tmp, "repo"); os.makedirs(work)
            with open(snap, "rb") as f, gzip.open(os.path.join(work, "state.db.gz"), "wb", 6) as o:
                shutil.copyfileobj(f, o)
            self._git("init", "--quiet", "-b", self.branch, cwd=work)
            self._git("add", "state.db.gz", cwd=work)
            self._git("-c", "user.name=po3-scanner", "-c", "user.email=po3-scanner@users.noreply.github.com",
                      "commit", "--quiet", "-m", f"state {time.time_ns()}", cwd=work)
            lease = f"--force-with-lease=refs/heads/{self.branch}:{self._read_sha()}"
            try:
                self._git("push", "--quiet", lease, self.repo, f"HEAD:refs/heads/{self.branch}", cwd=work)
            except StateSyncError as e:
                raise StateSyncError(f"state push rejected (another run changed the state branch?): {e}")
            new = self._git("rev-parse", "HEAD", cwd=work).strip()
            open(self.sha_file, "w").write(new)
            log.info(f"state pushed ({new[:8]})")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if len(argv) != 2 or argv[1] not in ("restore", "push"):
        print(__doc__); return 2
    s = StateSync.from_env()
    try:
        s.restore() if argv[1] == "restore" else s.push()
    except StateSyncError as e:
        log.error(str(e)); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
