# 導入

このMacのCodex実行には[Docker Sandboxesランチャー](sbx-launcher.md)を使用する。
以下のDiscord/GitHub設定は共通だが、後述の旧Compose workerへのログイン・起動手順はこの方式では使わない。
ChatGPT OAuthと既存ジョブの実機接続、Discord Botの権限、GitHub Appの認証は検証済み。
実CIとDiscord受付からレビューまでの動作確認は未完了。

## ローカル検証

READMEのmock手順を実施。`docker compose config --quiet`、`docker compose ps`で確認する。
macOS Apple SiliconのDockerでビルド・起動を検証。Linux amd64実機は未検証。
Linuxではsecretsのbind mountにホストの所有権が反映される。非rootサービスはUID/GID 10001で動くため、必要に応じて秘密ファイルをroot:10001・0640にする（ディレクトリは0700を維持）。
設定ファイルはコンテナから読み取れるようにする。秘密の値を標準出力へ表示しない。

## Discord

指定された招待の公開APIから確認した情報:

- サーバID: `362822936836440064`
- 招待先チャンネルID: `362822936836440068`（general）
- 許可ユーザー: `312561681387487232` とサーバー所有者 `234858405448122368`

招待URL自体は設定にもリポジトリにも保存しない。

1. Discord Developer PortalでApplication/Botを4つ作る。表示名は統括／上流エンジニア／下流エンジニア／情シス・SRE。
2. 各Botのトークンを `secrets/discord_coordinator_token` / `secrets/discord_upstream_token` / `secrets/discord_downstream_token` / `secrets/discord_sre_token` に保存。
3. OAuth2の `bot` と `applications.commands` で指定サーバへ追加。View Channel、Send Messages、Send Messages in Threads、Create Public Threads、Read Message History、Attach Filesを与える。Administrator不要。
4. 自分のDiscordで開発者モードを有効化し、ユーザーIDをコピーして `owner_ids` に設定。
5. 統括BotだけDeveloper PortalのBotページでMessage Content IntentをONにする。`message_content: true`と`natural_language_requests: true`を設定すると、統括エージェントがgeneralの会話文脈を読み、返答・専門Botへの発言委任・新規依頼・追加確認を選ぶ。案件スレッドの通常返信は要件回答として扱う。他の3 BotではOFFのままにする。
6. 通常の案件worker、統括worker、上流会話worker、下流会話worker、SRE workerの5プロセスを起動する。会話用の3役は別プロセス・別Sandbox状態領域を使い、Discord側の上限3件と合わせて並列実行する。一時的な409/502/503は役ごとに1回再試行し、最終失敗もDiscordへ表示する。

通常運用では5本をターミナルから直接起動しない。`deploy/com.meron14725.discord-agent-workers.plist`を
`~/Library/LaunchAgents/`へ登録し、launchdから`scripts/supervise_sbx_workers.py`を起動する。
supervisorは各workerの終了、役割、容量、認証付き`/health`を監視し、3回連続で応答しない場合に終了する。
launchdがworker一式を再起動し、標準出力と標準エラーは`artifacts/host-workers*.log`へ保存する。
起動スクリプトは環境設定を組み立てるentrypointであり、プロセスの寿命はlaunchdが管理する。

```sh
cp deploy/com.meron14725.discord-agent-workers.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.meron14725.discord-agent-workers.plist
launchctl print gui/501/com.meron14725.discord-agent-workers
```

設定更新時は次の順で読み直す。

```sh
launchctl bootout gui/501/com.meron14725.discord-agent-workers
cp deploy/com.meron14725.discord-agent-workers.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.meron14725.discord-agent-workers.plist
```

情シス・SRE Botは最初からAdministratorや管理権限を付けない。
[Discord管理権限の安全設計](../adr/0003-discord-sre-safety.md)のポリシー実装と検証が終わるまでは、
View Channels、Send Messages、Send Messages in Threads、Read Message History、Attach Filesだけで追加する。
検証後もAdministratorは使わず、必要な管理権限を個別に追加する。

