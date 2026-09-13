# 検証結果と残作業

2026-09-13。**Issue #2のv2フローを`project` aliasで有効化した。DB schema v2、renderer、orchestrator、Discord gateway、host v2 worker（容量4）は稼働中。既存案件はworkflow v1のまま維持した。実案件の全工程受入試験はこれから行う。**

## 実行結果

- Pythonテスト: 173件成功。既存v1に加え、Issue正本、安全な要件変更案の反映・再照合、要件・計画の別承認、DB migration、役割台帳、内部相談、型付き引き継ぎ、説明renderer、秘密情報遮断、案件作業空間、per-task repositoryの作成順序、並列上限、repository lease、中止・失敗工程の再実行、30秒開始監視、実行中だけを対象とする10分滞留復旧、オーナー回答待ちの保護、同一guildのオーナーメッセージ参照、24時間確認通知、安全な担当fallback、Codex厳格schema、正式役割IDの会話worker認可、Draft PRからSquash mergeまでを検証。
- Ruff: 成功。テスト依存のStarlette/AnyIOに非推奨警告2件あり。
- Compose設定検査: 成功。
- 制御／ワーカーイメージのarm64ビルド: 成功。
- PostgreSQL 17.7 / orchestratorのhealthcheck: 成功。
- ネットワーク無効のDockerデモ: 模擬レビュー修正→人間承認→模擬マージ成功。
- 分離したPostgreSQL DBで同じ模擬フロー: 成功。
- PostgreSQLで第2制御プロセスのリーダーロック取得を拒否: 成功。
- ワーカー内 `codex --version`: `codex-cli 0.154.0`。
- **移行後の実モデル疎通:** 統括workerとv2専用workerからChatGPT OAuth経由で実行し、どちらもHTTP 200とschema適合結果を確認。
- **実sandbox:** 旧Compose内方式はbubblewrapとLandlockの制約で不採用。Docker Sandboxesをホストランチャーから使用し、実モデル、ジョブ、停止、役別workerを検証済み。[旧方式の詳細](adr/0002-sandbox.md)。
- **実Discord:** 統括、上流、下流、情シス・SREの4 Botを接続。専門役の並列会話と、SREによる変更提案→owner承認→チャンネル作成→after確認を実証。v2では通常最大4、SRE特権1へ制御を拡張したが実負荷試験は未実施。
- **専門家間引き継ぎ:** 実モデルで上流から下流への型付き依頼を確認。下流から上流への確認と回答を2往復行い、その後は下流が追加引き継ぎなしで最終回答するところまで検証済み。追加する専門役は1段に限定し、自己委譲、既存担当への重複、第三の担当への横流し、3往復目を制御層で拒否する。
- **規則管理:** 全役に優先適用する会社共通規則と、上流・下流・情シスSREの役割別規則を別ファイルで読み込み、欠落やサイズ超過時は起動を停止する。

## 元仕様との対応

| AT | 現在の証跡と残作業 |
|---|---|
| 01–02 導入 | macOS DockerでDB/制御、実Discord 4 Bot、Docker Sandboxes workerを起動。Linux amd64は未検証 |
| 03 要件整理 | モック質問契約・型検証と実モデル会話を確認。実案件の全工程E2Eは未完 |
| 04 仕様承認 | hash/actor固定・承認前実行拒否・repo作成時期をテスト |
| 05 PR | モックPR実在照合・publisher API実装。実GitHub未検証 |
| 06 修正 | 指摘ID引継ぎ・新レビュー・回数上限をモックで検証 |
| 07–09 CI/merge | 非success拒否、発行主体照合、人間ゲート、低リスクゲートをテスト。実マージ未検証 |
| 10 head更新 | 承認失効と再レビューをテスト |
| 11 base更新 | 古い統合結果を拒否。自動base統合と再開は未対応 |
| 12 イベント再送 | 同一依頼10回で案件・ジョブ1件 |
| 13 外部応答喪失 | publish成功後の応答喪失からPR重複を避ける障害注入テスト |
| 14 古いワーカー | lease期限切れ・fence更新後の結果拒否 |
| 15 Botループ | Bot入力拒否、通知はジョブ起動条件にしない |
| 16 不正承認 | actor/guild/channel/bot、古い版、期限切れSHA承認拒否 |
| 17 仕様変更 | 旧ジョブ失効・版更新・承認取り直しをテスト |
| 18 上限 | API予約予算・サブスク実行回数・時間停止をテスト。プロバイダー実利用枠未検証 |
| 19 保護変更 | CI/制御/秘密/指示ファイル変更拒否、許可パスと量のゲート |
| 20 停止 | 停止後結果拒否・子プロセス停止。GitHub実通信中の停止は未検証 |
| 21 復元 | backup/restore手順・スクリプト。別ホスト復元未実施 |
| 22 不正結果 | Pydantic schema・identity・AC evidence検査。自由文からの成功推測なし |
| 23 外部操作 | 手動merge/closeのモック照合と外部マージ監査 |
| 24 権限 | Composeで秘密・ネットワーク分離、symlink拒否。実sandboxと厳密egressは未達 |

## 既知の未対応事項

- 役割台帳は初期8役、10役以上へ拡張可能。Discordへ接続済みなのは4 Botで、フロントエンド・UX、QA、評価管理、調査・分析のtoken作成と一役ずつの実接続試験が残る。
- Issue #2のv2は`project`で有効。GitHub Issue PATCHは条件付き更新に非対応だったため、要件案をIssueコメントへ提示し、オーナーが本文へ反映後に保存済みhashを再照合する。非公開test repositoryでAC-01〜AC-17の実測が残る。
- 説明rendererは資格情報なし・内部networkのみのComposeサービスとして構成済み。実コンテナでのHTML/PNG添付確認が残る。
- 実DiscordとCodexログインは検証済み。GitHubの実案件全工程、CIと保護ルールの契約プラン上の適用可否は未検証。
- 新repoのCI・ブランチ保護・レビューApp導入の自動設定。テンプレートとホスト側運用が必要。
- base自動統合、CI障害のコード/インフラ自動分類、CI失敗からの自動修正。
- GitHub App token自動更新、精密な料金推計、公式サブスク残枠の自動取得。
- 長期停止時のDiscord全履歴回収。直近100件の補完と `/answer` を提供。
- 完全な副作用exactly-once、Discord通知の完全な重複排除、別DB複製間の二重稼働防止。
- バイナリ・大規模repo・symlink・submodule。ソース／成果物は初期100ファイル・2MB上限。
- 監査・ログの保持期限削除の自動化、eject/egress制限、悪意あるコードからモデル認証を隠す認証プロキシ。
