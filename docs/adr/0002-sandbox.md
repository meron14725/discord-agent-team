# ADR 0002: Docker内Codex sandboxのP0停止条件

確認日: 2026-09-12。Docker Desktop / arm64、Codex CLI 0.154.0、cap_drop ALL、no-new-privileges、非root。

## 確認結果

既定のbubblewrap起動は次のエラーで停止した。

```
bwrap: No permissions to create a new namespace
```

CLIの `use_legacy_landlock` をグローバルに指定するとecho単体は動いたが、workspace-write/read-onlyの書込み境界検査は失敗した。

```
permission profiles requiring direct runtime enforcement are incompatible with --use-legacy-landlock
```

そのため、Landlockへの切替も本実装には採用していない。
Codex sandboxの固定版での構文は `codex [global options] sandbox -- COMMAND`。
`codex sandbox linux ...` という古い構文をそのまま使うとlinuxをコマンドとして実行しようとする。

## 現在の動作

`CodexRunner`はモデル呼び出しより先に、workspace-writeで作業領域内の書込み成功と外側の書込み失敗、read-onlyで作業領域への書込み失敗を確認する。
この検査が通らないホストではBlockedとなり、モデルを呼ばない。

```sh
docker compose --profile live run --rm --no-deps upstream-worker agent-team preflight
```

mockデモとDB制御層はこの制約を受けない。ログイン・実モデル・サブスク利用枠の検証は別途必要。

## 承認された検証案と経緯

Docker標準seccompプロファイルの他の制約を保持したまま、`clone`、`clone3`、`unshare`、`setns` を許可し、非rootの内側sandboxに名前空間作成を認める案。
Docker socket、privileged、host network、追加capability、sandbox完全無効化は含めない。
ただし名前空間関連のカーネル機能を利用可能にするため、現在より攻撃面が増える変更である。

このプロファイルを外部から取得して作成するツール操作は、自動承認レビューに拒否された。
理由は「未検証の外部プロファイルを取得し、名前空間syscallを広く許可する永続設定を、明示的な許可なく作成して隔離を弱めるため」。
この最初の拒否時点では取得・ファイル作成・設定変更を実施しなかった。その後、ユーザーが4システムコールを許可する設定の検証を明示的に承認した。

承認を受け、以下の範囲で検証を実施した。
検証だけで本運用を開始しない。別案は、必要な名前空間機能が利用可能な専用Linuxホストで既定設定のP0を実施すること。


## 4システムコール許可の検証結果

**名前空間の作成には成功したが、Codex sandbox起動は未解決。既定のcompose.yamlには採用していない。**

| 検査 | 結果 |
|---|---|
| 既定Docker設定でuser/mount名前空間作成 | `EPERM` |
| 4 syscall許可で名前空間作成 | 上流・下流とも成功 |
| UID / NoNewPrivs / CapBnd / seccomp | 10001 / 1 / 0 / フィルター有効を両役で確認 |
| 制御API・DB・GitHub・Botの秘密マウント | 両役とも存在しないことを確認。秘密の中身は読んでいない |
| Docker socket / 制御configのマウント | 両役とも存在しない |
| Codex sandbox起動 | `bwrap: Failed to make / slave: Operation not permitted` |
| workspace-write / read-only境界 | 起動失敗のため未到達。合格とは扱わない |
| sandbox内のネットワーク制限 | 起動失敗のため未到達。既存の外向き通信制限未実装も継続 |
| モデル呼び出し / 本運用開始 | 行っていない |

bubblewrapは名前空間作成後のマウント伝播設定で止まっている。
固定したMobyプロファイルの`mount`許可は`CAP_SYS_ADMIN`条件付きであり、コンテナのcapabilityは全削除している。
これらの証拠から、4 syscall以外のマウント操作も現在の制限に阻まれていると判断する。
seccomp以外の制約も残り得るため、mountを追加すればすべて動くとまでは確認していない。
**今回、mount/umount等の追加許可やcapability追加は行っていない。**

### 成果物・再現手順

- `docker/seccomp-default.json`: Moby公式原本。Docker Desktopに内蔵された全ルールと同一であることまでは保証しない。
- `docker/seccomp-source.json`: 取得URL・コミット・原本SHA-256。
- `docker/seccomp-codex-four-syscalls.json`: 原本から4 syscallだけ変更した検証用設定。
- `scripts/build_seccomp_profile.py`: SHA検証付き再生成。
- `compose.sandbox-test.yaml`: 上下流workerだけに適用する、明示指定が必要な検証用差分。no-new-privilegesはbaseから継承。
- `scripts/probe_container_boundary.py`: 非root・制御秘密未マウント・名前空間作成の実機検査。
- `tests/test_seccomp.py`: 4 syscall以外に差分がないことと既定構成への未適用を検査。
- `docs/sandbox-validation.json`: 実行結果と設定hash。

```sh
python3 scripts/build_seccomp_profile.py
docker compose -f compose.yaml -f compose.sandbox-test.yaml --profile live config --quiet
docker compose -f compose.yaml -f compose.sandbox-test.yaml --profile live run --rm -T --no-deps upstream-worker python - < scripts/probe_container_boundary.py
docker compose -f compose.yaml -f compose.sandbox-test.yaml --profile live run --rm --no-deps upstream-worker agent-team preflight
```

最後のpreflightは、この検証環境では失敗するのが現在の記録である。
実験は`run --rm`の一時コンテナのみで実施し、常駐サービスやホストの設定は変更していない。
通常のコマンドから`-f compose.sandbox-test.yaml`を省けばDockerの既定seccompのまま使う。

### 次に必要な判断

本ホストで続ける場合は、マウント関連syscallの必要な範囲を調べ、さらに隔離設定を変更する案を別途レビュー・承認する必要がある。
今回の承認を、その追加変更への許可とは扱わない。
もう一つの選択肢は専用Linuxホストで、元のsandbox設定がそのまま機能するかP0を実施すること。

## 実現可能性の追加調査

[公式資料・公式ソース履歴を調べた結果](../sandbox-feasibility.md)、技術的に成立する方式は確認できた。
ただし、現在の4 syscallだけを許可した設定で起動したわけではない。公式の古いDev Container例はmainから削除されており、単純コピーの対象にしない。
今後は内側sandboxに合わせたコンテナ全体の再設計、または専用VM/Docker Sandboxesを隔離境界にする方式を比較する。

## Docker Sandboxes方式での実機検証へ移行

ユーザーの承認を受け、`sbx v0.42.1`を導入しDocker本人ログインを実施した。
ローカルmicroVMの起動と、内部の`codex-cli 0.149.1`のバージョン表示に成功。
これはこのADRの旧Compose設定が直ったことを意味しない。外側VMに隔離を担当させる別の経路である。
[今回の検証・原因説明・残課題](../runbooks/docker-sandboxes.md)を参照。
