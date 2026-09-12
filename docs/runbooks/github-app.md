# レビュー用GitHub App

作成者はpublisher PATの`meron14725`、レビュアーは`meron14725-agent-reviewer[bot]`。
App IDは4918109、installation IDは161067940。`config.subscription.example.yaml`に反映済み。
通常の`config.yaml`はmockのまま。CI issuerのプレースホルダーを解決してからlive設定を適用する。

秘密鍵はgitignore対象の`secrets/github_reviewer_private_key.pem`に保存する。
`compose.yaml`と`compose.sbx.yaml`を併用するとorchestratorの
`/run/secrets/github_reviewer_private_key`だけに渡され、エージェントのVMには渡されない。
ホストからorchestratorを起動する場合は`GITHUB_REVIEWER_PRIVATE_KEY_FILE`で指定する。
App設定がある場合、レビュー用の固定PATファイルは読まず、秘密鍵から認証する。

Appはmeron14725の全リポジトリにインストール済み。
権限はContents read、Pull requests write、Checks read、Metadata read。
要求する短期トークンの権限も同じ用途に限定する。
短期トークンはメモリだけで保持し、期限の60秒前以降の次の要求で再発行する。
認証失敗時にpublisherへ切り替えたり、書き込みを自動再送したりしない。
CIのcheck-runsもreviewer Appで取得する。

## 検証

`docs/github-app-validation.json`に機密値を含まない結果を保存した。
GET /appとGET /app/installationsでApp・インストール先・権限を確認し、
新しいInstallationAuthでトークンを発行してGET /installation/repositoriesのHTTP 200を確認した。
秘密鍵・JWT・短期トークン・リポジトリ名はログへ出さない。
実PRへのレビュー投稿と実check-runs取得はCI検証用リポジトリを用意した後に行う。

自動テストは署名、権限指定、キャッシュと期限前更新、発行失敗時の停止、送信先の制限を検証する。
実APIで、認証用httpx.RequestにUser-Agentがないと403になることが判明したため、明示して解消した。

参考: [JWTの生成](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)、
[インストールトークンの生成](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)。
