# Codex sandboxの実現可能性調査

確認日: 2026-09-12。今回は資料と公式ソースの読み取りのみ。隔離設定の追加変更・製品インストール・モデル呼び出しはしていない。

## 結論

技術的には可能。OpenAI公式資料はDocker内でのCodex実行を説明し、内側sandboxを有効にする方式と、外側の隔離に委ねる方式を分けている。
ただし、現在のCompose設定と4 syscallの例外だけでは、Codex CLI 0.154.0の内側sandboxは起動できないことを実測済み。
「実現可能な方式がある」と「この固定版・このホストで本運用できることを実証済み」は区別する。
[OpenAI: Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)

## 今回の停止原因

Docker既定のseccompは名前空間操作やmountを制限する。CodexのLinux sandboxはbubblewrapで名前空間とマウントを準備するため、両者の設定が衝突する。
今回の実測は、既定設定では名前空間作成がEPERM、4 syscall許可後は名前空間作成に成功し、続くマウント伝播設定で失敗した。
設定の衝突という説明を裏付けるが、残る拒否原因をmountの1項目だけと断定するものではない。
[Docker: seccomp](https://docs.docker.com/engine/security/seccomp/)

## 方式の比較

| 方式 | 成立の根拠 | 今回の基盤への影響 |
|---|---|---|
| Docker内でCodexの内側sandboxも有効化 | OpenAIが必要な権限を備えたDev Container方式を説明 | workerのseccomp・capability・bwrap設置方式等を一体で再設計。4 syscallだけでの成立は未確認 |
| 専用VM上でCodexのLinux sandboxを利用 | Dockerによる二重の制限を避け、対応Linuxのsandboxを利用する設計 | workerの実行場所変更。Linux/カーネル/LSM設定の実機検証は必要 |
| Docker SandboxesのmicroVMを隔離境界にする | DockerがCodex対応と自動実行手順を公開 | 通常のComposeコンテナとは別製品。実行アダプターと成果物受け渡しを変更し、内側sandbox前提の検査も作り直す |

[OpenAIのDocker/Dev Container説明](https://learn.chatgpt.com/docs/agent-approvals-security)、[Docker Sandboxes概要](https://docs.docker.com/ai/sandboxes/)、[Codex対応](https://docs.docker.com/ai/sandboxes/agents/codex/)

## 公式Dev Container例の注意点

調査時点で、OpenAI公式説明がリンクする`.devcontainer/devcontainer.secure.json`と`Dockerfile.secure`はmainから削除され、直接取得は404だった。
GitHub履歴を確認すると、削除コミットは`f419c3214ab84ce6305c86f5e8f1b34475f1c611`（Remove the repository devcontainer configurations）。
履歴上の設定`740c4f269de8db915dde9d238ec0b8da6339aa56`では、SYS_ADMIN等のcapability追加、外側seccomp/AppArmorのunconfined、setuid bubblewrapを使っていた。Codexの指定版は0.121.0。

したがって、「公式例があるから、現在の0.154.0とcap_drop ALL/no-new-privilegesのままコピーすれば動く」とは言えない。
これは内側sandboxを動かす構成が設計されていた根拠であり、最新の本番推奨設定・本ホストでの検証成功を示すものではない。
資料のリンク切れを無視して古い構成の導入を勧めない。

## 今回の推奨

設計判断として、Discord/DB/承認制御は現在のComposeに残し、Codex実行部分を専用VMまたはDocker Sandboxesへ分ける方向を優先する。
エージェントの実行環境にホストのDocker socketや制御用資格情報を渡さず、信頼済みホスト側ランチャーでVMの起動・終了・成果物回収を管理する。
10役を増やす場合も、役数とVMの同時起動数を分ける。

Docker SandboxesはmicroVM方式で、Codex用テンプレート・OAuth認証・`sbx exec`による自動化経路が公開されている。
Codexテンプレートは既定でCodex内側のsandbox/承認を無効化し、外側の隔離に委ねるため、現runnerのフラグをそのまま持ち込む構成ではない。
今回要求される仕様承認・マージ承認は引き続き制御層が担当する。上流readonlyも外側のマウント・権限で別途保証する必要がある。
[DockerのCodex設定](https://docs.docker.com/ai/sandboxes/agents/codex/)、[自動化](https://docs.docker.com/ai/sandboxes/workflows/automation/)

このMacはmacOS 26.3.1 / arm64で、Docker Sandboxesの公表するmacOS 14以降・Apple siliconという前提を満たす。`sbx`はPATH上に見つからず、インストール・ログイン・実行は未実施。
LinuxサーバではUbuntu 24.04以降とKVM等の条件があり、全VPSで動作するわけではない。
[Docker Sandboxesの動作条件](https://docs.docker.com/ai/sandboxes/install/)

## 次の実証の出口条件

一つの方式と版を固定し、最小ジョブで起動、作業領域の書込み境界、ネットワークと秘密の分離、プロセス停止、成果物回収を検証する。
その後に本人のサブスクログインと実モデルの非対話出力、既存DBジョブとの接続を確認する。
この調査は次の方式を選べる根拠を整理したもので、起動問題の解消を報告するものではない。
