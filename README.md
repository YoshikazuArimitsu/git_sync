# git_sync

複数のリモートリポジトリのブランチを相互に同期し、**全ブランチが全リモートに同じ状態で存在する**ようにするスクリプトです。
Python 3.8 以上と git があれば動きます（追加ライブラリ不要）。

## ファイル構成

| ファイル | 内容 |
|---|---|
| `git_sync.py` | 同期スクリプト本体 |
| `git_sync.sample.json` | 設定ファイルのサンプル |
| `git_sync.bat` | Windows 用の起動バッチ（ダブルクリック／タスクスケジューラ用） |
| `Dockerfile` / `entrypoint.sh` | Docker イメージ |
| `docker-compose.yml` / `.env.sample` | docker compose 用の定義と環境変数サンプル |

## セットアップ

1. `git_sync.sample.json` をコピーして `git_sync.json` を作る
2. `remotes` に同期したいリポジトリを 2 つ以上書く
3. 各リモートへ **パスワード入力なしで** fetch / push できるようにしておく
   （SSH 鍵、または Git Credential Manager にトークンを保存）
4. まず `--dry-run` で確認してから本番実行

```
python git_sync.py --dry-run
python git_sync.py
```

## Docker で動かす

### いちばん簡単な方法（docker compose）

```
copy git_sync.sample.json git_sync.json   # 設定を書く
copy .env.sample .env                     # 必要なら SSH 鍵の場所やトークンを書く
docker compose up -d --build              # 10 分ごとに同期（常駐）
docker compose logs -f                    # ログを見る
```

1 回だけ実行したいとき：

```
docker compose run --rm -e GIT_SYNC_INTERVAL=0 git-sync            # 本番
docker compose run --rm -e GIT_SYNC_INTERVAL=0 git-sync --dry-run  # 確認だけ
```

`git_sync.py` のオプション（`--dry-run`、`-b` など）は、そのままコンテナの引数として渡せます。

### コンテナ内のパス

| パス | 内容 | 渡し方 |
|---|---|---|
| `/config/git_sync.json` | 設定ファイル | 読み取り専用でマウント |
| `/ssh` | SSH 鍵・`config`・`known_hosts` | 読み取り専用でマウント（中でコピーして権限を直すので、Windows の `.ssh` をそのままマウントして OK） |
| `/data` | 作業リポジトリ・同期状態・ログ・known_hosts | **ボリュームで永続化**（消すと削除検知が初回扱いに戻る） |

コンテナ内では `work_dir` は `/data/work_repo`、`log_file` は `/data/logs/git_sync.log` に固定されます（設定ファイルの値より優先）。

### 設定の渡し方（3通り・組み合わせ可）

後に書いたものほど優先されます。

**1. 設定ファイルをマウントする**（おすすめ）

```
docker run --rm -v %CD%\git_sync.json:/config/git_sync.json:ro -v %USERPROFILE%\.ssh:/ssh:ro -v git-sync-data:/data git-sync
```

置き場所を変えたいときは `GIT_SYNC_CONFIG=/path/to/file.json` で指定できます。

**2. 設定 JSON を環境変数でまるごと渡す**（CI や Kubernetes の Secret 向け）

```
docker run --rm -e GIT_SYNC_CONFIG_JSON="{\"remotes\":[...]}" ... git-sync
```

ファイルと併用した場合は、JSON に書いたキーだけが上書きされます。

**3. 個別の環境変数で渡す・上書きする**

| 環境変数 | 対応する設定 | 例 |
|---|---|---|
| `GIT_SYNC_REMOTES` | `remotes` | `origin=git@github.com:you/a.git,sub=git@github.com:you/b.git` |
| `GIT_SYNC_DRY_RUN` | `dry_run` | `true` |
| `GIT_SYNC_ON_CONFLICT` | `on_conflict` | `prefer:origin` |
| `GIT_SYNC_SYNC_TAGS` | `sync_tags` | `true` |
| `GIT_SYNC_INCLUDE_BRANCHES` / `GIT_SYNC_EXCLUDE_BRANCHES` | `include_branches` / `exclude_branches` | `feature/*,tmp/*` |
| `GIT_SYNC_DELETE_MERGED` / `GIT_SYNC_REQUIRE_MERGED` | `delete_merged_branches.enabled` / `.require_merged` | `false` |
| `GIT_SYNC_MERGE_NAME` / `GIT_SYNC_MERGE_EMAIL` | `merge_identity` | |
| `GIT_SYNC_WORK_DIR` / `GIT_SYNC_LOG_FILE` | `work_dir` / `log_file`（`none` でファイル出力なし） | |

`GIT_SYNC_REMOTES` だけ渡せば、設定ファイル無しでも動きます。

### コンテナの動作を変える環境変数

| 環境変数 | 説明 | 既定値 |
|---|---|---|
| `GIT_SYNC_INTERVAL` | 同期の間隔（秒）。`0` で 1 回実行して終了 | `0`（compose では `600`） |
| `GIT_SSH_PRIVATE_KEY` | SSH 秘密鍵の**中身**（`/ssh` をマウントしない場合） | なし |
| `GIT_SYNC_SSH_STRICT` | ホスト鍵の確認：`accept-new`（初回は自動登録）/ `yes` / `no` | `accept-new` |
| `TZ` | ログの時刻のタイムゾーン | `Asia/Tokyo` |

### 認証情報（トークン）の扱い