Bot/Webhook/許可外ユーザーの発言はジョブを作らない。承認ボタンはDBのoutboxレコードに紐付き、再起動後も版・SHAを再検査する。
再接続時の履歴補完は100件まで。長期停止時の未受付回答は `/answer` で再入力する。
[discord.py interactions](https://discordpy.readthedocs.io/en/stable/interactions/api.html)

## GitHub: 案件ごとにrepoを作る

`config.subscription.example.yaml` は `/request repo:project` のたびに、`meron14725/project-task-<ID>` を割り当てる。
仕様を承認するまではrepoを作らない。キャンセル後も作成済みrepoは削除しない。

1. `meron14725` のrepo作成に使える専用ユーザートークンを `secrets/github_publisher_token` に配置。owner名はAPIで照合する。GitHub Appのinstallation token単独をユーザーrepo作成に使う経路には未対応。
2. 作成・ソース読み取り・feature更新・Issue/PR作成に必要な権限を与える。可能ならGitHubのfine-grained PATで対象所有者を絞る。案件repoが後から増えるため、そのアクセス対象と最小権限の両立を確認する。
3. 独立したレビュー用GitHub Appを[GitHub App認証](github-app.md)に従って設定。固定トークン方式の場合だけ `secrets/github_reviewer_token` を使う。`reviewer_login` と実際のGitHubレビュー主体を一致させる。同一ユーザーのPATを2枚発行しても別主体にはならない。
4. マージ用認証を `secrets/github_merger_token` に設定。publisherと同じownerを使う場合も、操作入口は分かれている。保護を迂回できる運用はしない。
5. 新repoへレビューAppを適用できるか確認。installationを選択repoだけに限定している場合、新規repoも許可する必要がある。Appの短期トークンは自動更新する。固定PATの期限切れは停止→置換→再起動で更新する。

推奨: 信頼済みの小さなPythonテンプレートrepoを用意して `template_repository: meron14725/<名前>` を指定する。
CIワークフローはテンプレート側で管理し、エージェントPRから変更できない。新repoの作成APIはテンプレートかREADME初期化のどちらかを使う。
[GitHub repository APIs](https://docs.github.com/en/rest/repos/repos)

テンプレートに入れるもの:

- `src/`、`tests/`、実行可能なunittestベースのCI。
- CIのpermissionsはread-only、PRコード実行に秘密を渡さない。`pull_request_target`でPRコードを特権実行しない。
- `test_commands`と一致する、実際にテストを発見するコマンド。任意依存はworkerイメージへホスト管理で追加する。

準備済みテンプレートは `templates/python/`。登録は以下で再開できる。

```sh
PYTHONPATH=src .venv/bin/python scripts/provision_template.py --apply
```

専用の非公開repo `meron14725/agent-team-python-template` を作成済み。
ソース・テスト・CIの登録を完了し、GitHub Actionsの`tests`が成功した。
実際の発行App ID `15368`を設定に反映済み。
作成時はpublisher PATのWorkflows write不足で403となり、ユーザーによる権限追加後に解消した。
既存の同一内容はスキップし、異なる内容は初期READMEを除いて上書きしない。
`docs/template-validation.json`にCI結果を記録した。

2026-09-12: `config.yaml`をliveへ切り替えた。元のmock設定は`backups/config-before-live.yaml`に保存。
マージはdisabled。PostgreSQL・orchestrator・Discord gatewayとMacのsbxランチャーが稼働中。
Discordの8コマンド登録とorchestratorからworkerへの認証付きHTTP 200を確認した。
実案件はまだ0件。次は所有者がgeneralチャンネルで`/request repo:project`を実行し、
仕様を読んで承認する。Discord受付・仕様生成・実装・実PR・レビューの一連の検証は未完了。

初回案件 `TASK-cd201ef3-83f` では仕様承認後、テンプレートからrepoを作成できたが、
作成直後のmainブランチ取得がHTTPエラーとなりBlockedになった。数秒後の読み取りでは
publisherとreviewer Appの両方からrepo・main・treeがHTTP 200だったため、GitHubの反映待ちと判断した。
新規作成直後に限り、main取得の404/409を最大6回待って再試行する。認証エラー等は再試行しない。
修正後のテストは65件成功。既存repoは再利用し、Discordの`/retry`から再開する。

初回`/retry`では実装生成まで完了したが、Sandbox内は`python3`のみで、設定の`python`が
終了コード127となった。テンプレートCIと同じ`python3 -m unittest discover -s tests`へ統一した。
失敗した成果物は公開前検査で止まり、GitHubへpushされていない。

新repoのmain/master保護はテンプレートから自動継承されると仮定しない。各repoに厳格な最新base追従、独立承認1件以上、更新後の承認失効、会話解決、管理者にも適用を設定する。
CIはcheck名だけでなく発行App IDを取得して `checks` に設定。サンプルの `app_id: 1` はプレースホルダー。
保護の取得権限・契約プラン・ルールの実効性が揃わなければ `merge_mode: disabled` のままPRまで使う。

[GitHub PR merge API](https://docs.github.com/en/rest/pulls/pulls)、[ブランチ保護API](https://docs.github.com/en/rest/branches/branch-protection)

## Codexサブスク

現ホストのsandbox検査は未通過です。[P0停止条件](../adr/0002-sandbox.md)を先に確認してください。

公式のChatGPTログインと認証ファイル永続化を使用する。現在使っているアプリのログインがDocker内へ自動で引き継がれるわけではない。
同じサブスクアカウントでコンテナ用のログインを1回行う。

```sh
docker compose --profile live build
docker compose --profile live run --rm --no-deps upstream-worker codex login --device-auth
```

表示されたコードをブラウザで承認する。device loginが無効ならChatGPTのセキュリティ設定／管理者設定を確認する。
認証の保存先は専用の `codex-auth` named volume。Codexが更新した認証情報を次回も使うため、readonlyコピーを毎回戻すことはしない。
ホストの既存 `~/.codex` 全体はマウントしない。

```sh
docker compose --profile live run --rm --no-deps upstream-worker codex login status
```

`auth_mode: chatgpt` とComposeの `AUTH_MODE: chatgpt` を揃える。
`model: ''` はCLIの既定モデルを使う。使いたいモデルが決まったら、アカウントで利用可能なモデル名を明示する。
APIキー方式へ切り替える場合だけ、両設定を `api_key` に変更し、`secrets/openai_api_key` とモデル・日次／案件／実行予約ドル上限を設定する。
予約額は実請求額ではなく、ジョブ起動を制限する推定値。プロバイダー側制限も必要。

[OpenAI Authentication](https://learn.chatgpt.com/docs/auth)、[認証維持](https://learn.chatgpt.com/docs/auth/ci-cd-auth)、[非対話実行](https://learn.chatgpt.com/docs/non-interactive-mode)

## 接続後の確認順序

1. `merge_mode: disabled`、専用検証repo、短い案件でBot受付と質問回答。
2. 承認前にrepoが作られないこと、承認後に1 repo/Issue/PRだけ作られること。
3. 別主体のレビュー、意図的なCI失敗、SHA変更、停止・再起動を確認。
4. ブランチ保護とcheck発行主体を確認して `human_gate` へ変更し、検証repoで人間承認マージ。
5. 全受け入れ条件の実接続検証を記録してから本案件へ。

現在の実装でbaseが変わるとBlockedになる。最新base取り込みを伴う自動再開は未対応なので、当面は案件repoのbaseを並行更新しない運用とする。
