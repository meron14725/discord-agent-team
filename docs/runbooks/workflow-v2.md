# Issue正本の開発フロー v2

## 現在の状態

2026-09-13に実運用設定で`workflow_v2.enabled: true`へ移行した。現在は`project` aliasをallowlistとし、最初の案件チャンネルにはgeneralを割り当てている。
既存v1案件は移行せず、そのまま完了させる。v2はallowlistに入った新規案件だけを対象にする。

移行前バックアップは`backups/pre-v2-20260913`。schema migration version 1・2、Dockerの4サービス、launchdの6 workerを確認した。
移行直後の実モデル疎通では、Codexの厳格な構造化出力が全プロパティを`required`に含めるよう要求し、統括workerがHTTP 503で停止した。Codexへ渡すschemaを再帰的に厳格化して復旧し、統括workerとv2専用workerの両方でHTTP 200と有効な構造化出力を確認した。復旧後の完了通知は統括Botからオーナーへメンション付きで送信する。

移行後の最初のCTO委任では、役割台帳が旧ID `upstream`を正式ID `cto`へ解決した一方、会話workerの受付表が旧IDだけを許可していたためHTTP 403になった。上流・下流・SREの会話workerが新旧IDを同じ権限範囲で受け付けるよう修正し、正式ID `cto`・`backend_integrator`・`security_sre`の3役を並列実行して、すべてHTTP 200と有効な実モデル応答になることを確認した。復旧通知は統括Botからオーナーへ送信した（DiscordメッセージID `1548378556810264689`）。

最初のper-task案件では、要件Issue作成前に未作成の案件repositoryからbase branchを読もうとしてGitHub 404になった。v2はIssueを正本にするため、private案件repositoryを冪等作成し、templateのbaseが読めるまで限定再試行してからIssueを作る。準備、worker実行、成果物処理の失敗通知を区別し、失敗したv2 jobは同じ工程と保存済みfence情報から再試行する。

同案件の要件確認では、CTOが`needs_clarification`で正常終了してオーナー回答を待っている状態をstalled監視が実行停止と誤認し、不要な再実行後にBlockedへ遷移した。stalled回復はqueued/runningのjobが実在する場合だけに限定した。また、オーナーが同一guild内の過去メッセージを回答として示した場合は、統括Botが最大3件まで、同じguildかつ登録オーナー本人の投稿だけを読み取って本文を回答へ付加する。参照本文にも秘密検査を適用する。

再開後、CTOは受入条件IDをMarkdownの太字（`**AC-001**`）で生成したため、要件検証器がIDを認識せず保存工程で停止した。検証器は通常表記と太字表記の両方を同じIDとして受け付け、生成指示では正規形`- AC-001: 検証可能な条件`を要求する。

## 1案件の流れ

1. オーナーがrepository別チャンネルへ自然文で目的を書く。
2. 統括が受付し、CTOが目的と判断基準を整理してGitHub Issueを作る。
3. 必要な論点だけ話題スレッドを作り、内部相談の結論をIssueコメントへ残す。
4. 要件説明HTML/PNGとIssue本文hashを提示し、許可されたオーナーが承認する。
5. CTOから実装統合担当へ型付きで引き継ぎ、Bot同士のメンション付き会話を表示する。
6. 実装統合担当が通算版の計画をbranchへ保存し、別のCTO sessionがレビューする。重大指摘は最大15回修正する（`plan_revision_limit`）。
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

## 正式案件のリポジトリ選択

統括へ `repos` の alias、repository、description、per_task を渡し、
`repository_alias` を選択させる。既定aliasやgeneralのチャンネル割当だけで新規repoを作らない。
未指定は対象確認へ戻し、台帳外のaliasは拒否する。専門家からの案件提案にも同じ選択結果を使う。

運用設定では `discord-agent-team` を既存基盤の改修先として登録する。
`per_task: false`、`repository: meron14725/discord-agent-team` とし、
Botの人格・会話・統括・連携・運用の改善をdescriptionに記載する。
新しい独立案件だけ `project`（per_task: true）を使う。
基盤へのルーティングは実装・マージ・権限変更の承認を兼ねない。

## 実行回数上限（2026-09-13変更）

オーナー指示により回数上限を従来の5倍へ変更した。v2は案件のモデル実行150回、案件内相談75回、同一話題の相談25回、計画修正15回、実装修正15回。v1の実行回数は日次150回、案件50回。並列数・接続再試行・承認条件は従来どおり。Codex契約側の利用上限を変更する設定ではない。

DBスキーマv3は既存の使用回数と相談履歴を保持して制約を更新する。モデル上限で停止した案件は、設定反映後に同じ案件を再試行すると、仕様・計画の版を照合して取り消された処理を再開する。承認や使用回数はリセットしない。

## 差分未提出と要件整理の再起動ループ

実装担当が差分を返さないエラーをHTTP 500だけで扱うと、同じ依頼を繰り返して停止原因が隠れる。既知の「差分未提出」だけを固定のエラーへ分類し、統括が出力形式の修正指示付きで1回再依頼する。再発時は停止する。確認待ち・blockedの担当応答には差分を要求せず、その質問と理由を制御側へ戻す。

