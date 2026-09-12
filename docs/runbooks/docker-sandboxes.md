# Docker Sandboxesへの移行検証

更新日: 2026-09-12。通常configはmockのまま。実機接続済みの新ランチャーは[別手順](sbx-launcher.md)で起動する。

## 今回の到達点

**microVM起動、ChatGPT OAuth実モデル実行、既存ジョブとの接続が成功。Discord/GitHub本接続は未設定。**

既存Engine→認証付きHTTP→専用VMを接続し、仕様作成→模擬承認→実装・unittest 3件成功→Reviewingへの遷移を実測した。
上流readonlyのモデル前検査、実装差分・Result契約の検査、実VMキャンセル・削除も確認。
詳しくは[ランチャーの実装・起動・検証](sbx-launcher.md)。

| 実測項目 | 結果 |
|---|---|
| sbx | v0.42.1、macOS arm64 |
| Codex用microVM | 作成・起動成功、workspace共有なし |
| テンプレート | `docker/sandbox-templates:codex-docker` |
| イメージdigest | `sha256:8b4cd0a46c8b600bc6b6a64af23c03d4c2807fbfc61f47568092a93fb9dc88b0` |
| Codex CLI | `codex-cli 0.149.1`の実行成功。旧workerの0.154.0とは異なる |
| 合成JSONの投入・書込み・回収 | nonceの一致を確認 |
| ホスト案件パス | VM内から存在しないことを確認 |
| 制御資格情報 | DB・Discord・内部APIの代表的環境変数なし。sbx登録秘密情報なし |
| MCP | 登録サーバなし |
| SSH | 転送設定false、`ssh-add -l`で鍵を取得できないことを確認 |
| 代表的外向き通信 | `https://example.com`がHTTP 403で拒否 |
| 停止・削除 | 成功。最後の`sbx ls`は`No sandboxes found` |
| 既存アプリ検証 | pytest 53件成功、ruff成功 |

### サブスク認証後の実モデル検証

共通OAuth設定への明示承認と本人ログイン後、[実モデル検証ログ](../sbx-model-validation.json)の全ステップが成功した。

- `sbx secret ls`でOpenAIのOAuth登録、`sbx inspect`でVMのOAuth利用を確認。APIキーは設定していない。
- 固定digestのVMへ合成入力`[20, 22]`だけを投入。
- Codexが入力を読み、`result.txt`へ`42`と改行を書き込み、最終JSON`{"ok":true,"total":42}`を返した。
- 信頼済み検証側で最終JSONとファイル内容を照合して一致を確認。
- 検証VMを削除。最後の`sbx ls`で残存VMなし。

モデル呼び出しは1回。CLI報告のusageはinput_tokens=37478、cached_input_tokens=8960、output_tokens=259。
これはCLIのトークン計測値で、サブスク残枠・金額の計測ではない。
小さな依頼でもCodexのツール・指示コンテキストを含むため、入力トークンは依頼文だけの長さにはならない。

途中の`agent_message`には`total=0`という未確定メッセージもあった。
ジョブの結果として採用するのは途中のJSONではなく、プロセス成功後の`--output-last-message`ファイルをスキーマ検証したもの。
今回も最終ファイルと実際の成果物の両方を確認し、途中のメッセージを成功結果には使っていない。

これは最小タスクでの実行経路の検証であり、既存Result契約の全項目、上流read-only、キャンセル、長時間認証維持の合格を意味しない。

`GH_TOKEN`はDockerプロキシ管理を示す40文字のダミー形式だった。
「変数が存在する＝本物の秘密」とする最初の検査が誤検知した。
値は表示せず、形式と登録秘密情報がないことを確認した。変数名がないことを必須にせず、
ダミー値かどうかと実際の認証経路を区別する検査に修正した。
同様に`SSH_AUTH_SOCK`も変数名の有無ではなく、転送設定と鍵を列挙できないことを確認した。
この検査は全ファイル・全ネットワーク経路の網羅的な侵入試験ではない。

## 何が問題だったか

以前の構成は「Dockerコンテナの中で、Codexがもう一段の隔離環境を作る」というもの。
CodexはLinux上でbubblewrapを使い、専用の名前空間とファイルのマウントを準備する。
一方、workerはDockerの制限を強めていたため、その準備操作まで拒否された。

実際の比較は以下の順序だった。詳細なコマンドは[ADR 0002](../adr/0002-sandbox.md)。

| 条件 | 実際の結果 | 分かったこと |
|---|---|---|
| 元のCompose設定 | `bwrap: No permissions to create a new namespace` | 内側の隔離環境を準備できない |
| `clone/clone3/unshare/setns`だけ許可 | 名前空間作成は成功 | 最初の停止原因はこの制限に関係している |
| 同じ設定でCodex起動 | `bwrap: Failed to make / slave: Operation not permitted` | 次のマウント操作も制限されている。4操作の許可だけでは不足 |

これはモデルの能力やDiscordの問題ではなく、隔離機構を重ねた際の権限設定の衝突。
mountを許可するだけですべて解決することまでは実証していない。

## Docker Sandboxesとは

