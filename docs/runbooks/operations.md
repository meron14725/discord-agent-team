# 停止・復旧・移行

## 停止

案件単位は `/pause` または `/cancel`。停止時に古いfencing tokenを失効し、新規副作用を防ぐ。
実行中Codexへの停止要求はheartbeat周期（通常15秒）で検出し、TERM→最大5秒→プロセスグループKILL。
GitHub API実行中はDB行ロックにより停止と操作を直列化するため、停止反映がAPI処理の終了まで遅れる場合がある。
送信済みマージは取り消せない。後続の照合で実際のmergedフラグとmerge SHAを確認する。

全体停止は `docker compose stop`。liveプロファイルを使う場合は `docker compose --profile live stop`。
稼働確認は `docker compose ps` / `docker compose logs --tail=100`。

## 再開

`docker compose up -d`、本接続では `docker compose --profile live up -d`。
期限切れリースは新fenceで取得。保存済み成果物は再利用し、Issue/PRはmarker・feature branchで照合する。
外部通信の結果が不明でも同じ案件の別PRを勝手に作らない。GitHubが手動で閉じられていたらBlocked。

`/retry` は原因解消後に実行。仕様不足は `/revise` で承認を取り直す。
CI失敗の原因はこの版では自動分類しない。インフラを修復して同じSHAのCIを再実行するか、必要な変更を `/revise` で依頼する。
baseが更新された案件の自動統合・自動再開は未対応。旧baseの承認でマージさせない。

## バックアップ

```sh
scripts/backup.sh
```

書き込みサービスを停止し、`pg_dump -Fc` とartifactsを同じ停止点で取得。スクリプトは停止状態で終了する。
configも保存する。秘密ファイルとCodex認証は通常のバックアップへ混ぜず、ホストの暗号化された保存先で別管理する。
初期推奨は日次バックアップ、結果ログ30日、承認・マージ監査180日。自動スケジューラーと保持期限削除は未実装なのでホスト側運用で実施する。
DBとartifactsには依頼やソースの情報が含まれるため、バックアップ先を公開しない。

## 別ホストへの復元

1. 移行元の全サービスを停止。そのホストを自動再起動しないようにする。
2. 移行先に同じリリースの設定・イメージ・バックアップを置き、secretsを別経路で配置。
3. 新規の空DB/volumeへ `scripts/restore.sh BACKUP_DIRECTORY`。既存DBを消す処理は含まない。
4. `config.yaml`を確認し、必要ならCodexを再ログイン。古いauthバックアップを稼働中の新しいauthへ上書きしない。
5. 移行先のみ起動。まずstatusとPR照合を確認する。

リーダーロックは同じDBへの二重接続を防ぐもので、別々のDBに復元された2台の競合を防ぐものではない。
実際の別ホスト復元試験は未実施。復旧時刻・DB件数・PR参照・未送信outboxを確認して記録する。

## 認証更新

Bot/GitHub tokenは該当サービスを停止→秘密ファイルを置換→再起動。ログへ値を出さない。
DBパスワードはファイル変更だけでは既存DBのユーザーパスワードが変わらないため、PostgreSQL側変更も必要。
Codexログイン失効は全ワーカー停止後、setupのdevice loginを再実行する。CLIが通常利用中に更新した認証は専用volumeに残る。

## 実接続前のP0

ビルドと `codex --version` の成功だけではsandboxの実効性・ログイン・実モデル出力は検証できない。
対象ホストで最小の非対話ジョブ、書き込み範囲、停止、予算・利用上限、ログイン更新を試験する。
コンテナでsandboxが拒否された場合、privilegedやDocker socket追加で回避しない。実機検証が成立するまでlive運転を開始しない。
