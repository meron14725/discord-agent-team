# Agentic company reference notes

最終確認日: 2026-09-12

この文書は、Discordを入口に複数のAIエージェントを運営する本基盤で、外部事例をどのように設計へ反映したかを残す。リンク先の構成をそのまま複製するのではなく、単一オーナー、ChatGPTサブスクリプション、案件ごとのGitHubリポジトリ、強い承認境界という現在の条件に合わせて採否を決める。

## 反映した原則

- Discordでは自然文で会話し、統括が返答、専門家への委任、正式案件、追加確認を判断する。日常会話を一律にタスク化しない。
- 委任先は独立したモデルターンとして考え、統括が専門家の回答を代筆しない。
- 会話と実行を分離する。読み取り調査や助言は自律実行し、コード変更、公開、権限変更、削除、課金は型付き計画と明示承認を通す。
- プロダクト方針と実行規約をリポジトリ内の文書として版管理し、エージェントへ毎回渡す。
- 長時間実行は状態、ログ、停止、再試行、再起動後の復旧を観測可能にする。
- 役割が増えても、人格ごとに常駐プロセスを増やさず、役割レジストリと上限付きワーカープールで処理する。特権を持つSRE系だけは実行経路を分離する。
- 統括の判断後、選ばれた専門家は最大3役まで別々のモデルターンとして並列実行する。
- 案件ごとに隔離されたworkspaceを使い、同時変更の衝突と影響範囲を制限する。

## 参照資料と採否

### [Claw-Empire](https://github.com/GreenSheep01201/claw-empire)

参考にした点は、部署と役割の登録、CEOからの自然な指示、案件ごとのgit worktree、稼働中エージェントの表示と停止、停止した実行の復旧、完了報告の保存である。今後の約10役への拡張は、これらを役割定義、実行スロット、案件状態、監査イベントとして実装する。

ゲーム風UIやピクセルオフィスは現在の検証目的に不要なため採用しない。認証情報をアプリ自身の汎用データストアへ集約する方式も採用せず、秘密ファイルと権限別プロセスを維持する。

### [Claude-to-IM Skill](https://github.com/op7418/Claude-to-IM-skill)

参考にした点は、IMをAIコーディングセッションへ接続する常駐daemon、会話セッションの永続化、Discord内の許可・拒否、ストリーミング表示、許可ユーザー・チャンネル・guildの制限、秘密のログ除去、`doctor`による診断である。本基盤でもDiscord Gatewayを会話入口にし、許可リストと内部API認証を維持する。承認UI、実行状態、診断と復旧は今後の運用機能に含める。

同リポジトリは単一エージェントとのIM橋渡しが主目的である。本基盤では複数の独立した役割、正式案件の状態機械、独立レビュー、CI確認が必要なため、橋渡し部分だけを参考にする。

### [Just Talk To It](https://steipete.me/posts/2025/just-talk-to-it)

参考にした点は、スラッシュコマンドや巨大な定型プロンプトを通常操作に要求せず、自然な会話で意図を伝えること、モデルが先にコードと文書を読むこと、小さな変更を並行させること、状況確認と中断を通常の運用にすることである。役名だけの人格付けではなく、参照文書、具体例、権限、受入条件を各役へ与える。

同記事の同じ作業ツリーへ複数エージェントを入れる運用は個人の高速な対話開発には有効だが、自動実行の安全境界とは合わない。本基盤の正式案件では隔離workspaceを使う。

### [Shipping at Inference-Speed](https://steipete.me/posts/2025/shipping-at-inference-speed)

参考にした点は、CLIから始めてエージェント自身が出力を検証できる閉ループを作ること、3〜8件程度の並行作業では人間の確認がボトルネックになりやすいこと、過度な一括仕様化より小さく作って触りながら更新すること、プロジェクト内文書を継続的なコンテキストにすることである。

このため、全会話を大型案件へ変換せず、最小の検証可能な成果へ絞る。並行数は無制限にせず、オーナーがレビューできる上限を設定する。

### [OpenAI Symphony orchestration specification](https://openai.com/index/open-source-codex-orchestration-symphony/)

参考にした点は、リポジトリ所有のワークフロー文書、型付き設定、候補案件の定期照合、上限付きdispatch、指数バックオフ、停止検知、再起動復旧、案件ごとのworkspace、構造化ログである。現在のPostgreSQL状態機械とDiscordイベントを残しつつ、リポジトリごとの実行規約、stale run検知、workspace安全条件を段階的に取り込む。

