# ADR 0003: Discord情シス・SREの管理権限

## 決定

情シス・SREはDiscordの構成を診断し、許可された変更を実行できるようにする。
ただしLLMとDiscord Gatewayへ管理権限を直接公開しない。Botトークンは信頼済み制御層だけが保持し、
SREエージェントは現在状態と監査済み診断情報から型付き変更案を返す。

「絶対安全」は保証しない。単一の誤判定、プロンプト注入、古い承認、対象IDの取り違え、
Botトークン漏えいのいずれか一つだけでは重要変更が成立しないことを目標にする。

## Discordで付与する権限

導入時は表示・報告用の基本権限だけを付与する。

- View Channels
- Send Messages
- Send Messages in Threads
- Read Message History
- Attach Files

管理実行機能の受け入れ試験後、次を個別に追加する。Administratorは付けない。

- View Audit Log
- Manage Server
- Manage Channels
- Manage Roles
- Manage Messages
- Manage Threads
- Create Public Threads / Create Private Threads

Kick Members、Ban Members、Moderate Members、Manage Webhooks、Mention Everyone、
Create Invites、音声メンバー操作は初期対象外とし、付与しない。

SRE Botの最高ロールは人間のowner/adminロールより下、統括・上流・下流Botのロールより上に置く。
Discordのロール階層により、Manage Rolesを持ってもBotの最高ロール以上は管理できない。
人間owner/adminロール、SRE自身のロール、general、監査チャンネルを保護対象IDとして設定する。

## リポジトリごとのチャンネル構成

最終形ではgeneralを会社の受付ロビーとして残し、統括が新規相談を受ける。
仕様承認後にリポジトリを作成または選択した時点で、情シス・SREが管理対象カテゴリ内に
リポジトリ専用テキストチャンネルを作る。案件や話題ごとに、そのチャンネル内へスレッドを作成する。

```text
general                         受付・統括との会話
projects/
  project-task-001              リポジトリ専用チャンネル
    TASK-001 機能追加           案件スレッド
    TASK-002 不具合修正         案件スレッド
  another-repository
    TASK-003 調査               案件スレッド
operations/
  sre-audit                     SREの提案・承認・実行結果
```

チャンネル名からリポジトリを推測しない。DBにguild ID、repository full name、repository ID、
channel ID、category ID、作成操作IDを対応づけ、GitHub側の実在とownerを再確認する。
同じリポジトリへの同時作成は一意制約と冪等キーで1チャンネルにする。
統括は投稿されたchannel IDからリポジトリを解決し、専門Botへ同じ案件スレッドを共有する。

チャンネル新規作成と案件スレッド作成は、管理対象カテゴリ内・既定権限テンプレートどおりの場合だけ
低リスク操作として自動化できる。カテゴリ移動、権限上書き、リポジトリとの対応変更、renameは
before/after確認を行う。チャンネル・カテゴリ・スレッドは自動削除せず、リポジトリのarchive時も
読み取り専用化またはarchiveカテゴリへの移動に留める。generalと監査チャンネルは常に変更対象外とする。

## 実行ゲート

変更処理は次の順序を必須にする。

1. Discord APIから対象と現在値を取得する。
2. SREが型付き変更案、理由、影響、確認方法、ロールバック案を返す。
3. 制御層がguild ID、対象種別、許可フィールド、保護対象、権限bit、ロール階層を検査する。
4. 変更前スナップショット、変更案、対象IDから承認hashを作る。
5. 重要変更はDiscordへbefore/afterを表示し、ownerの期限付き承認を待つ。
6. 実行直前に現在値を再取得し、承認時から変化していれば承認を失効する。
7. Botトークンを持つ制御層が一度だけAPIを呼び、X-Audit-Log-Reasonへtask IDを入れる。
8. APIから状態を再取得して結果を確認し、DBへ監査記録とロールバック材料を保存する。

低リスクで自動実行できる初期操作は、管理対象カテゴリ内でのチャンネル作成、
管理対象チャンネルのtopic更新、スレッドのarchiveに限定する。
チャンネル・カテゴリ・ロールの削除、権限overwrite、ロール権限、サーバー設定は必ず人間承認とする。

次は常時禁止する。

- Administratorの付与またはAdministratorを含むロールの操作
- 人間owner/admin、SRE自身、保護対象IDの変更
- guild所有権、Bot token、Developer Portal、アプリ所有チームの変更
- Webhook作成・変更、外部招待、メンバーのkick/ban/timeout
- 承認した対象以外への一括適用、自由形式のDiscord API URL・JSON実行

## 障害時

Discord APIが429、5xx、タイムアウトの場合、GETと冪等な低リスク操作だけを限定再試行する。
書き込み応答を失った場合は再送前に現在状態と監査ログを照合する。
403、対象消失、ロール階層変化、承認hash不一致はBlockedとし、権限を自動拡張しない。
ロールバックも新しい変更として同じ検査を通す。

## 導入順序

1. 完了: 基本権限でSRE Botを追加し、読み取り・報告だけを検証する。
2. 完了: 型付き変更schemaとポリシー検査を実装し、自動テストを行う。
3. 専用の検証カテゴリ・検証チャンネルで作成、更新、競合、承認失効、復元を実証する。
4. 監査記録を確認後、必要な管理権限だけを追加する。
5. 実サーバー設定はhuman gateのまま開始する。

Discord公式仕様ではManage Serverがサーバー設定変更、Manage Channelsがチャンネル作成・編集・削除、
Manage RolesがBotの最高ロールより下のロール管理を許可する。
[Server and Channel Management](https://docs.discord.com/developers/platform/server-and-channel-management)、
[Guild Resource](https://docs.discord.com/developers/resources/guild)を基準にする。
