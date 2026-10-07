#!/bin/sh
# git_sync コンテナのエントリポイント
#  - SSH 鍵の準備 (/ssh のマウント or 環境変数 GIT_SSH_PRIVATE_KEY)
#  - GIT_SYNC_INTERVAL に応じて 1 回実行 / 定期実行
set -eu

log() { echo "[entrypoint] $*"; }

# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------
SSH_HOME="${HOME:-/root}/.ssh"
mkdir -p "$SSH_HOME"
chmod 700 "$SSH_HOME"

# 1) ホストの .ssh を /ssh に読み取り専用でマウントした場合
#    (Windows からのマウントはパーミッションが 777 になり ssh に拒否されるため、
#     コピーしてから権限を直す)
if [ -d /ssh ] && [ "$(ls -A /ssh 2>/dev/null)" ]; then
  cp -R /ssh/. "$SSH_HOME/"
  log "SSH 設定を /ssh から読み込みました"
fi

# 2) 秘密鍵の中身を環境変数で渡した場合
if [ -n "${GIT_SSH_PRIVATE_KEY:-}" ]; then
  printf '%s\n' "$GIT_SSH_PRIVATE_KEY" | tr -d '\r' > "$SSH_HOME/id_git_sync"
  log "SSH 秘密鍵を環境変数 GIT_SSH_PRIVATE_KEY から読み込みました"
fi

find "$SSH_HOME" -type f -exec chmod 600 {} +
# Windows で作られた config / known_hosts の CRLF を除去
for f in "$SSH_HOME/config" "$SSH_HOME/known_hosts"; do
  [ -f "$f" ] && sed -i 's/\r$//' "$f"
done

# ssh コマンドの組み立て
#   GIT_SYNC_SSH_STRICT: accept-new(既定。初回接続のホスト鍵を自動登録) / yes / no
STRICT="${GIT_SYNC_SSH_STRICT:-accept-new}"
KNOWN_HOSTS="${GIT_SYNC_KNOWN_HOSTS:-/data/known_hosts}"
mkdir -p "$(dirname "$KNOWN_HOSTS")"
touch "$KNOWN_HOSTS"
SSH_CMD="ssh -o StrictHostKeyChecking=$STRICT -o UserKnownHostsFile=$KNOWN_HOSTS"
[ -f "$SSH_HOME/known_hosts" ] && SSH_CMD="ssh -o StrictHostKeyChecking=$STRICT -o UserKnownHostsFile=\"$SSH_HOME/known_hosts $KNOWN_HOSTS\""
[ -f "$SSH_HOME/id_git_sync" ] && SSH_CMD="$SSH_CMD -i $SSH_HOME/id_git_sync"
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-$SSH_CMD}"

# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------
INTERVAL="${GIT_SYNC_INTERVAL:-0}"

# 引数が git_sync.py のオプション以外 (sh など) ならそのまま実行 (デバッグ用)
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
  exec "$@"
fi

if [ "$INTERVAL" = "0" ]; then
  exec python /app/git_sync.py "$@"
fi

log "${INTERVAL} 秒ごとに同期します (停止: docker stop)"
while :; do
  rc=0
  python /app/git_sync.py "$@" || rc=$?
  [ "$rc" -ne 0 ] && log "git_sync.py が終了コード $rc で終了しました"
  sleep "$INTERVAL" &
  wait $!
done
