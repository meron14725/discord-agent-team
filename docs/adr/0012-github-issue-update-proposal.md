# ADR-0012: GitHub Issue本文は変更案を経由して安全に更新する

- 状態: 採用
- 日付: 2026-09-13

## 背景

要件の正本であるGitHub Issue本文をエージェントが更新すると、人間が同時に編集した内容を失う可能性がある。当初はGETで得たETagを`If-Match`付きPATCHへ渡す楽観ロックを計画した。

実GitHubで専用Issueを使って検証したところ、正しいETagを付けたIssue更新もHTTP 400になった。GitHub公式文書も、個別endpointに明記がない限りPATCHなどの変更系リクエストで条件付きrequestをサポートしないとしている。Issue更新endpointには対応の記載がない。

## 決定

GitHub.comでは`github_issue_conditional_updates: false`を維持する。CTOは要件本文の変更案とhashをIssueコメントへ投稿し、オーナーへDiscordでメンションする。オーナーが変更案をIssue本文へ反映して再試行すると、制御層は保存済みモデル結果のhashと現在のIssue本文を照合する。一致した場合だけ説明成果物と要件承認を作る。

変更案の不一致、別編集、古い再試行は停止する。制御層は人間のIssue本文を無条件に上書きしない。

## 結果

初回の要件確定にオーナーのIssue本文反映が1操作増える。一方、要件正本と人間の編集を失わず、同じモデル呼び出しを繰り返さずに再開できる。将来GitHubがIssue更新の原子的な版条件を公式提供した場合だけ、自動更新を再検討する。

## 参照

- https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests
- 実環境preflight: `https://github.com/meron14725/discord-agent-team/issues/3`（検証後にclose）
