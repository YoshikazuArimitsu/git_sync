#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
git_sync.py - 複数のリモートリポジトリ間でブランチを相互同期するスクリプト

処理の流れ:
  1. 設定ファイル (JSON) からリモートリポジトリの一覧を読み込む
  2. 作業用ローカルリポジトリに全リモートを登録し、fetch する
  3. 全リモートのブランチの和集合を求め、ブランチごとに統合する
       - 全リモートで同じコミット        → 何もしない
       - 一方が他方の祖先 (fast-forward) → 新しい方を採用
       - 履歴が分岐している             → マージコミットを作成
       - マージでコンフリクト            → そのブランチはスキップして報告
  4. 統合結果を全リモートへ push する (強制 push はしない)

使い方:
  python git_sync.py                       # 同じフォルダの git_sync.json を使用
  python git_sync.py -c other.json         # 設定ファイルを指定
  python git_sync.py --dry-run             # push せずに何が起きるかだけ表示
  python git_sync.py --branch main --branch develop   # 対象ブランチを限定
"""

import argparse
import datetime
import fnmatch
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "git_sync.json")


# ----------------------------------------------------------------------------
# ログ
# ----------------------------------------------------------------------------
class Logger:
    def __init__(self, log_file=None):
        self.fp = None
        if log_file:
            d = os.path.dirname(os.path.abspath(log_file))
            os.makedirs(d, exist_ok=True)
            self.fp = open(log_file, "a", encoding="utf-8")

    def __call__(self, msg=""):
        line = msg
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # Windows コンソールの文字コード対策
            print(line.encode("cp932", "replace").decode("cp932"), flush=True)
        if self.fp:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.fp.write(f"[{ts}] {line}\n")
            self.fp.flush()

    def close(self):
        if self.fp:
            self.fp.close()


log = Logger()


# ----------------------------------------------------------------------------
# git 実行ヘルパー
# ----------------------------------------------------------------------------
class GitError(Exception):
    def __init__(self, args, code, out, err):
        super().__init__(f"git {' '.join(args)} failed ({code}): {err.strip() or out.strip()}")
        self.code = code
        self.out = out
        self.err = err


class Git:
    def __init__(self, repo_dir, identity=None, verbose=False):
        self.repo_dir = repo_dir
        self.verbose = verbose
        self.base = ["git", "-C", repo_dir,
                     "-c", "core.autocrlf=false",
                     "-c", "core.quotepath=false",
                     "-c", "advice.detachedHead=false"]
        if identity:
            if identity.get("name"):
                self.base += ["-c", f"user.name={identity['name']}"]
            if identity.get("email"):
                self.base += ["-c", f"user.email={identity['email']}"]

    def run(self, *args, check=True):
        cmd = self.base + list(args)
        if self.verbose:
            log("    $ git " + " ".join(args))
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, encoding="utf-8", errors="replace")
        if check and p.returncode != 0:
            raise GitError(args, p.returncode, p.stdout, p.stderr)
        return p

    def out(self, *args):
        return self.run(*args).stdout.strip()

    def ok(self, *args):
        return self.run(*args, check=False).returncode == 0

    def is_ancestor(self, a, b):
        """a が b の祖先 (または同一) なら True"""
        return self.ok("merge-base", "--is-ancestor", a, b)


# ----------------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------------
def load_config(path):
    if not os.path.isfile(path):
        sys.exit(f"設定ファイルが見つかりません: {path}\n"
                 f"git_sync.sample.json をコピーして作成してください。")
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)

    remotes = cfg.get("remotes") or []
    if len(remotes) < 2:
        sys.exit("設定エラー: remotes には 2 つ以上のリモートを指定してください。")
    names = set()
    for r in remotes:
        if not r.get("name") or not r.get("url"):
            sys.exit(f"設定エラー: remotes の各要素には name と url が必要です: {r}")
        if r["name"] in names:
            sys.exit(f"設定エラー: リモート名が重複しています: {r['name']}")
        names.add(r["name"])

    cfg_dir = os.path.dirname(os.path.abspath(path))
    work_dir = cfg.get("work_dir", "./work_repo")
    if not os.path.isabs(work_dir):
        work_dir = os.path.join(cfg_dir, work_dir)
    cfg["work_dir"] = os.path.normpath(work_dir)

    log_file = cfg.get("log_file")
    if log_file and not os.path.isabs(log_file):
        log_file = os.path.join(cfg_dir, log_file)
    cfg["log_file"] = log_file

    cfg.setdefault("include_branches", ["*"])
    cfg.setdefault("exclude_branches", [])
    cfg.setdefault("sync_tags", False)
    cfg.setdefault("on_conflict", "skip")      # skip | prefer:<remote名>
    cfg.setdefault("merge_identity", {"name": "git-sync", "email": "git-sync@localhost"})
    return cfg


# ----------------------------------------------------------------------------
# 準備: 作業リポジトリ作成・リモート登録・fetch
# ----------------------------------------------------------------------------
def prepare_repo(cfg, git):
    wd = cfg["work_dir"]
    if not os.path.isdir(os.path.join(wd, ".git")):
        log(f"作業リポジトリを作成します: {wd}")
        os.makedirs(wd, exist_ok=True)
        subprocess.run(["git", "init", "-q", wd], check=True)

    existing = set(git.out("remote").split())
    wanted = {r["name"]: r["url"] for r in cfg["remotes"]}
    for name, url in wanted.items():
        if name in existing:
            git.run("remote", "set-url", name, url)
        else:
            git.run("remote", "add", name, url)
    # 設定から外れたリモートは削除
    for name in existing - set(wanted):
        log(f"設定に無いリモートを削除します: {name}")
        git.run("remote", "remove", name)

    # 前回の中断などで作業ツリーが汚れていたら掃除
    git.run("merge", "--abort", check=False)
    git.run("reset", "--hard", "-q", check=False)
    git.run("clean", "-fdq", check=False)

    failed = []
    for r in cfg["remotes"]:
        log(f"fetch: {r['name']} ({r['url']})")
        p = git.run("fetch", "--prune", "--no-tags" if not cfg["sync_tags"] else "--tags",
                    r["name"], check=False)
        if p.returncode != 0:
            log(f"  !! fetch 失敗: {p.stderr.strip()}")
            failed.append(r["name"])
    return failed


def remote_branches(git, remote):
    """{branch名: sha} を返す"""
    out = git.out("for-each-ref", "--format=%(refname)%09%(objectname)",
                  f"refs/remotes/{remote}/")
    result = {}
    prefix = f"refs/remotes/{remote}/"
    for line in out.splitlines():
        if not line.strip():
            continue
        ref, sha = line.split("\t")
        br = ref[len(prefix):]
        if br == "HEAD":
            continue
        result[br] = sha
    return result


def branch_selected(branch, cfg, only):
    if only:
        return branch in only
    if not any(fnmatch.fnmatchcase(branch, p) for p in cfg["include_branches"]):
        return False
    if any(fnmatch.fnmatchcase(branch, p) for p in cfg["exclude_branches"]):
        return False
    return True


# ----------------------------------------------------------------------------
# ブランチ統合
# ----------------------------------------------------------------------------
def integrate_branch(git, branch, heads, order, cfg):
    """
    heads: {remote名: sha}  (そのブランチを持つリモートのみ)
    order: 設定ファイル上のリモートの並び (統合の優先順)
    戻り値: (統合後のsha or None, 説明文)
    """
    shas = [heads[r] for r in order if r in heads]
    uniq = []
    for s in shas:
        if s not in uniq:
            uniq.append(s)

    if len(uniq) == 1:
        return uniq[0], ("同一" if len(heads) == len(order) else "未登録リモートへ追加")

    # まず fast-forward で片付くものを畳み込む
    # (他の全 sha の子孫になっている sha があればそれを採用)
    for cand in uniq:
        if all(git.is_ancestor(o, cand) for o in uniq if o != cand):
            return cand, "fast-forward"

    # 分岐している → マージ
    prefer = None
    if cfg["on_conflict"].startswith("prefer:"):
        prefer = cfg["on_conflict"].split(":", 1)[1]

    git.run("checkout", "-q", "--detach", uniq[0])
    git.run("reset", "--hard", "-q")
    src_names = [r for r in order if r in heads]
    for other in uniq[1:]:
        if git.is_ancestor(other, "HEAD"):
            continue
        if git.is_ancestor("HEAD", other):
            git.run("checkout", "-q", "--detach", other)
            continue
        from_remotes = ", ".join(r for r in src_names if heads[r] == other)
        msg = f"git-sync: merge {branch} from {from_remotes}"
        p = git.run("merge", "--no-ff", "--no-edit", "-m", msg, other, check=False)
        if p.returncode != 0:
            git.run("merge", "--abort", check=False)
            if prefer and prefer in heads:
                # コンフリクト時に優先リモート側の内容を採用 (-X ours/theirs)
                ours_is_prefer = (heads[prefer] != other)
                strategy = "ours" if ours_is_prefer else "theirs"
                p2 = git.run("merge", "--no-ff", "--no-edit", "-X", strategy,
                             "-m", msg + f" (conflicts resolved: prefer {prefer})",
                             other, check=False)
                if p2.returncode == 0:
                    continue
                git.run("merge", "--abort", check=False)
            git.run("reset", "--hard", "-q", check=False)
            return None, "コンフリクト"
    return git.out("rev-parse", "HEAD"), "マージ"


def push_branch(git, remote, branch, sha, dry_run):
    if dry_run:
        return True, "(dry-run)"
    p = git.run("push", "--porcelain", remote, f"{sha}:refs/heads/{branch}", check=False)
    if p.returncode != 0:
        return False, (p.stderr.strip().splitlines() or ["push 失敗"])[-1]
    return True, "ok"


# ----------------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------------
def main():
    global log
    ap = argparse.ArgumentParser(description="複数リモートリポジトリのブランチ相互同期")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="設定ファイル (JSON)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="push せずに結果だけ表示")
    ap.add_argument("-b", "--branch", action="append", help="対象ブランチを限定 (複数可)")
    ap.add_argument("-v", "--verbose", action="store_true", help="実行する git コマンドを表示")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = Logger(cfg["log_file"])
    dry_run = args.dry_run or cfg.get("dry_run", False)

    log("=" * 70)
    log(f"git-sync 開始  config={os.path.abspath(args.config)}"
        + ("  [DRY-RUN]" if dry_run else ""))

    git = Git(cfg["work_dir"], cfg.get("merge_identity"), args.verbose)
    order = [r["name"] for r in cfg["remotes"]]

    fetch_failed = prepare_repo(cfg, git)
    if fetch_failed:
        # 取得できないリモートがあると「ブランチが無い」と誤認して
        # 全ブランチを push してしまうため、安全のため中止する
        log(f"fetch に失敗したリモートがあるため中止します: {', '.join(fetch_failed)}")
        log.close()
        return 2

    per_remote = {r: remote_branches(git, r) for r in order}
    all_branches = sorted(set().union(*[set(b) for b in per_remote.values()]))
    targets = [b for b in all_branches if branch_selected(b, cfg, args.branch)]
    log(f"ブランチ数: 全 {len(all_branches)} / 対象 {len(targets)}")
    log("-" * 70)

    stats = {"unchanged": 0, "updated": 0, "conflict": 0, "push_error": 0}
    problems = []

    for br in targets:
        heads = {r: per_remote[r][br] for r in order if br in per_remote[r]}
        sha, how = integrate_branch(git, br, heads, order, cfg)
        if sha is None:
            stats["conflict"] += 1
            detail = ", ".join(f"{r}={heads[r][:8]}" for r in heads)
            log(f"[CONFLICT] {br}: 自動マージできません ({detail}) → スキップ")
            problems.append(f"{br}: コンフリクト ({detail})")
            continue

        need = [r for r in order if per_remote[r].get(br) != sha]
        if not need:
            stats["unchanged"] += 1
            log(f"[OK]       {br}: 全リモートで一致 ({sha[:8]})")
            continue

        stats["updated"] += 1
        log(f"[SYNC]     {br}: {how} → {sha[:8]}  push先: {', '.join(need)}")
        for r in need:
            before = per_remote[r].get(br)
            ok, msg = push_branch(git, r, br, sha, dry_run)
            state = (before[:8] if before else "(新規)") + f" → {sha[:8]}"
            log(f"             - {r}: {state} {msg}")
            if not ok:
                stats["push_error"] += 1
                problems.append(f"{br} → {r}: push 失敗 ({msg})")

    if cfg["sync_tags"]:
        log("-" * 70)
        for r in order:
            if dry_run:
                log(f"[TAGS]     {r}: (dry-run)")
                continue
            p = git.run("push", "--tags", r, check=False)
            if p.returncode == 0:
                log(f"[TAGS]     {r}: ok")
            else:
                log(f"[TAGS]     {r}: 一部失敗 {p.stderr.strip().splitlines()[-1:]}")
                problems.append(f"tags → {r}: push 失敗")

    # 作業ツリーを空の状態に戻しておく
    git.run("checkout", "-q", "--detach", check=False)

    log("-" * 70)
    log(f"結果: 一致 {stats['unchanged']} / 更新 {stats['updated']} / "
        f"コンフリクト {stats['conflict']} / push失敗 {stats['push_error']}")
    if problems:
        log("要対応:")
        for p in problems:
            log(f"  - {p}")
    log("git-sync 終了")
    log.close()
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