実装開始時は要件承認に加えて、計画版・ハッシュ・base SHAに一致する承認記録を照合して担当へ渡す。再依頼も実行予算を消費する。承認不足や別版への変更を自動承認で解決してはならない。

Issue更新後、要件整理中・要件確認待ち・承認を失効した確認待ちの案件を定期照合が再度取り消してはならない。Paused/Cancelledも再開しない。要件本文が実際に変更された場合は、その版の要件確認へ進む。

ホストPythonの依存パッケージ読込が`Resource deadlock avoided`で失敗した場合は、再起動の繰り返しだけでは直らない。Documents外の専用仮想環境へ`uv sync --frozen --no-dev`で依存を再構築し、launchdのPythonおよび環境変数`TEAM_WORKER_PYTHON`へ指定する。各起動スクリプトはこの指定を優先し、未指定時は従来の`.venv/bin/python`を使う。現行Macは`~/.local/share/discord-agent-team/venv`を使用する。6つのworkerのヘルスチェック成功を確認してから停止案件を再試行する。

同じ読込障害がVM復旧ジャーナルにも発生したため、状態保存先も`TEAM_WORKER_STATE_ROOT`で分離する。現行Macは`~/.local/share/discord-agent-team/state`。移行時はworkerを停止し、`sbx ls`で残存sandboxがないことを確認した。旧ジャーナルは削除しない。残存sandboxがある環境では、ジャーナルを移行・照合するまで空の状態で起動してはならない。

v2の接続断（`ConnectError`）は、認証付きworkerヘルスチェックで役割・空き容量・正常状態を確認後、同一jobについて1回だけ自動再開する。最新jobの版一致・実行予算・未保存応答を確認し、特権操作とPaused/Cancelledを対象にしない。HTTP 500全般、承認不足、秘密情報検査違反を接続断とみなしてはならない。

## 人格実装の案件限定メンテナンス許可

人格追加は通常の`prompts/*`変更禁止と衝突する。オーナーが承認した保守案件に限り、信頼済み設定`maintenance_authorizations[task_id]`へrepository、requirements_hash、plan_hash、owner_id、pathsを記録する。pathsは承認済み計画から抽出した正確なファイル名のみで、globは不可。人格定義`prompts/personas/<role>/vN/PERSONA.md`、設定例`config.example.yaml`、src/testsの統合変更に限定する。会社規則、AGENTS、CI、実設定、秘密情報は例外対象外。

制御側で案件・要件・計画・オーナーを照合してからworkerへ渡し、モデル出力内の自称許可は採用しない。workerの差分適用時と制御側の成果物検査時に同じパス範囲を検証する。別版への変更時は許可を再確認する。通常案件の禁止事項・レビュー・マージ・本番反映の条件は変えない。

実装・修正担当のblocked/needs_clarificationへの`answer`は、同じ仕様・計画の版であれば担当への補足として保持し、承認を外さず再開する。補足で承認済み範囲を変更してはならず、変更が必要なら明示的な`revise`へ戻す。`revise`は従来どおり承認を失効させる。人格案件の読み取り入力には既存のpyproject、lock、prompts、vendorも含め、テスト・ローダーから参照できるようにする。これらの読み取り許可は書き換え許可ではない。

大規模な人格実装では全ソース本文の一括入力と一回の差分応答だけでは実装が進まなかったため、案件限定メンテナンスでは読み取り専用マウントのソースをshell読取で段階的に参照させる。モデルは/tmpで草案を組み立てられるが、ソースとbroker出力の直接編集は禁止し、差分適用・テストはbrokerが実施する。通常のbrokered案件のshell禁止は維持する。モデル入力はソースパス一覧と許可コマンドを渡し、巨大な本文を重複送信しない。

保守案件と独立レビューのPythonテストは、信頼済みuv.lockからLinux/Python 3.14向けwheelを準備し、`SBX_TEST_RUNTIME_DIR`配下のrequirements.txtとwheelsをsandboxへ転送して実行前に導入する。`uv pip install --no-index --require-hashes`でハッシュ検証とオフライン導入を必須にし、モデルから依存追加先やコマンドを指定させない。v2の起動スクリプトは`TEAM_TEST_RUNTIME_DIR`（既定`~/.local/share/discord-agent-team/test-runtime`）を使う。現在のsandboxにpython3だけが存在するため、brokerがpythonへの互換リンクも用意する。独立レビューにはbaseソースと固定済みテストコマンドも供給し、承認時はbrokerのテスト成功証跡を必須とする。通常案件はこの保守用環境導入の対象外。

大きな保守差分はモデルの応答JSONへ埋め込まず、`/tmp/team-implementation.patch`へ生成し、PatchProposalのpatch_fileで固定パスを参照する。brokerは通常ファイル・2MB上限・秘密検査・許可パスを検証してから適用する。検証済み差分はstate_dir/resultsへ保存し、テスト成功や適用成功を示す記録とは明確に区別する。任意パスやsymlinkの参照は許可しない。結果収集は既存スナップショット100件＋変更100件までを扱えるが、変更自体の100件/2MB上限は維持する。
