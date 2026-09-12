# 既存ジョブとDocker Sandboxesの接続

## 検証結果（2026-09-12）

- 実Engine→認証付きHTTP→上流Codex: 確定仕様を作成し`AwaitingSpecApproval`に遷移。
- 一時DBの模擬オーナー承認→下流Codex: `tests/test_example.py`を作成。unittest 3件成功。
- 既存のResult ID/hash/SHA・差分・テスト検査を通り、模擬GitHub公開後に`Reviewing`へ遷移。
- 別試行ではCodexの追加質問が`Clarifying`へ反映されることも確認。
- 上流はモデル前のroot書込み検査でEROFS。下流はホストworkspaceなし。
- モデル未使用の実VMで親子プロセスを動かし、キャンセル後VMが存在せず回収記録が空になることを確認。
- Compose内→`host.docker.internal:8090`への認証付きhealth応答を確認。
- pytest全61件成功、ruff成功。通常の設定はmockのまま。検証ランチャーは終了済み。

GitHubとDiscordは模擬。実PR・実レビュー・CI・マージ・Discord投稿の実証は含まない。

## 構成

既存EngineのRunRequest/RunResponse契約を維持し、認証付きRemoteRunnerからMacのランチャーへ依頼する。
ランチャーはジョブごとに固定digestのVMを作成し、最終Result・差分・テスト結果を返す。
既存Engineの仕様hash、head/base SHA、結果保存、承認、GitHub公開前検査を通る。

```text
Compose内のEngine → Bearer認証 → Macのランチャー（127.0.0.1:8090）
                                  → ジョブ専用VM → Codex
                      ← Result・差分・テスト ←
```

Discord/GitHubの資格情報をランチャーへ渡さない。ランチャー用worker_tokenは制御層とランチャーだけが共有する。
ホストのDocker socketをComposeやエージェントへマウントしない。
仕様役と実装役は同じHTTP窓口を利用するが、同時実行は1ジョブ、VMは毎回別。

## 役割ごとの境界

- 上流: リクエスト内のソースをホストの専用一時ディレクトリに展開し、追加の`:ro`マウントとして渡す。
  sbxの一次マウントはread/write必須のため、別の空の一時scratchディレクトリだけを一次マウントにする。
  実案件ディレクトリやホストhomeは共有しない。モデル呼び出し前にVM内rootによるソース書込みがEROFSになることを確認する。
- 下流: ホストworkspaceのマウントなし。JSONでソースをVM内に投入する。
- 共有skills無効、SSH転送無効を要求。登録済みのホストMCPがある場合は停止する。
- テンプレートが追加するネットワーク許可を列挙し、ChatGPT以外へVM単位のdenyを追加。
  ChatGPT許可、GitHub・一般サイト・ホストランチャーへの拒否をモデル呼び出し前に確認する。
- Codexの案件・ユーザー設定とrulesを無視し、ChatGPT proxy providerをランチャーの固定引数で指定。
  `api.openai.com`への切替を許可しない。モデル名は既存configの指定を使い、空ならCodexの既定値。
- 結果は途中のメッセージでなく最終JSONファイルを使う。スキーマと案件ID/hash/SHAを確認する。
  差分は100ファイル・2 MBに制限し、非通常ファイル・危険な相対パスを拒否する。

テストはVM内で実行するため、生成されたテストコードがホスト上で動くことはない。
VM内のテスト結果だけを独立したCIの代わりにはしない。既存のレビュー・CI・マージ承認ゲートは維持する。

## 起動

前提: sbx 0.42.1、承認済みChatGPT OAuth登録、SSH転送false、ホストMCP未登録。
macOS + Docker Desktopの`host.docker.internal`からlocalhostランチャーへ認証付きHTTPで到達することを実測済み。
Linux等の別ホストで同じネットワーク設定が使えることは未検証。

```sh
python3 scripts/init_config.py
sh scripts/run_sbx_worker.sh
```

ランチャーはフォアグラウンド起動。停止はCtrl-C。同じstateディレクトリの二重起動はファイルロックで拒否する。
worker_tokenをコンソールやチャットへ出力しない。

Discord/GitHub設定が完了した後のCompose起動:

```sh
docker compose -f compose.yaml -f compose.sbx.yaml --profile live up -d --build postgres orchestrator discord-gateway
```

サービス名を明示し、旧upstream-worker/downstream-workerを起動しない。
`compose.sbx.yaml`はURLをホストランチャーへ向け、レビュー用GitHub Appの秘密鍵をorchestratorだけに渡す。
configのmodeやマージ方針は変更しない。
2026-09-12追記: 現在のconfigはlive、mergeはdisabled。Botのコマンド登録とworker疎通まで確認済み。
実Discord依頼からPR・レビューまでの検証はユーザーの初回依頼待ち。詳細は[導入](setup.md)を参照。

## 再現検証

```sh
# 一時DBと模擬GitHubを使う。実モデルは最大2回、模擬オーナー承認を含む。
PYTHONPATH=src .venv/bin/python scripts/probe_sbx_job.py

# モデル使用なし。VM内の親子プロセス起動→キャンセル→VM削除。
PYTHONPATH=src .venv/bin/python scripts/probe_sbx_cancel.py

.venv/bin/python -m pytest -q
```

結果: `docs/sbx-job-validation.json`、`docs/sbx-cancel-validation.json`。
実GitHubへリポジトリ・PRを作成したりDiscordへ投稿したりはしない。

## 失敗・停止・再起動

タイムアウトはVM作成から回収までの全体に適用し、finallyでVMを削除する。
HTTPキャンセルもCLI子プロセスを止めた後、VMごと削除する。
作成するVM名を`artifacts/sbx-state/vms.json`へ先に記録し、ランチャー再起動時に記録されたVMだけを削除する。
ジョブはEngineの既存lease/fencingで失敗・再試行判定を行い、古い結果の採用を防ぐ。
Docker認証切れなどで削除できない場合は記録を残し、黙って成功にしない。
異常終了時のホスト一時scratch/sourceファイルの自動掃除は未実装。VM回収後、OSの一時領域に残る場合がある。

## 今回判明した互換性

1. `codex <source>:ro`を一次workspaceにすると`primary workspace must be read/write`で停止する。
   専用の空scratchを一次workspace、ソースを追加readonlyにする。
2. Codex組み込みkitはGitHub/npm等の通信を追加許可する。ホスト側deny-allだけではモデル接続時の限定許可にならない。
   VMごとの有効allow一覧から、ChatGPT以外を明示denyする。
3. `--ignore-user-config`はDocker注入の`model_provider=sandboxd`も無視するため、そのままではAPI側へ向かう。
   ChatGPT URLとダミーBearerを信頼済み引数として明示する。実トークンはホストプロキシに留める。
