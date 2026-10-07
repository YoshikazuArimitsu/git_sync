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
  5. 前回同期時の状態を保存し、次回以降「マージ済みで、どこかのリモートで
     削除されたブランチ」を検知したら、全リモートから削除する

使い方:
  python git_sync.py                       # 同じフォルダの git_sync.json を使用
  python git_sync.py -c other.json         # 設定ファイルを指定
  python git_sync.py --dry-run             # push せずに何が起きるかだけ表示
  python git_sync.py --branch main --branch develop   # 対象ブランチを限定

  設定は 設定ファイル → 環境変数 GIT_SYNC_CONFIG_JSON → 個別の環境変数
  (GIT_SYNC_REMOTES など) の順に読み込み、後のものが優先される。
  URL 中の ${VAR} は環境変数で置き換えられる (トークンをファイルに書かないため)。
"""

import argparse
import datetime
import fnmatch
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.environ.get("GIT_SYNC_CONFIG") or os.path.join(SCRIPT_DIR, "git_sync.json")

# URL 中の認証情報 (https://user:token@host) をログに出さないためのマスク
_CRED_RE = re.compile(r"(https?://)[^/@\s]+@")


def mask(text):
    return _CRED_RE.sub(r"\1***@", text)


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
        line = mask(str(msg))
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
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return None
    v = v.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    sys.exit(f"設定エラー: 環境変数 {name} は true/false で指定してください: {v}")


def _env_list(name):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return None
    return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]


def _parse_remotes_env(text):
    """GIT_SYNC_REMOTES="name1=url1,name2=url2" (カンマ・改行・空白区切り)"""
    remotes = []
    for item in re.split(r"[,\s]+", text.strip()):
        if not item:
            continue
        if "=" not in item:
            sys.exit(f"設定エラー: GIT_SYNC_REMOTES は name=url 形式で指定してください: {item}")
        name, url = item.split("=", 1)
        remotes.append({"name": name.strip(), "url": url.strip()})
    return remotes


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(text, where):
    """${VAR} を環境変数で置換する。未定義ならエラー (トークン入れ忘れ防止)"""
    def repl(m):
        v = os.environ.get(m.group(1))
        if not v:
            sys.exit(f"設定エラー: {where} で参照している環境変数 {m.group(1)} が未定義(または空)です。")
        return v
    return _VAR_RE.sub(repl, text)


def load_config(path):
    """
    設定は次の順に読み込み、後のものほど優先する:
      1. 設定ファイル (-c / 環境変数 GIT_SYNC_CONFIG / 既定 git_sync.json)
      2. 環境変数 GIT_SYNC_CONFIG_JSON (JSON 文字列。ファイル内容を丸ごと渡す用)
      3. 個別の環境変数 GIT_SYNC_REMOTES, GIT_SYNC_DRY_RUN など
    """
    cfg = {}
    sources = []
    base_dir = os.getcwd()

    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        sources.append(os.path.abspath(path))
        base_dir = os.path.dirname(os.path.abspath(path))

    env_json = os.environ.get("GIT_SYNC_CONFIG_JSON")
    if env_json and env_json.strip():
        try:
            extra = json.loads(env_json)
        except json.JSONDecodeError as e:
            sys.exit(f"設定エラー: GIT_SYNC_CONFIG_JSON が JSON として不正です: {e}")
        cfg.update(extra)
        sources.append("GIT_SYNC_CONFIG_JSON")

    env_over = {}
    if os.environ.get("GIT_SYNC_REMOTES", "").strip():
        cfg["remotes"] = _parse_remotes_env(os.environ["GIT_SYNC_REMOTES"])
        env_over["remotes"] = True
    for key, env in [("work_dir", "GIT_SYNC_WORK_DIR"), ("log_file", "GIT_SYNC_LOG_FILE"),
                     ("on_conflict", "GIT_SYNC_ON_CONFLICT")]:
        if os.environ.get(env, "").strip():
            cfg[key] = os.environ[env].strip()
            env_over[key] = True
    for key, env in [("sync_tags", "GIT_SYNC_SYNC_TAGS"), ("dry_run", "GIT_SYNC_DRY_RUN")]:
        v = _env_bool(env)
        if v is not None:
            cfg[key] = v
            env_over[key] = True
    for key, env in [("include_branches", "GIT_SYNC_INCLUDE_BRANCHES"),
                     ("exclude_branches", "GIT_SYNC_EXCLUDE_BRANCHES")]:
        v = _env_list(env)
        if v is not None:
            cfg[key] = v
            env_over[key] = True
    if any(os.environ.get(e, "").strip() for e in
           ("GIT_SYNC_DELETE_MERGED", "GIT_SYNC_REQUIRE_MERGED")):
        d = cfg.get("delete_merged_branches")
        d = dict(d) if isinstance(d, dict) else ({"enabled": bool(d)} if d is not None else {})
        v = _env_bool("GIT_SYNC_DELETE_MERGED")
        if v is not None:
            d["enabled"] = v
        v = _env_bool("GIT_SYNC_REQUIRE_MERGED")
        if v is not None:
            d["require_merged"] = v
        cfg["delete_merged_branches"] = d
        env_over["delete_merged_branches"] = True
    name = os.environ.get("GIT_SYNC_MERGE_NAME", "").strip()
    email = os.environ.get("GIT_SYNC_MERGE_EMAIL", "").strip()
    if name or email:
        mi = dict(cfg.get("merge_identity") or {})
        if name:
            mi["name"] = name
        if email:
            mi["email"] = email
        cfg["merge_identity"] = mi
        env_over["merge_identity"] = True
    if env_over:
        sources.append("環境変数(" + ", ".join(env_over) + ")")

    if not sources or "remotes" not in cfg:
        sys.exit(f"設定が見つかりません (リモートの指定がありません): {path}\n"
                 f"設定ファイルを用意するか、環境変数 GIT_SYNC_CONFIG_JSON / "
                 f"GIT_SYNC_REMOTES で設定を渡してください。")
    cfg["_sources"] = sources

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
        r["url"] = expand_env(r["url"], f"remotes[{r['name']}].url")

    # 相対パスは「設定ファイルのフォルダ」(ファイルが無ければカレント) 基準
    work_dir = cfg.get("work_dir", "./work_repo")
    if not os.path.isabs(work_dir):
        work_dir = os.path.join(base_dir, work_dir)
    cfg["work_dir"] = os.path.normpath(work_dir)

    log_file = cfg.get("log_file")
    if isinstance(log_file, str) and log_file.strip().lower() in ("", "-", "none", "off"):
        log_file = None   # 画面 (標準出力) のみ
    if log_file and not os.path.isabs(log_file):
        log_file = os.path.join(base_dir, log_file)
    cfg["log_file"] = log_file

    cfg.setdefault("include_branches", ["*"])
    cfg.setdefault("exclude_branches", [])
    cfg.setdefault("sync_tags", False)
    cfg.setdefault("on_conflict", "skip")      # skip | prefer:<remote名>
    cfg.setdefault("merge_identity", {"name": "git-sync", "email": "git-sync@localhost"})

    d = cfg.get("delete_merged_branches")
    if not isinstance(d, dict):
        d = {"enabled": bool(d) if d is not None else True}
    d.setdefault("enabled", True)
    d.setdefault("require_merged", True)
    d.setdefault("merged_into", ["main", "master", "develop"])
    d.setdefault("protected_branches", ["main", "master", "develop", "release/*"])
    cfg["delete_merged_branches"] = d
    return cfg


# ----------------------------------------------------------------------------
# 前回同期状態 (削除検知用)
#   work_dir/.git/git_sync_state.json に {branch: sha} を保存する。
#   .git 内に置くので、作業ツリーの掃除 (git clean) で消えない。
# ----------------------------------------------------------------------------
def state_path(cfg):
    return os.path.join(cfg["work_dir"], ".git", "git_sync_state.json")


def load_state(cfg):
    p = state_path(cfg)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("remotes") != sorted(r["name"] for r in cfg["remotes"]):
            # リモート構成が変わったら前回状態は信用しない (誤削除防止)
            log("リモート構成が前回と異なるため、今回は削除検知を行いません。")
            return None
        return data.get("branches", {})
    except Exception as e:  # 壊れていたら削除検知しない
        log(f"状態ファイルを読めません ({e})。今回は削除検知を行いません。")
        return None


def save_state(cfg, branches):
    p = state_path(cfg)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.datetime.now().isoformat(timespec="seconds"),
                   "remotes": sorted(r["name"] for r in cfg["remotes"]),
                   "branches": branches}, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, p)


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
# 削除検知
# ----------------------------------------------------------------------------
def match_any(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def check_deletion(git, branch, heads, order, last_sha, per_remote, dcfg):
    """
    戻り値: ("delete", 説明) / ("keep", 理由) / (None, None)=削除ではない
    """
    if last_sha is None or len(heads) == len(order):
        return None, None          # 前回未同期 or 全リモートに存在 → 削除ではない
    missing = [r for r in order if r not in heads]

    if match_any(branch, dcfg["protected_branches"]) or branch in dcfg["merged_into"]:
        return "keep", f"保護ブランチのため復元 (削除元: {', '.join(missing)})"

    moved = [r for r, s in heads.items() if s != last_sha]
    if moved:
        return "keep", (f"{', '.join(missing)} で削除されたが {', '.join(moved)} で"
                        f"新しいコミットがあるため復元")

    if dcfg["require_merged"]:
        merged_to = None
        for target in dcfg["merged_into"]:
            if target == branch:
                continue
            for r in order:
                tip = per_remote[r].get(target)
                if tip and git.is_ancestor(last_sha, tip):
                    merged_to = f"{r}/{target}"
                    break
            if merged_to:
                break
        if not merged_to:
            return "keep", (f"{', '.join(missing)} で削除されたが "
                            f"{'/'.join(dcfg['merged_into'])} に未マージのため復元")
        return "delete", f"{', '.join(missing)} で削除 & {merged_to} にマージ済み"
    return "delete", f"{', '.join(missing)} で削除"


def delete_branch(git, remote, branch, sha, dry_run):
    if dry_run:
        return True, "(dry-run)"
    # 削除直前に誰かが push していたら消さない (force-with-lease)
    p = git.run("push", "--porcelain", f"--force-with-lease=refs/heads/{branch}:{sha}",
                remote, f":refs/heads/{branch}", check=False)
    if p.returncode != 0:
        return False, (p.stderr.strip().splitlines() or ["削除失敗"])[-1]
    return True, "削除"


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
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                    help="設定ファイル (JSON)。環境変数 GIT_SYNC_CONFIG でも指定可")
    ap.add_argument("-n", "--dry-run", action="store_true", help="push せずに結果だけ表示")
    ap.add_argument("-b", "--branch", action="append", help="対象ブランチを限定 (複数可)")
    ap.add_argument("-v", "--verbose", action="store_true", help="実行する git コマンドを表示")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = Logger(cfg["log_file"])
    dry_run = args.dry_run or cfg.get("dry_run", False)

    log("=" * 70)
    log(f"git-sync 開始  config={' + '.join(cfg['_sources'])}"
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

    dcfg = cfg["delete_merged_branches"]
    last_state = load_state(cfg) if dcfg["enabled"] else None
    if dcfg["enabled"] and last_state is None and not os.path.isfile(state_path(cfg)):
        log("初回実行のため削除検知は次回から有効になります。")
    new_state = {}
    # 今回の対象外ブランチの前回状態は引き継ぐ (-b 指定時など)
    if last_state:
        new_state = {b: s for b, s in last_state.items() if b not in targets}

    stats = {"unchanged": 0, "updated": 0, "conflict": 0, "push_error": 0, "deleted": 0}
    problems = []

    for br in targets:
        heads = {r: per_remote[r][br] for r in order if br in per_remote[r]}

        if last_state is not None:
            verdict, why = check_deletion(git, br, heads, order, last_state.get(br),
                                          per_remote, dcfg)
            if verdict == "delete":
                log(f"[DELETE]   {br}: {why}")
                all_ok = True
                for r in order:
                    if r not in heads:
                        continue
                    ok, msg = delete_branch(git, r, br, heads[r], dry_run)
                    log(f"             - {r}: {heads[r][:8]} {msg}")
                    if not ok:
                        all_ok = False
                        stats["push_error"] += 1
                        problems.append(f"{br} → {r}: 削除失敗 ({msg})")
                if all_ok:
                    stats["deleted"] += 1
                else:
                    new_state[br] = last_state[br]   # 次回再試行
                continue
            if verdict == "keep":
                log(f"[RESTORE]  {br}: {why}")

        sha, how = integrate_branch(git, br, heads, order, cfg)
        if sha is None:
            stats["conflict"] += 1
            detail = ", ".join(f"{r}={heads[r][:8]}" for r in heads)
            log(f"[CONFLICT] {br}: 自動マージできません ({detail}) → スキップ")
            problems.append(f"{br}: コンフリクト ({detail})")
            if last_state and br in last_state:
                new_state[br] = last_state[br]
            continue

        need = [r for r in order if per_remote[r].get(br) != sha]
        if not need:
            stats["unchanged"] += 1
            new_state[br] = sha
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
        if not any(p.startswith(f"{br} → ") for p in problems):
            new_state[br] = sha
        elif last_state and br in last_state:
            new_state[br] = last_state[br]

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
    if dcfg["enabled"] and not dry_run:
        save_state(cfg, new_state)

    log(f"結果: 一致 {stats['unchanged']} / 更新 {stats['updated']} / "
        f"削除 {stats['deleted']} / "
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