設定ファイルの URL には `${環境変数名}` と書けます。実行時に環境変数の値に置き換わるので、トークンを設定ファイルに書かずに済みます。

```json
{ "name": "github", "url": "https://x-access-token:${GITHUB_TOKEN}@github.com/you/repo.git" }
```

`.env` に `GITHUB_TOKEN=...` と書いておけば compose から渡されます。ログ上では `https://***@github.com/...` と伏せ字になります。未定義の変数を参照していると、実行前にエラーで止まります。

> ※ `/ssh` にマウントした `config` に `IdentityFile C:\Users\...` のような Windows のパスが書かれていると、コンテナ内では見つかりません。`~/.ssh/id_ed25519` のような書き方にしてください。

## 設定項目

| キー | 説明 | 既定値 |
|---|---|---|
| `work_dir` | 統合に使う作業用ローカルリポジトリ（自動作成） | `./work_repo` |
| `log_file` | ログの追記先。省略で画面出力のみ | なし |
| `remotes` | `{ "name": 識別名, "url": URL }` の配列。**並び順が統合時の優先順** | 必須 |
| `include_branches` | 対象ブランチ（ワイルドカード可） | `["*"]` |
| `exclude_branches` | 除外ブランチ（ワイルドカード可） | `[]` |
| `sync_tags` | タグも全リモートへ push するか | `false` |
| `on_conflict` | `skip`：コンフリクトしたブランチは触らず報告 / `prefer:<name>`：衝突箇所はそのリモート側を採用してマージ | `skip` |
| `merge_identity` | マージコミットの作成者名・メール | `git-sync` |
| `dry_run` | `true` なら常に push しない | `false` |
| `delete_merged_branches` | マージ済みブランチの削除同期（下記参照） | 有効 |

## 同期のルール

各ブランチについて、全リモートの先端コミットを比べて統合します。

- **全リモートで一致** → 何もしない
- **一部のリモートにしか無い** → 無いリモートへそのまま作成
- **片方がもう片方を含む（fast-forward）** → 新しい方に揃える
- **履歴が分岐している** → マージコミット `git-sync: merge <branch> from <remote>` を作って全リモートへ push
- **マージでコンフリクト** → `on_conflict` に従う（既定はスキップして報告）

push は強制 push をしないため、リモートの履歴を巻き戻したり消したりすることはありません。
同期中に誰かが push して先に進んだ場合、そのリモートへの push は拒否され「push失敗」として報告されます（次回実行で再統合されます）。

## マージ済みブランチの削除同期

どこかのリモートでブランチが削除されたとき、次の **すべて** を満たせば、残っている全リモートからも削除します。

1. 前回の同期で全リモートに揃っていたブランチである
2. 残っているリモートで、前回から新しいコミットが積まれていない
3. 保護ブランチ（`protected_branches` / `merged_into`）ではない
4. `merged_into` のいずれかのブランチにマージ済みである（`require_merged: true` のとき）

条件を満たさない場合は削除とみなさず、消えたリモートへ**復元**してログに `[RESTORE]` と理由を出します。
削除時は `--force-with-lease` を使うため、直前に誰かが push したブランチは消しません。

```json
"delete_merged_branches": {
  "enabled": true,
  "require_merged": true,
  "merged_into": ["main", "master", "develop"],
  "protected_branches": ["main", "master", "develop", "release/*"]
}
```

| キー | 説明 |
|---|---|
| `enabled` | `false` で削除同期を無効化（削除されたブランチは常に復元） |
| `require_merged` | `true`：マージ済みのものだけ削除 / `false`：未マージでも削除 |
| `merged_into` | 「マージ済み」の判定先ブランチ。ここに書いたブランチ自体は削除しない |
| `protected_branches` | 絶対に削除しないブランチ（ワイルドカード可） |

> **Squash merge / Rebase merge について：** GitHub などで「Squash and merge」「Rebase and merge」を使うと、元ブランチのコミットが `main` に残らないため「未マージ」と判定され、削除されずに復元されます。これらの運用の場合は `require_merged` を `false` にしてください（その場合も条件 1〜3 は適用されます）。

## コマンドラインオプション

```
python git_sync.py [-c 設定ファイル] [-n] [-b ブランチ ...] [-v]
  -c, --config   設定ファイルを指定（既定: スクリプトと同じフォルダの git_sync.json）
  -n, --dry-run  push せずに、何が起きるかだけ表示
  -b, --branch   対象ブランチを限定（複数指定可）
  -v, --verbose  実行する git コマンドを表示
```

終了コード: `0` 正常 / `1` コンフリクトや push・削除の失敗あり / `2` fetch 失敗で中止

## 注意点

- 削除検知は「前回の同期状態」と比べて行うため、**初回実行では動きません**（2回目から有効）。状態は `work_dir/.git/git_sync_state.json` に保存されます。リモート構成を変えた直後の1回も削除検知を行いません。
- fetch できないリモートが 1 つでもあると、誤って「ブランチが無い」と判断しないよう、全体を中止します。
- `work_dir` はスクリプト専用です。中で手作業をしないでください（実行時に作業ツリーをリセットします）。
- 定期実行する場合は Windows のタスクスケジューラで `git_sync.bat` を登録するか、Docker の常駐モード（`GIT_SYNC_INTERVAL`）を使ってください。
- **Windows ローカル実行と Docker を同じリモートに対して同時に動かさないでください。** 作業リポジトリと同期状態が別々になるため、片方が削除したブランチをもう片方が復元するなど、動作が干渉します。