AIエージェントを小さな仮想マシン（microVM）内で実行するDockerの製品。
`docker compose`で起動する普通のコンテナとは別で、`sbx`というCLIを使う。
仮想マシンごとにLinux環境と独立したDocker Engineを持つ。

移行後に目指す構成:

```text
Discord → 承認・案件管理・DB（既存Compose）
                     ↓ 信頼済みホストランチャー
                  sbx → 案件用microVM → Codex
                     ↑ 検査済みの結果・差分を回収
```

Docker公式のCodexテンプレートは内側のCodex sandbox/対話承認を無効化し、外側のVMで隔離する。
したがって解決の方針は「失敗する内側sandboxを直した」ではなく「隔離を担当する層を変える」。
仕様承認・マージ承認は引き続きこの基盤の制御層で行う。

ホストフォルダを共有すると、その範囲はVMから変更できる。
今回の検証ではworkspaceを指定せず、共有skillsも無効にする。ホストのソース・秘密ファイルは渡さない。
VMが起動しただけでは、上流のread-only保証や全ネットワーク経路の検証が完了したとは扱わない。

出典: [Docker構成](https://docs.docker.com/ai/sandboxes/architecture/)、
[CodexテンプレートとOAuth](https://docs.docker.com/ai/sandboxes/agents/codex/)、
[共有skills](https://docs.docker.com/ai/sandboxes/workflows/agent-skills/)。

## 再現手順

このMacには公式Homebrew tapから`sbx v0.42.1`を導入済み。
Docker本人ログインも完了。初期ネットワークポリシーは`deny-all`に設定済み。
これはsbx全体の初期設定であり、モデル接続時には必要な宛先だけ別途許可する。
Dockerクラウドの認証交換は403の警告があったが、ローカルの`sbx ls`は成功した。
その後、検証中にローカル管理APIも`401 Unauthorized / secret not found`となり、
再ログインが必要になった。[認証喪失時のログ](../sbx-validation-auth-loss.json)。
ユーザーの再認証で復旧し、残存VMの削除に成功した。再認証時には403警告はなかった。
初回のクラウド認証警告との因果関係や、長期的な認証保持の可否は未確認。
`sbx settings set ssh.agentForwardingEnabled false`とdaemon再起動を行い、SSH転送は無効に設定済み。

新規環境でのみ:

```sh
HOMEBREW_NO_AUTO_UPDATE=1 brew install docker/tap/sbx
sbx login
sbx policy init deny-all
```

起動検証（モデル呼び出しなし）:

```sh
python3 scripts/probe_sbx.py
```

自分で生成したランダム名のVMだけを作成し、終了時に削除する。
2 CPU・4 GiB、workspace共有なし、skills共有なし、外向き通信拒否。
Codexバージョン表示、合成JSONの受け渡し、ホスト案件パスが見えないこと、代表的な制御環境変数がないこと、example.comのHTTP拒否、停止・削除を確認する。
結果は[実行ログ](../sbx-validation.json)。各ステップの成功と全体成功を区別する。
初回の「通信ポリシー未初期化」エラーは[初回ログ](../sbx-validation-initial.json)に保存。

## モデル認証の承認境界

当初、自動承認レビューが「OpenAI認証をglobal scopeへ永続保存し、複数のSandboxから利用できる設定にする範囲が未承認」として拒否した。
その後ユーザーがこの共通認証の範囲を明示承認し、`sbx secret set openai --oauth`を実行した。
ブラウザでの本人ログインが完了し、`Saved OAuth token for service "openai" in scope "(global)"`を確認済み。
APIキーは設定していない。
CLI 0.42.1のhelpではOAuthは`openai/global only`で、Sandbox単位のOAuthは提供されていない。
この拒否を回避するためにホストの既存認証ファイルをコピーすることはしない。

承認対象は、OpenAI OAuthをホストのOSキーチェーンに保存し、sbxの共通認証として使うこと。
公式説明ではトークン本体をVMへ渡さずホスト側プロキシで認証する。
ただし他のSandboxからもモデル利用が可能になる範囲と、サブスク利用枠を消費し得る点は残る。
APIキーへの切替は行わない。

認証後の最小実モデル検証は`python3 scripts/probe_sbx_model.py`。
このスクリプトはサブスク利用枠を消費する。固定digestのVM内に合成入力だけを作り、
Codexにファイルの読み書きと構造化JSON応答を1回依頼して、双方の結果を検査する。
ホストworkspace共有・共有skillsなし、ChatGPT宛ての通信許可は検証VM単位。
成功・失敗とも検証VMを削除し、ログは`docs/sbx-model-validation.json`へ保存する。

## 本番切替前に残る作業

1. 実Discord BotとGitHubアカウント設定を行い、実レビュー・CI・人間承認まで通す。
2. 長時間の認証維持を確認する。Docker認証喪失の原因調査は継続課題。
3. 別案件へのアクセスやVM内特権操作を含む追加の侵入テスト、実機での強制終了後回収を確認する。
   現在、回収ジャーナルの再起動処理は自動テスト、通常キャンセルは実機で確認済み。

10役は役割定義として増やし、同時実行はまず1に制限する。
10個のVMを常駐させる必要はない。役数と同時VM数を分けることで、メモリとサブスク枠に合わせて運用できる。
