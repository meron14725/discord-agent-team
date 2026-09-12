# Discord Agent Team

Discordを窓口として、統括AIが会話と仕事を振り分け、上流AIが仕様とレビュー、下流AIが実装と修正、情シス・SRE AIが運用と安全なDiscord変更を担当する一人会社の基盤です。
仕様書に基づく初期実装です。**Docker Sandboxes上の実Codex、実Discordの4 Bot、GitHub認証を接続済みです。SREの変更提案→人間承認→実行→確認まで実サーバーで検証しています。**

要件追加を反映しています。

- GitHub: `meron14725` 配下に案件ごとの非公開リポジトリを作成。作成タイミングは仕様承認後。
- Discord: 招待先サーバ `362822936836440064`、`general` チャンネル `362822936836440068`。
- Codex: ChatGPTログインでサブスクを利用。APIキーへの自動切替なし。
- 将来像: 人間10人ではなくAI10役。現在は統括、上流、下流、情シス・SREの4役。[10役への拡張設計](docs/scaling.md)。

## まず試す

外部アカウントもモデル呼び出しも不要です。

```sh
python3 scripts/init_config.py
docker compose up -d --build
docker compose run --rm --no-deps demo
```

`demo` はネットワーク無効・一時SQLiteで、質問整理→仕様承認→模擬実装→レビュー修正→人間承認→模擬マージを通します。
デモの承認者入力・テスト成功・GitHubの結果はシミュレーションです。並行して起動する通常の制御サービスはPostgreSQLを使います。
DBと制御APIのホスト公開ポートはありません。

```sh
docker compose ps
docker compose logs --tail=100 orchestrator
docker compose stop
```

## 本接続

**旧Compose worker内のCodex sandboxはP0検査が失敗しています。** このMacでは[Docker Sandboxesランチャーの起動手順](docs/runbooks/sbx-launcher.md)を使います。ランチャー、既存ジョブ、役別の会話workerとの接続は実機検証済みです。[旧方式の検証](docs/adr/0002-sandbox.md)・[移行の原因説明](docs/runbooks/docker-sandboxes.md)。

[導入手順](docs/runbooks/setup.md)に従い、`config.subscription.example.yaml` を元に設定します。
実設定とトークンは `config.yaml` と `secrets/` に置き、Gitには保存しません。公開用の設定例には実トークンを記入しないでください。
通常のサーバ招待リンクはBotのインストールURLではありません。

```sh
# 設定編集とトークン配置後
cp config.subscription.example.yaml config.yaml
# config.yaml のプレースホルダーを編集

docker compose --profile live build
docker compose --profile live run --rm --no-deps upstream-worker codex login --device-auth
# 自分のサブスクアカウントでブラウザ承認

docker compose --profile live up -d
```

追加4役は内部相談には利用でき、Discord接続だけが既定で無効です。各Discord Appを作成してtokenを`secrets/`へ保存し、
`role_registry`の対象役を一役ずつ`discord_enabled: true`にしてから、次のoverlayで起動します。

```sh
docker compose -f compose.yaml -f compose.optional-bots.yaml \
  -f compose.sbx.yaml --profile live up -d --build
```

有効な役だけがGatewayへ接続されます。全員宛ての発言では、その時点で有効な全専門役が
それぞれ応答します。

`/request repo:project summary:作りたいもの` から始めます。サブスク利用枠は全AI役で共有します。
v1はサブスク認証時にモデル同時実行1件、v2は通常最大4件・SRE特権1件です。v2は1案件30モデル実行、内部相談15回・同一話題5回、1実行20分、計画／実装修正各3回を上限にします。これらはアプリ側の上限で、契約の利用枠を表す数値ではありません。

実接続の設定例は `merge_mode: disabled` です。CI・独立レビュー・ブランチ保護の実効性を確認してから `human_gate` に変更します。
`auto_low_risk` のコードもありますが、実アカウントでの自動マージは未検証です。

## 操作

| Discord操作 | 動作 |
|---|---|
| `/request repo:project summary:...` | 案件を保存し、専用スレッドで要件整理 |
| `/answer task:... text:...` | 質問に回答。Message Content Intent不要 |
| 仕様承認ボタン | 本文hash・仕様版を固定し、repo/Issue作成・実装へ |
| `/status task:...` | 状態・PR・停止理由 |
| `/revise task:... text:...` | 旧承認と旧ジョブを失効して仕様を作り直す |
| マージ承認ボタン | 表示されたhead/base/仕様hashのみを承認 |
| `/pause` / `/resume` | 停止／再照合して再開 |
| `/cancel` | 終端キャンセル。既存repo・PR・ブランチは残す |
| `/retry` | Blockedの原因解消後に再照合・再実行 |

## 実装と検証

Python、FastAPI、SQLAlchemy、PostgreSQL、discord.py、Codex CLI 0.154.0、Docker Composeを使用。
DB状態更新とoutboxは同一トランザクション。ジョブはリース・fencing・仕様版を照合。
GitHubの書き込みは制御側のRESTアダプターのみが行い、実行ワーカーにはGitHub/Bot/DB資格情報を渡しません。

エージェント規則は、全役に優先適用する `prompts/company-policy.md`、会社の現状と役割一覧を持つ `prompts/company-memory.md`、各役だけに適用する `prompts/roles/*.md` に分けています。役割別規則は会社共通規則を弱めたり上書きしたりできません。[規則の分類と変更方法](docs/agent-rules.md)。

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check src tests scripts
uv run agent-team demo
```

- [受け入れ条件・検証結果・未対応範囲](docs/acceptance.md)
- [Issue正本の開発フローv2と段階的な有効化](docs/runbooks/workflow-v2.md)
- [構成の判断と仕様との差分](docs/adr/0001-mvp.md)
- [バックアップ・復旧・移行](docs/runbooks/operations.md)
- [元仕様](docs/discord-agent-team-spec.md)

この版は小規模なUTF-8テキストのPythonリポジトリ向けです。バイナリ・symlink・submoduleは停止します。
base更新時の自動統合、CI障害の自動原因分類、GitHub Appトークン自動更新、厳密な外向きドメイン制限は未対応です。