Symphonyは汎用の分散ジョブ基盤を目標にしていない。本基盤も10役程度の段階では、重い分散システムを導入せず、単一ホスト上の永続キューと上限付きワーカーを使う。

### [X: @ai_300 article](https://x.com/ai_300/article/2076177454159585677)

URLはユーザーから参考資料として受領した。2026-09-12時点ではXのログイン制限により本文を取得できなかったため、内容を推測せず、現時点の設計判断には使用していない。本文を確認できた時点で採否を追記する。

### [From SaaS Idea to Agentic Build](https://www.brainstron.ai/blog/from-idea-to-agentic-build-solo-founder-workflow)

参考にした点は、最初の顧客と課題を絞ること、狭いwedgeと中止条件、product memory、定義・実装・承認の担当分離、読み取り専用critic、最小の証拠ループである。これを `prompts/company-memory.md` と統括・上流の判断規則へ反映した。

### [n8n Discord integration](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.discord/)

Discordのチャンネル操作、メッセージ送信、待機、human-in-the-loopが外部連携に使えることを確認した。n8nは将来、スケジュール、通知、メール、カレンダー、CRM連携の端に置く。承認状態、GitHub発行権限、DB、Docker、Discord管理権限を持つ中核制御にはしない。

### [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

`codex exec`がスクリプトとCI向けの公式な非対話実行手段であり、保存済みCLI認証、sandbox、JSONL、JSON Schema出力を利用できることを確認した。本基盤のPython workerはユーザー入力をシェル文字列へ連結せず、stdinと固定argvでCLIへ渡し、型付きJSONを検証する。

### [Discord Gateway](https://docs.discord.com/developers/events/gateway)

Message Content intent、イベント購読の絞り込み、接続再開、レート制限を確認した。Discordのリアルタイム入口にはGatewayを使い、再接続はライブラリに任せ、アプリ側ではイベントの重複排除と永続状態を持つ。

## 現在の実装との対応

- 会話判断と独立した専門家ターン: `src/agent_team/adapters/codex.py`
- 型付きモデル契約: `src/agent_team/contracts.py`
- Discord Gatewayと実Bot発言: `src/agent_team/adapters/discord.py`
- 永続案件状態、承認、監査: PostgreSQL、orchestrator API、outbox
- 会社の共通知識と判断境界: `prompts/company-memory.md`
- runtimeと外部連携の境界: `docs/adr/0004-agent-runtime-and-integrations.md`
- sandbox方針: `docs/adr/0002-sandbox.md`
- Discord管理操作の安全方針: `docs/adr/0003-discord-sre-safety.md`

## 担当間引き継ぎの判断

すべての会話を統括LLMへ戻す構成にはしない。Claw-EmpireはCEO指示をサーバーとPlanningへ集約しつつ、部門協調と会議を持つ。Symphonyはdispatch、再試行、reconciliationを単一のauthoritative orchestratorが所有する一方、個々のagent runは隔離して自律実行する。Shipping at Inference-Speedは、複雑なmulti-agent orchestrationより人間がCodexのqueueを扱う簡素な運用を選んでいる。Solo Founder Workflowは順序と役割分離を示すが、全引き継ぎをPMエージェントが再判断する構成までは要求していない。

本基盤では、新規割当、再割当、同時実行数、権限、再試行を統括の制御層が一元管理する。専門家同士の相談や調査結果は型付き内部依頼で渡し、統括モデルによる再推論は曖昧さや競合がある場合だけ使う。DiscordのBot投稿を次のBot投稿のトリガーにはしない。これにより、不要な待ち時間とモデル使用を抑えながら、無限委譲、重複作業、権限迂回を防ぐ。

## 次に取り込む順序

1. 完了: host workerをlaunchd管理にし、再起動、ログ、health checkを安定化する。
2. 完了: 統括が選んだ専門家を最大3役まで、SRE分離を保って並列実行する。
3. 完了: 専門家から別担当への型付き引き継ぎを、統括の制御層で1段だけ実行する。自己委譲、既存担当への重複、2段目を拒否し、同じ追加担当への複数依頼は1実行へ統合する。
4. Discordへ実行状況、停止、再試行、承認を自然文とボタンで見せる。
5. リポジトリごとのチャンネルと話題ごとのスレッドを、安全なSRE計画から作れるようにする。
6. 役割レジストリで約10役へ増やし、現在の上限付きpoolへ割り当てる。
7. 案件ごとのworkflow文書、stale run照合、隔離workspaceの回収を追加する。
8. 必要になった外部業務だけn8n経由で接続する。
