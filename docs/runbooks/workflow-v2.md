# Issue正本の開発フロー v2

## 現在の状態

2026-09-13に実運用設定で`workflow_v2.enabled: true`へ移行した。現在は`project` aliasをallowlistとし、最初の案件チャンネルにはgeneralを割り当てている。
既存v1案件は移行せず、そのまま完了させる。v2はallowlistに入った新規案件だけを対象にする。

移行前バックアップは`backups/pre-v2-20260913`。schema migration version 1・2、Dockerの4サービス、launchdの6 workerを確認した。
移行直後の実モデル疎通では、Codexの厳格な構造化出力が全プロパティを`required`に含めるよう要求し、統括workerがHTTP 503で停止した。Codexへ渡すschemaを再帰的に厳格化して復旧し、統括workerとv2専用workerの両方でHTTP 200と有効な構造化出力を確認した。復旧後の完了通知は統括Botからオーナーへメンション付きで送信する。

移行後の最初のCTO委任では、役割台帳が旧ID `upstream`を正式ID `cto`へ解決した一方、会話workerの受付表が旧IDだけを許可していたためHTTP 403になった。上流・下流・SREの会話workerが新旧IDを同じ権限範囲で受け付けるよう修正し、正式ID `cto`・`backend_integrator`・`security_sre`の3役を並列実行して、すべてHTTP 200と有効な実モデル応答になることを確認した。復旧通知は統括Botからオーナーへ送信した（DiscordメッセージID `1548378556810264689`）。

## 1案件の流れ

1. オーナーがrepository別チャンネルへ自然文で目的を書く。
2. 統括が受付し、CTOが目的と判断基準を整理してGitHub Issueを作る。
3. 必要な論点だけ話題スレッドを作り、内部相談の結論をIssueコメントへ残す。
4. 要件説明HTML/PNGとIssue本文hashを提示し、許可されたオーナーが承認する。
5. CTOから実装統合担当へ型付きで引き継ぎ、Bot同士のメンション付き会話を表示する。
6. 実装統合担当が通算版の計画をbranchへ保存し、別のCTO sessionがレビューする。重大指摘は最大3回修正する。
7. 計画説明HTML/PNG、計画hash、base SHAを提示し、オーナーが承認する。
8. 新しい一時sessionで実装し、Draft PR、独立レビュー、CI、最終索引を作る。
9. 最新head/base、要件hash、計画hashへのマージ承認後だけSquash mergeする。

要件本文はGitHub Issueだけが正本である。DBと`docs/work-items/issue-<番号>/README.md`には識別子、hash、版、リンク、判断記録だけを保存する。

## 有効化前の確認

1. DBをバックアップし、`agent-team migrate`後のschema version 2を確認する。
2. `renderer`、orchestrator、現在の4 Bot、役別workerのhealthを確認する。
3. `github_issue_conditional_updates: false`を維持する。GitHub.comのIssue PATCHは安全な条件付き更新に対応しないため、CTOがコメントへ出した要件案をオーナーがIssue本文へ反映し、Discordで再試行する。
4. repository別DiscordチャンネルIDを`project_channels`へ設定する。
5. 要件・計画承認者を通常会話の`owner_ids`とは別に指定する。
6. `merge_mode: disabled`のまま、検証カテゴリと非公開test repositoryだけで一周させる。
7. CI発行App、別主体レビュー、branch protection、最新SHA失効を確認してから`human_gate`へ切り替える。

## 中止と再開

`/cancel`は未送信の副作用、job、lease、承認を失効し、Issue closeを最大3回試す。Issue、branch、PR、artifact、監査、会話ログは削除しない。

再開は`/resume`ではなく`/restart`を使う。新しいtask IDとbranchを作り、前task IDと同じIssueを参照する。Issueを再度開き、要件説明と要件承認からやり直す。旧confirmation IDは使えない。

## 追加Bot

追加4役は内部相談では利用でき、Discord接続だけが既定無効である。対象roleのDiscord Appとtokenを用意し、`role_registry`で一役だけ`discord_enabled: true`にして次を使う。

```sh
docker compose -f compose.yaml -f compose.optional-bots.yaml \
  -f compose.sbx.yaml --profile live up -d --build
```

その役の通常応答、全員宛て応答、3回接続失敗、Bot投稿を命令として再処理しないことを確認してから次の役を有効にする。通常処理は最大4件、同じrepositoryの書き込みは1件、SRE特権操作は1件である。
