FROM python:3.12-slim

# git / ssh / タイムゾーン / PID1 用の tini
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates tzdata tini \
 && rm -rf /var/lib/apt/lists/* \
 # ボリュームの所有者がコンテナと異なっても git が拒否しないようにする
 && git config --system --add safe.directory '*'

WORKDIR /app
COPY git_sync.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh /app/git_sync.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Tokyo \
    # 設定ファイルの場所 (ここにマウントする)
    GIT_SYNC_CONFIG=/config/git_sync.json \
    # 作業リポジトリ・同期状態・ログは /data に置く (ボリュームで永続化する)
    GIT_SYNC_WORK_DIR=/data/work_repo \
    GIT_SYNC_LOG_FILE=/data/logs/git_sync.log \
    # 0 = 1回実行して終了 / 秒数を指定すると常駐して定期実行
    GIT_SYNC_INTERVAL=0

VOLUME ["/data"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
