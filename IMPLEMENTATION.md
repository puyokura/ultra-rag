# ultra-RAG 実装状態

この文書は実装・検証の記録である。製品仕様の正本は `architecture.md`、作業手順は `AGENTS.md`、使い方は `README.md`、旧READMEは `README.original.md` として内容を変更せず退避している。

検証日: 2026-10-30
検証ホスト: Windows 11 / Python 3.13.14 / Node.js 24.14.1 / dsh 0.2.1-rc.2、Debian 13 (`agent`) / Python 3.13.5 / systemd user / dsh 0.2.1-rc.1
未検証: 実 sudo / 実 SSH パスワード認証、実外部モデル・実資格情報、再起動をまたぐ常設運用、Linux の実行主体分離

## 1. 実装した範囲

### RAG（`rag/`, `contracts/`）

- 可読 Markdown の正本を `revisions/<n>/` に生成し、全ファイルの SHA-256 を含む manifest と `control/current.json` の原子置換で公開する。各読み取りは manifest とファイル内容を照合し、直接編集や部分書き込みを確定改訂として扱わない。
- 記憶の作成・更新・旧版化（`superseded`）・忘却・記録削除・候補の確認を実装した。削除は系譜で連結した版、候補、対象抜粋を追跡して除去し、本文を含まない tombstone（ID・出典指紋・改訂）を残す。
- 同じ `update_id` の再送は同じ結果を返し、同一 ID で内容が異なる要求は拒否する。対象が提案の基底改訂より後に変わった更新は競合保留にし、最後の確定内容を維持する。
- プロフィールと禁止・必須事項は決定論的に生成する。検索上位件数に依存せず、全採用済み禁止・必須を適用範囲つきで出力する。忘却・旧版・削除済みは含めない。
- 一往復の書き溜めは、原文の連続範囲と完全一致する抜粋だけを保存する。一致しない範囲は拒否し、途中切り捨てをしない。AI 抜粋がない場合は段落単位の構造選択を使い、抽出メモであることを明示する。秘密パターンは保存前に `[REDACTED]` へ置換し、編集済みとして原文一致と区別する。利用者が「覚えて」と明言した段落は、モデルのツール呼び出しを待たずに明示登録として採用する（案件限定の明言は案件範囲、今回限りは自動昇格しない）。
- SQLite FTS5（trigram）と LIKE フォールバックによる日本語部分一致検索、個人・共通・現在案件の範囲制御、候補の既定除外、索引の再構築を実装した。
- ループバック HTTP API（Bearer トークン）と MCP stdio サーバー（記憶 15 ツール、キー 3 ツール）を実装した。記憶の出典表示（`memory_explain`）と、自動検索とは分離した過去セッションの明示参照（`memory_explicit_reference`）を含む。
- PC 複製向けに差分同期（変更ファイル・削除パス・manifest）、未同期キュー、通信断、競合保持、削除伝播を実装した。
- dsh の `session.v4.jsonl.zstd` を取り込み、台帳で再実行を抑止し、古い方針は確認待ち候補にする。subagent セッションは理由つきで除外する。
- dsh Cordis プラグイン（`rag/dsh-plugin/index.mjs`）を実装した。`agent/pre-step` で確定プロフィール・全適用条件・関連記憶・更新通知を注入し、`session/event` の `turn/end` で抜粋保存フックを起動する。

### 認証支援（`secret-core/`, `key-use/`, `password-input/`）

- Windows DPAPI（ユーザー結び付け、アプリ固有 entropy）で保管庫のマスターキーを封緘し、各キー値は AES-GCM で個別暗号化する。一覧・状態照会はメタデータだけを返し、値を返す API は設けていない。
- 端末登録・トークン認証・端末停止、キー削除・差し替えの失効制御、秘密を含まない監査（開始記録の保存失敗時は新規供給を拒否）を実装した。
- 選択キーだけを子プロセス環境へ一時供給し、専用作業領域、非管理者制約、ホスト指定、単発時間上限、同時実行数上限（`secret.max_concurrent`、既定4）、プロセスツリー停止、既知値マスキング、成功時だけの成果物取り込み（上書き・部分反映）を実装した。
- 実行ごとに独立監視プロセスを起動し、利用許可の 5 分期限、キー削除、キー差し替え、端末停止、所有者停止を監視して対象プロセスを停止する。管理プロセスの終了だけでは監視は止まらない。
- 再起動世代を区別し、旧世代の実行 ID は照会不能・再実行しない。同一 ID・同一内容の再送は既存状態を返し、同一 ID・異内容は拒否する。
- sudo は標準入力、SSH は `SSH_ASKPASS` 経由でパスワードを一回の要求にだけ渡す。要求は 5 分で失効し、使用前に消費済みにして再利用できない。認証区間の生出力は返さず、状態と非秘密の診断だけを返す。SSH ホスト鍵は専用 known_hosts と記録で管理し、scan / check / accept を提供する。
- systemd の `ExecStart` から使う前景ランチャー `secret_core.keyrun` を実装した。選択キーだけを供給し、終了状態を systemd に返し、監視・失効・停止は通常の実行と同じ経路を使う。unit の例と方針は `secret-core/systemd/` に置く。unit の登録・enable は行わない。Debian 実機では `systemd-run --user` の一時ユニットで供給と終了を実測した。
- 利用許可の 5 分期限を維持する `secret_core.lease_daemon` を実装した。管理チャネルとして動作中だけ lease を更新し、停止すれば期限切れで新規供給を拒否する。Debian 実機の一時ユニットで lease 更新を実測した。

## 2. 実行入口（実測済み）

```powershell
# 初期化（サーバー役）
bin\ultra-rag.cmd --home C:\path\to\home init

# 記憶
bin\ultra-rag.cmd --home C:\path\to\home rag remember "実測を優先する" --kind design_principle
bin\ultra-rag.cmd --home C:\path\to\home rag search "実測"
bin\ultra-rag.cmd --home C:\path\to\home rag snapshot --query "実測" --text
bin\ultra-rag.cmd --home C:\path\to\home rag verify

# 過去セッション取り込み（既定は ~/.dsh を読む）
bin\ultra-rag.cmd --home C:\path\to\home rag import --dsh-home C:\Users\<user>\.dsh --dry-run

# HTTP / MCP
bin\ultra-rag.cmd --home C:\path\to\home rag serve --host 127.0.0.1 --port 8765
python scripts\run_mcp.py --home C:\path\to\home

# 実パス入りの dsh overlay を生成
bin\ultra-rag.cmd --home C:\path\to\home rag overlay --output C:\path\to\ultra-rag.overlay.yml

# キー（値は argv に書かず隠し入力。--value-stdin は所有者の明示操作）
bin\ultra-rag.cmd --home C:\path\to\home secret register --name example --service demo --purpose test
bin\ultra-rag.cmd --home C:\path\to\home secret exec --command "..." --key key_... --timeout 60

# パスワード（端末で隠し入力）
bin\ultra-rag.cmd --home C:\path\to\home password request --adapter ssh --host <host> --account <user> --command "true"
bin\ultra-rag.cmd --home C:\path\to\home password hostkey scan --host <host>
bin\ultra-rag.cmd --home C:\path\to\home password hostkey accept --host <host> --fingerprint SHA256:...
bin\ultra-rag.cmd --home C:\path\to\home password provide pw_...
```

`--home` はサブコマンドより前に置く。省略時は `ULTRA_RAG_HOME`、なければ OS のアプリデータ領域を使う。

### dsh への接続

`rag/dsh-plugin/overlay.example.yml` の `<REPO>` と `<HOME>` を置換し、既存プロファイルを変更せずに overlay として渡す。

```powershell
dsh --profile <profile> --patch <overlay.yml>
```

検証では実 dsh 0.2.1-rc.2 の headless プロファイルを起動し、プラグインの文脈注入、MCP ツール呼び出し、`turn/end` の抜粋保存まで確認した。モデルは外部 API ではなく、テスト用の OpenAI 互換エンドポイント（`tests/fake_llm_server.py`）を使った。実モデル・実資格情報での接続は未検証である。

headless プロファイルは `{{cwd}}` を未登録変数として参照して起動に失敗するため、検証 overlay では `system-prompt` を最小構成に上書きした。これはリポジトリの実装ではなく、手元プロファイルの既存設定に起因する。

### 同期

サーバー側の `control/api-token` をクライアントの `control/sync-token` として所有者が配置し、クライアントは `init --role client` の後で次を実行する。

```powershell
bin\ultra-rag.cmd --home <client> init --role client
bin\ultra-rag.cmd --home <client> rag sync pull --remote http://<server>:8765 --token-file <client>\control\sync-token
bin\ultra-rag.cmd --home <client> rag sync once --remote http://<server>:8765 --token-file <client>\control\sync-token
```

## 3. 試験と実測結果

```powershell
python -m unittest discover -s tests -t .
```

2026-10-30 時点の結果: **120 tests / OK（Windows専用2件skip）**。

| 試験群 | 実測した内容 |
| --- | --- |
| 正本・改訂 | 原子公開、公開失敗時の旧改訂維持、manifest 改ざん検出、更新 ID 再送、競合保留、旧版保持、削除の連結版・抜粋除去、tombstone による再取り込み拒否 |
| 人物像・範囲 | 全禁止の注入、候補の非政策扱い、忘却・削除の除外、案件境界、2 文字日本語検索、索引再構築、通知の一度だけ表示 |
| 書き溜め | 原文完全一致の検証、不一致拒否、段落単位の構造選択、同一ターンの冪等性、秘密除外の編集済み表示 |
| 取り込み | v4 zstd の実ファイル生成、台帳スキップ、subagent 除外、秘密の redaction、候補の確認前非注入 |
| 同期 | 初回全量、差分、未同期提案、通信断、復帰、競合保持、削除伝播、削除済み出典の再登録拒否、HTTP 認証・並列照会 |
| 保管庫・端末 | 平文非保存、AES-GCM、DPAPI 封緘、鍵差し替え、削除、端末停止、改ざん封緘の復号拒否 |
| 実行 | 選択キーのみ供給、非管理者、ホスト不一致、監査開始失敗時の拒否、期限切れ拒否、時間切れ停止、端末停止・キー削除・差し替え・許可期限による停止、独立監視、旧世代 ID、重複要求、成果物の成功時のみ取り込み・上書き・部分失敗 |
| パスワード | 5 分失効、一回消費、キャンセル、sudo の一回入力、SSH askpass 経路、ホスト鍵未受諾の拒否、専用 known_hosts、変更検出、生出力の非返却 |
| 接続 | 実 MCP stdio プロセス（初期化・ツール一覧・実行・EOF 正常終了）、Node プラグインハーネス、実 dsh headless 起動 + 実 MCP ツール呼び出し + turn/end 保存 |

### architecture 22.3 の受入条件との対応

| 条件 | 状態 | 根拠 |
| --- | --- | --- |
| 管理と利用（AI にキー本体の管理権限なし） | 実装 | 値を返す API・MCP ツールなし。登録・差し替え・削除は所有者 CLI の隠し入力のみ |
| 一覧と選択供給 | 実測 | `test_secret_exec.py` のメタデータ非漏えい・選択キーのみ供給 |
| 通常 dsh との分離 | 一部・未検証 | 値は子プロセス環境だけ。OS 権限による別プロセス遮断は未検証 |
| 並行実行の隔離 | 一部・未検証 | 実行ごとの作業領域・環境を分離。同一ユーザーの別プロセスからの遮断は未検証 |
| 実行権限（管理者へ昇格しない） | 実装・非管理者で実測 | 起動前に昇格を検出して拒否。昇格済み環境での拒否は未実行 |
| 作業領域 | 実測 | 実行ごとに別ディレクトリ、入力は専用領域へコピー |
| 成果物の成功条件 | 実測 | 終了コード 0 のときだけ取り込み。失敗・時間切れは取り込まない |
| 成果物の競合 | 実測 | 既存の取り込み先を確認なしで上書き |
| 複数成果物 | 実測 | 取り込めるものだけ反映し、失敗を列挙 |
| ホスト選択 | 実測 | 不一致は未実行の `rejected` |
| 未実行と不明 | 実測 | `rejected` と旧世代 `unknown` を区別 |
| 重複要求 | 実測 | 同一 ID・同一内容は既存状態、異内容は拒否 |
| 再起動後の ID | 実測 | 世代変更後の旧 ID は `unknown`、再実行しない |
| 削除・差し替え | 実測 | キー削除・差し替え・端末停止で対象実行を停止 |
| 端末認証 | 一部 | 登録端末の認証・停止を実測。全端末で全キーの遠隔利用は未接続 |
| 許可期限 | 実測 | 期限切れの新規拒否と既存停止を実測 |
| 管理処理の異常終了 | 一部 | 独立監視を実プロセスで実測。管理死亡中の継続は監視単体で担保 |
| 独立監視の停止 | 実測 | 監視を殺すと管理側が子プロセスを停止することを実測 |
| 単発時間上限 | 実測 | 時間切れ停止、延長・再実行なし |
| 常駐と自動起動 | 実装 | 常駐は会話終了後も監視。自動起動は実装せず systemd 側に残す |
| systemd 連携 | 常設サービスで実測 | `ultra-rag-http.service` / `ultra-rag-lease.service` を enabled・active にし、LANバインドとlease更新を確認。ホスト再起動後の自動復帰は未検証 |
| 保存時保護 | Windows で実測 | DPAPI 封緘、改ざん復号拒否。Linux は未実行 |
| 監査障害 | 開始を実測 | 開始記録失敗時に新規拒否。終了記録失敗時の表示はコード上のみ |
| パスワード入力 | 経路を実測（テスト用 CLI） | 一回消費・非保存・生出力非返却。実 sudo/SSH は未検証 |
| 認証待ち | 失効・取消を実測 | 5 分期限、キャンセル、接続断時の取消は未検証 |
| 認証キャッシュ | 未検証 | 本機構は CLI のキャッシュを変更しない実装だが、実 CLI で未確認 |
| SSH の受け入れ | 経路を実測（模擬 scan） | 専用 known_hosts、scan/check/accept、変更検出。実ホスト未検証 |
| 認証区間 | 実測（模擬 CLI） | 生出力を返さず状態・分類だけを返す |
| 漏えい保証の限界 | 文書化 | 任意コマンド・通信先無制限の限界を本表と実装に明記 |

### 実 dsh での証拠

`tests/test_dsh_real_harness.py` は次を一つの実行で確認する。

1. 既存プロファイルを変更せず `--patch` でプラグインと MCP サーバーを挿入する。
2. `agent/pre-step` が「必ず実プロセスで検証する」を含む記憶ブロックをモデル要求の messages に追加する。
3. スクリプト化したモデルが `mcp__ultra-rag__memory_remember` を呼び、MCP サーバーが記憶を採用済みで永続化する。
4. `turn/end` 後にプラグインが Python フックを起動し、セッション抜粋が保存される。

これは dsh の実起動・実 MCP 通信・実プロセスフックの証拠であり、外部モデルの品質や実資格情報の接続証拠ではない。

### Debian 実機での検証（2026-10-30）

SSH 接続先 `agent`（Debian 13、Python 3.13.5、systemd user、systemd-creds）で以下を実測した。

- コード配置: `/home/agent/ultra-RAG`、venv: `/home/agent/ultra-RAG/.venv`
- 本番ホーム: `/srv/ultra-rag`（`700`、所有者 `agent`）。旧検証ホームは残しているが正本ではない。
- 常設サービス: `ultra-rag-http.service` と `ultra-rag-lease.service` を systemd system service として `enable --now` し、active を確認した。HTTPは `192.168.1.200:8765` のLANアドレスだけにバインドし、`0.0.0.0` 公開とポート転送は行っていない。PCから `healthz` と認証付き同期を実測した。
- サーバー側の全試験: **120 tests / OK（Windows専用2件をskip）**
- 保管庫: `systemd-creds --user` で封緘・解錠する `systemd-creds` 保護を実測。キー登録・一覧・供給・マスキング・監査を実行した。
- systemd: `systemd-run --user` の一時ユニットから `secret_core.keyrun` を起動し、選択キーだけの供給、終了コードの返却、マスキング済み出力を確認した。同じく一時ユニットで `lease_daemon` を起動し、lease が更新されることを確認した。その後、常設の system service として `ultra-rag-http.service` と `ultra-rag-lease.service` を enabled / active にした。ホスト再起動をまたぐ運用は未検証。
- PC↔鯖同期: PC側に `--role client` の一時ホームを作り、SSH ポート転送越しの HTTP で `sync pull` / `sync once` を実行した。サーバーの記憶をPCへ反映し、PCの提案をサーバーで採用し、サーバー削除をPCへ伝播（tombstone と検索除外）した。
- MCP の依存: サーバーで `mcp==2.3.0` が導入され FastMCP API と非互換になったため、`pyproject.toml` を `mcp>=1.0,<2` に固定し、`mcp==1.30.0` で再試験した。
- 実モデル接続: サーバーの headless プロファイルは `{{cwd}}` 未登録変数で起動に失敗し、`system-prompt` を上書きしても、既定プロバイダ `antigravity` の資格情報がサーバーに存在しなかった。実資格情報での実モデル接続は未検証のままである。dsh の実起動・実 MCP 呼び出し・turn 保存はテスト用モデルエンドポイントで確認済み。

以上の検証で使用したテストキー・テスト記憶・一時ユニット・一時クライアントホームは削除した。本番ホームは `/srv/ultra-rag`、PCクライアントは `%LOCALAPPDATA%\ultra-rag` として初期化・同期確認済み。実 sudo / 実 SSH パスワード認証、実外部モデル、ホスト再起動をまたぐ運用、Linux での権限分離は未検証のままである。

## 4. 未実装・未検証・残る U 項目

部分完了を全体完成として扱わない。以下は未検証または未実装である。

### 未検証ホスト・接続

- ホスト再起動後の常設サービス復帰は2026-10-30に実測済み（3サービスactive、healthz 200、旧exec_idはunknown、旧プロセスなし）。長期運用は未検証。
- 公開ネットワーク・TLS・ファイアウォール越しの HTTP 配備。PC↔鯖のホスト鍵同期は実HTTPで検証済みだが、インターネット公開構成は未検証。
- 実sudoアダプターは実sudo（NOPASSWD）とPTY隠し入力で `authenticated` を確認したが、これはパスワード検証の証拠ではない。実SSHは実ホスト鍵のaccept・選択・一時known_hosts・誤パスワードの `authentication_failed` まで確認したが、正しいパスワードでの成功は未検証。
- 外部 AI モデル / 実資格情報での dsh 接続。検証はテスト用モデルエンドポイントのみ。
- OS レベルの実行主体分離。別実行の秘密ファイル・環境・プロセスへの直接アクセスを OS 権限で遮断する実装は未検証であり、現状は専用ディレクトリ・環境・プロセスツリーの分離に留まる。同一ユーザーの別プロセスから作業領域を読める範囲が残る。

### 実装済みU項目と残る設計項目

- 意味検索・ローカル埋め込みは任意機能として実装済み。既定はliteral検索（FTS5 trigramとLIKE）のみで、`rag.embedding_enabled` を有効にした場合だけruri-v3-310mを併用する。
- Markdown 直接編集の自動取り込み（U1）は実装済み。詳細は第7章。
- 専用領域の入力・成果物・常駐回収（U2）は実装済み。詳細は第7章。
- マスキング失敗時の安全な応答（U3）は実装済み。詳細は第7章。
- 端末トークン+exec_idの入力紐付け（U4）は実装済み。詳細は第7章。Web/GUIの入力画面は作らない決定のまま。
- `max_concurrent`、端末限定の照会・停止、冪等停止（U5）は実装済み。CPU/メモリ上限はOSへ委ねる。
- ホスト鍵の併存・接続時選択・追記専用・鯖からPCへの同期（U6）は実装済み。
- 保管庫再作成の無効化・archive・監査保持（U7）は実装済み。
- 候補50件/頁、却下保持、抜粋の明示的超過、プロフィール重複排除（U8）は実装済み。
- バックアップ保持期間・削除・復元時の削除制御再適用。保管庫の `backup_vault()` は封緘ファイルの複製のみで、運用設計は未完了。
- 監査履歴のエクスポート、保持容量管理、本人による部分削除。
- 成果物への秘密混入検出。architecture が保証しないとしている範囲のまま。
- キー供給のリモート API。MCP のキー操作は同一ホストのローカル実行のみで、端末認証を伴うネットワーク越し供給は未接続。

### 制約・保証しないこと

- キーを供給した任意コマンドは値を読める。マスキングは誤出力の低減であり、加工・暗号化・外部送信による漏えいを防がない。
- ファイル削除・監査削除は、発行元での失効や外部へ送信済みの値の回収ではない。
- 常駐実行は管理プロセス終了後も監視プロセスが期限・失効を監視するが、監視も停止した場合の強制停止は保証しない。
- DPAPI は現在の Windows ユーザーに結び付く。別ユーザー・別ホストへ封緘ファイルを移すと復号できない。Debian では `systemd-creds --user` の封緘・解錠を実測済みだが、systemd 常設運用と権限分離は未検証。
- `file-dev` 保護は隔離した開発専用で、`ULTRA_RAG_ALLOW_INSECURE_KEYSTORE=1` がない限り拒否する。本番の代替ではない。

## 5. U 項目と実装の依存

| U | 現在の扱い |
| --- | --- |
| U1 直接編集 | 実装済み。編集は提案として検証・競合を通り、採用まで非公開。 |
| U2 成果物 | 実装済み。明示入力・明示成果物、原子的置換、失敗時保持、常駐の明示回収。 |
| U3 マスキング | 実装済み。失敗時は `masking_failed` で生出力・成果物を出さない。 |
| U4 入力画面 | 端末の隠し入力のみ。端末トークン+exec_idで紐付け、応答喪失時は状態照会のみ。 |
| U5 同時実行 | 実装済み。`max_concurrent`、端末限定、冪等停止。資源上限はOSへ委譲。 |
| U6 ホスト鍵 | 実装済み。host:port+鍵種別、併存、接続時選択、追記専用、鯖→PC同期。 |
| U7 保管庫再作成 | 実装済み。archive・無効化・再登録・監査保持。 |
| U8 候補運用 | 実装済み。50件/頁、却下保持、抜粋超過の明示、プロフィール重複排除。 |

## 6. D0 で確認した実在の接続点

- dsh 0.2.1-rc.2 の Cordis ホストプラグイン。`dsh-rules` と同じ `agent/pre-step` でメッセージを差し替えられること、`session/event` で永続イベントを購読できることを実測した。
- `@deepseek-ai/dsh-mcp-client` の stdio 接続。公開ツール名は `mcp__<serverName>__<rawName>`。
- `session.v4.jsonl.zstd` のヘッダと `user/message`、`assistant/message`、`turn/start`、`turn/end` の形式。`zstandard` で読み取り可能。
- dsh の overlay は `--patch` で既存プロファイルを変更せず追加できる。
- 実資格情報は環境変数・資格情報サービスにあり、本実装は値を読まない。外部モデル接続の検証には別途、利用者自身の資格情報設定が必要。

## 7. U項目の決定と実測

2026-10-30 の利用者決定に従い、U1〜U8をこの節に記録する。決定は仕様の代替ではなく、architecture の未決定事項に対する実装上の確定である。部分完了は完了と書かない。

| ID | 決定 | 実装状態 |
| --- | --- | --- |
| U1 | 直接編集の自動取り込みを有効化。IDと基底改訂を持つ編集だけを更新提案へ変換し、既存の検証・競合を通す。編集中は非公開。構造破壊とファイル削除は採用しない | 実装・サーバー実測・回帰7件 OK |
| U2 | 明示パスのみ。ディレクトリ再帰、リンク非追跡、1ファイル原子的置換、失敗時保持、常駐成果物の明示取り出し | 実装・サーバー実測 OK |
| U3 | 既知値マスキング継続。マスキング失敗時は生出力を返さず `masking_failed`。成果物取り込み禁止。非秘密メタのみ | 実装・サーバー実測 OK |
| U4 | 端末CLIの非公開入力のみ。端末トークン+exec_idで紐付け。応答喪失時は状態照会のみ、自動再送なし | 実装・サーバー実測 OK |
| U5 | CPU/メモリ上限はOSへ委ね、`max_concurrent` を維持。停止・照会は発行元端末限定（明示指定時のみ他端末）。停止は冪等 | 実装・サーバー実測 OK |
| U6 | host:port+鍵種別で識別。異鍵は併存し接続時に選択。追記のみで削除CLIなし。鯖正本からPCへ同期 | 実装・サーバー実測 OK |
| U7 | 再作成で旧exec_id・旧端末トークンを無効化。端末・キー再登録。監査・メタデータをバックアップ。無効参照は invalid | 実装・サーバー実測 OK |
| U8 | 候補は保持し、50件/頁のページングで全件取得。抜粋は原文一致のみ。プロフィールは重複統合のみ、上限超過は黙って落とさず分割確認 | 実装・サーバー実測 OK |

### U1 直接編集（実装・実測済み）

- 追加: `rag/direct_edit.py`、`rag edit export|scan|watch|status`、`tests/test_direct_edit.py`、`ultra-rag-edit.service`
- 編集ワークスペースは `<home>/rag/edit/workspace`。確定改訂・検索索引・プロフィールから分離し、scanで採用されるまでモデルへ出さない。
- 各ファイルは `ultra_rag_edit`、`memory_id`、`base_revision`、`kind`、`scope`、`project_id`、`conditions`、`exceptions` を持つ。本文が記憶文。
- 更新は `evidence_kind=direct_edit` の通常の更新提案に変換する。既存の競合検出（対象改訂が古い場合）とプロフィール生成をそのまま通す。
- ID変更、kind/scope/project_id変更、未知ID、front matter破損、未来のbase_revisionは自動採用しない。
- 編集ファイルの削除は記録削除ではない。削除は `rag delete` だけ。
- 採用後は同じファイルパスを新しいmemory_idとrevisionで書き換え、編集ハンドルを維持する。
- 常駐: `ultra-rag-edit.service` を enabled / active にし、`--interval 2 --export-first` で動作。`/srv/ultra-rag/rag/edit` は700。
- 実測: サーバーで `tests.test_direct_edit` 7件 OK（非公開→scan採用、競合保留、構造拒否、削除無視、未知ID拒否、実プロセスのwatch取り込み）。サーバー全体120 tests OK（Windows専用2件skip）。`healthz` 200、`ultra-rag-edit.service` activeを確認。
- 残る細部: 編集ワークスペースはサーバー上だけで走査する。PC側の編集ファイル削除は正本削除にも編集削除にもならない（同期は削除を伝播しない）。編集の同時実行はfilelockで直列化する。

### U2 入力・成果物（実装・実測済み）

- `secret exec --input PATH`（複数可）は明示パスだけを専用workspaceへコピーする。ディレクトリは再帰し、シンボリックリンク・junctionは追わず `skipped` に理由を返す。
- `--deliverable NAME` と `--deliver-to DIR` は明示した成果物だけを回収する。1ファイルごとに一時ファイル＋fsync＋`os.replace` で原子的に置換する。
- 正常終了時は成果物を自動回収する。失敗・時間切れ・停止時はworkspaceを保持し、パスと理由を返す。常駐実行は終了時または `secret artifacts exec_...` で明示回収する。
- 実測: サーバーで再帰コピー、リンク拒否、原子的置換、失敗時保持、常駐成果物の後回し回収を確認。実CLIでも入力ファイル・入力ディレクトリから `out.txt` / `dirout.txt` を回収し、常駐の `report.txt` を `secret artifacts` で回収した。全120 tests OK。

### U3 マスキング失敗（実装・実測済み）

- `contracts.util.mask_known_values` が完全一致・base64・JSONエスケープを置換し、`total` / `types` / `markers` を返す。
- マスキング処理が例外になった場合は生のstdout/stderrを返さず、`status=failed`、`stop_reason=masking_failed`、`masking_status=failed` とする。成果物は取り込まず、workspaceだけ保持する。
- 常に「分割・暗号化・変換・外部送信は検出しない」旨の `masking_note` を返す。診断は件数・種別のみ。
- 実測: 完全一致、base64、マスキング例外時の非漏えい・成果物不取り込み・workspace保持をサーバーで確認。全120 tests OK。

### U4 端末入力の紐付け（実装・実測済み）

- `PasswordRequest` に `device_id` と `exec_id` を追加。`password request --exec-id` を必須にし、端末トークン認証と要求の端末一致を `create/status/cancel/run` で検証する。
- `password provide` はTTYの隠し入力だけ。`--password-stdin` を削除し、非TTYは拒否する。送信直後に応答を失っても再送せず、`password status --exec-id` で状態だけを照会する。
- 実測: 別端末・別exec_id拒否、停止端末拒否、非TTY拒否、exec_id照会をサーバーで確認。実sudoはPTYの隠し入力から起動し `authenticated`、実SSHは誤パスワードを `authentication_failed` と分類した。全120 tests OK。

### U5 端末限定と冪等停止（実装・実測済み）

- `ExecManager.status/stop` は `caller_device` を受け、発行元以外は `rejected` を返す。明示指定時だけ `--allow-other-device` で許可する。
- 停止ファイルは `write_lock` で直列化し、複数セッションからの同時停止を冪等にした。監視は停止ファイルの理由を保持する。
- 実行世代はCLIプロセスのPIDではなくホストのboot_idに紐付ける。同一ブート内の別CLIから状態を照会でき、再起動後の旧IDだけをunknownにする。
- CPU・メモリ上限は設けず、OSのcgroup / Job Objectへ委ねる。`secret.max_concurrent` は維持する。
- 実測: 別端末の照会・停止拒否、明示許可、5並列停止の冪等性と理由保持をサーバーで確認。実CLIでも別端末トークンへ切り替えて `rejected`、`--allow-other-device` で `running` を確認した。全120 tests OK。

### U6 ホスト鍵の併存と同期（実装・実測済み）

- `hostkeys.json` を `{"version":2,"records":[...]}` の追記型配列へ変更。識別は正規化した `host`、`port`、`key_type`、`fingerprint`。旧形式は読み込み時に移行する。
- 同じhost:portで異なる鍵を同時に保持し、`known_hosts` へ重複なく追記する。接続時は要求の `--host-key-fingerprint` で選び、曖昧な場合は `host_key_selection_required` で拒否する。選択時は選択鍵だけを書いた一時known_hostsを使い、別鍵の提示を認証前に拒否する。
- 削除・訂正CLIは提供しない。`password sync --remote` がサーバーの `/v1/hostkeys` からPCへ追記同期する。
- 接続時に `ssh-keyscan` を再実行しない。accept時の記録と、選択鍵だけを書いた一時known_hostsによるOpenSSHの照合で足り、再scanはsshdのレート制限で失敗し得るため。
- 実測: 実sshdから3鍵（RSA/ECDSA/Ed25519）をscan、実鍵をaccept、PCの `password sync` で `records_added=1` を受信。2鍵併存、重複acceptの非増殖、明示選択、誤SSHパスワードの `authentication_failed` を確認。全120 tests OK。

### U7 保管庫再作成（実装・実測済み）

- `secret recreate --confirm` は実行中の管理下プロセスを停止し、停止できなければ中止する。旧vault・端末トークン・run・監査・passwordを `secret/archive/vault-recreated-<UTC>/` へ移動する。
- 旧exec_id・旧端末IDを `invalidated.json` に記録し、`exec-status` と `devices list/status` は `invalid` を返す。旧トークンは認証できない。configには新しい端末IDを払い出す。
- 旧キーは復号できない。自動再実行はしない。archiveには旧ファイルとSHA-256 manifestを保持し、新しい監査ログに `vault_recreate` を記録する。
- 実測: 実行中停止、archiveとmanifest、旧キー解決拒否、旧exec/端末の `invalid`、旧トークン拒否、新端末ID、監査記録をサーバーで確認。実CLIでも `running` → `recreate` → `invalid` を確認した。全120 tests OK。

### U8 候補・抜粋・プロフィール（実装・実測済み）

- `candidate_page` は `rejected` を含めて保持し、50件/頁で `page/page_size/total/pages/has_more/next_page` を返す。CLI `rag candidates --page` とMCP `memory_candidates(page=...)` が全頁へ到達できる。
- `reject` は記録を削除せず `status=rejected` として保持する。整理は既存の adopt / reject / forget / delete だけ。
- プロフィールは同一文・kind・scope・project・条件・例外の重複だけを排除し、件数上限を設けない。`context_chars` が `rag.max_context_chars` を超えた場合は全文を返したまま `overflow` / `split_required` / `overflow_hint` を返す。
- 抜粋は保存時の原文完全一致だけを使い、`reference` の上限超過は `excerpts_total` / `excerpts_truncated` / `excerpt_overflow` / `split_required` で明示する。黙った切り捨てをしない。
- 実測: 55件の頁跨ぎ・却下保持、重複排除、文脈超過の非切り捨て、抜粋超過の明示をサーバーで確認。全120 tests OK。

## 8. 最終同期と稼働確認（2026-10-30）

- 正本: `/home/agent/ultra-RAG`。PC複製: `C:\Users\User\Desktop\ultra-RAG`。
- `README.md` / `IMPLEMENTATION.md` をサーバーへ反映した後、保護対象の `AGENTS.md` / `architecture.md` を除くソースファイルのSHA-256がサーバーとPCで完全一致した（`find` + `sha256sum` と `Get-FileHash` の突合、差分0）。
- 実環境テスト:
  - U2: 実CLIで入力ファイル・入力ディレクトリをコピーし、`out.txt` / `dirout.txt` を原子的に回収。常駐実行の `secret artifacts` も実プロセスで回収した。
  - U4: 実sudo（NOPASSWD）をPTY経由の隠し入力で実行し `authenticated`。実SSHは実ホスト鍵をacceptし、選択鍵だけの一時known_hostsで誤パスワードを `authentication_failed` と分類した。正しいSSHパスワードでの成功は未検証。
  - U6: 実sshdから3鍵をscanし、実ホスト鍵をaccept。PCクライアントの `password sync` で `/v1/hostkeys` から `records_added=1` を受信し、PC側の `hostkeys.json` / `known_hosts` を確認した。
  - U7: 実CLIで常駐実行を作成し、`secret recreate --confirm` 後に旧exec_idが `invalid`、旧端末が `invalid`、archiveが作成されることを確認した。
  - 再起動: 実際にホストを再起動し、35秒でSSH復帰。`ultra-rag-http.service` / `ultra-rag-lease.service` / `ultra-rag-edit.service` は active、`healthz` は200、旧exec_idは `unknown`、旧プロセスは残らなかった。boot_idの変化も確認した。
- 実環境テストで発見・修正した不具合: 実行世代をCLIプロセスのPIDに紐付けていたため、別CLIからの `exec-status` が常に `unknown` になっていた。ホストのboot_idに紐付ける方式へ変更し、同一ブート内の別プロセスから状態を照会できるようにした。再起動後は世代が変わり旧IDは `unknown` になる。
- 主要ファイルのSHA-256:
  - `README.md`: `8c2f7b61d4f21ab9ba1f69ab154db6b9fabdf49ea01a6b6007f335e171116e84`
  - `secret-core/secret_core/runner.py`: `ab252b641fda6c1efdae3efcae88193270ab325ecfee4b12ad2e75c7989ec555`
  - `secret-core/secret_core/vault.py`: `f0245c1c4c42da92c1158349e3b5239860414128640288726f9dbc5c02b42846`
  - `password-input/password_input/service.py`: `8d70f38058dcd75af38dbbc15bfe330b62ff0bc2d8cde42b4776db4e250f2967`
  - `rag/service.py`: `de6beb42bbd45ee27fa9dfaff8e76f5719c249ae1726dd912c61af1c9e93166c`
  - `tests/test_candidates.py`: `eafde2f8239734c452acdf234f39829b71b95986af9fd223c5e29f2db6df3cbc`
- 回帰: ローカル 120 tests OK、サーバー 120 tests OK（Windows専用2件skip）。
- 稼働: `ultra-rag-http.service` / `ultra-rag-lease.service` / `ultra-rag-edit.service` は active。`healthz` は `{"ok": true, "revision": 48}`。`/v1/hostkeys` はトークンあり200・なし401を実測。
- 本番データ: `/srv/ultra-rag` のキーは変更していない。監査済みの40記憶（個人7・共通教訓6・プロジェクト27）と実ホスト鍵1件を追加した。試験用の一時ホーム・キー・実行は削除した。
- 未検証: 正しい実SSHパスワードでの認証成功、実外部モデル接続、長期運用、OSレベルの実行主体分離。意味検索は未実装のまま。

## 9. 実稼働dsh/dsh-tui連携（2026-10-30）

- 保存情報の監査: サブエージェント1体だけに全会話を通読させ、秘密・一時ログ・未検証推測を除外した候補から、初回18件を保存した。その後、設計・運用・検証の恒久知識22件を追加し、質問文の誤登録1件をforgetした。現在の有効記憶は40件（個人7・共通教訓6・プロジェクト27）、改訂は48。
- ローカル複製: `%LOCALAPPDATA%\ultra-rag` の `sync_remote` を `http://192.168.1.200:8765` に設定。`rag sync once` でサーバー改訂48を取得。ローカルに正本の複製と未同期キューを持つ。
- dshプロファイル: `dsh-tui`、`headless`、`web` の `cordis.patch.yml` 末尾へ `ultra-rag-memory` と `mcp-ultra-rag` を追加。既存設定は変更せず、`.bak-ultrarag-*` を保持。
- headlessの `{{cwd}}` は起動時に未登録変数エラーになるため、当該1箇所を `Working directory: the current workspace.` へ置換した。
- 自動同期: プラグインに `syncBeforeTurn` を追加し、各ユーザー要求の前に `rag sync once` を実行。前ターンのキュー送信とサーバー更新の取り込みを行う。失敗してもローカル複製で継続する。
- 定期同期: Windowsのユーザータスク `ultra-RAG Sync` を5分間隔で登録。手動実行の `Last Result` は0。ログは `%LOCALAPPDATA%\ultra-rag\control\sync-task.log`。ログオン時タスクは管理者権限が必要なため作成しなかった。
- ランチャー: `bin\ultra-rag-dsh.cmd` / `bin\ultra-rag-dsh-tui.cmd` が起動前後に同期する。`bin\ultra-rag-sync.cmd` は単体同期。
- SKILL: `%USERPROFILE%\.dsh\skills\ultra-rag-memory\SKILL.md` を追加。記憶の参照・保存・更新・削除・報告手順を定義。
- 常時カスタム指示: `%USERPROFILE%\.dsh\rules\50-ultra-rag.md` を追加。dsh-rulesが毎ターン注入し、記憶質問ではSKILLを最初に使うよう指示。rules合計は約7.4KB。
- 誤登録修正: 「今覚えてることは?」のような質問を、単に「覚えて」を含むという理由で明示登録と誤認していた。`_is_memory_request` を追加し、疑問符を除外して命令形だけを受け付ける。回帰テストを追加し、誤登録1件はforgetした。
- ターン同期修正: クライアントキューの `user` / `assistant` オブジェクト形式が `/v1/turn` の `user_text` / `assistant_text` と不一致で送信できなかった。`sync_push` で正規化し、実キューを送信できるよう修正。回帰テストを追加。
- 実測:
  - `dsh-tui cli mcp status` が `mcp__ultra-rag__*` の15ツールを列挙。
  - `dsh --profile headless` の実モデル（ローカルantigravityプロキシ）から `mcp__ultra-rag__memory_snapshot` を呼び、`revision=48` を受信。
  - `dsh-tui cli skills list` が `ultra-rag-memory` を user-dsh として表示。スキル名を出さない「今の記憶の改訂番号を確認して」でも `memory_snapshot` が自動で呼ばれ、47を受信。
  - `dsh --profile headless` / `dsh-tui --dump-config` / `dsh --profile web --dump-config` が `ultra-rag-memory` と `mcp-ultra-rag` を合成。
  - 実TUI対話は端末UIのため自動化していない。TUIプロファイルのMCP登録・設定合成・同期経路までは実測済み。
- 資格情報: 直接headless実行はローカルプロキシへダミー環境値を渡して確認した。本人用の実資格情報は本機構に保存しない。dsh-tuiの通常起動は既存の資格情報経路を使う。
- 回帰: サーバー120 tests OK（Windows専用2件skip）、ローカル120 tests OK。





## 10. キー登録の実環境テスト（2026-10-30）

- 実行ユーザー: 保管庫所有者 `agent`。rootでは `systemd-creds --user` を復号できないため、root実行時は所有者UIDを示す明確なエラーを返すよう修正した。
- ダミーキーを `secret register --value-stdin` で登録し、`secret list` に1件表示されることを確認。
- `secret exec` で子プロセスへ供給し、子プロセス内で値のSHA-256一致を確認。結果は `finished` / exit 0。
- 実行結果・監査に生値が含まれないことを確認。
- `secret delete --yes` 後、`secret list` が0件になることを確認。
- 実APIキー・実CLOUDFLARE_ACCOUNT_IDは使用していない。`--env-name` は秘密値の名前であり、アカウントID等の非秘密ラベルと混同しない。

- NVIDIA_API_KEY: `GET https://integrate.api.nvidia.com/v1/models` はHTTP 200で、キー認証は成立。chat completionsはモデル依存で、`meta/llama-3.1-8b-instruct` は410（EOL）、`mistralai/mistral-large-2-instruct` と `google/gemma-3-12b-it` は404（アカウントに関数なし）、`deepseek-ai/deepseek-v4.1-flash` は60秒タイムアウト。キー自体の認証は確認できたが、推論権限は未確認。値・アカウントIDは出力していない。

- 表示名修正用 `secret rename <key_id> --name ...` を追加。封緘値は再生成せず、メタデータのみを更新する。実NVIDIAキーを `openai-main` / `openai` から `nvidia-main` / `nvidia` へ変更し、変更後も `GET /v1/models` が200で値を保持していることを確認。単体試験で `sealed` の不変も確認。

- `.env`一括登録: `secret import-env` を追加。`KEY_NAME=value`、`export`、コメント、引用符を解釈し、重複・空値・小文字キー・閉じない引用符を登録前に拒否する。全件検証後に `Vault.register_many` で登録し、検証エラーでは部分登録しない。実CLIを隔離ホームで起動し、2件のダミーキーが `key-name` / `second-key` として登録され、子プロセスが両方を受け取ったこと（長さ7/11）、出力・一覧・実行結果に生値が出ないことを確認。120 tests OK。

- 2026-10-30 ユーザー指示により、登録済みの全6キー（NVIDIA 1件、Cloudflare系5件）を `secret delete --yes` で削除した。削除前にID・表示名・環境変数名だけを確認し、値は表示していない。削除後 `secret list` は0件、`healthz` は200、3サービスはactiveのまま。

- 非秘密設定用の `.env.public.example` と秘密用の `.env.example` を追加。両方を同じディレクトリに置く運用とし、`.env` / `.env.public` はGit管理対象外にした。`.env.public` はアプリが自動読込せず、CLI/dsh起動前にシェルで `set -a; . ./.env.public; set +a` として読み込む。テンプレートをbashでsourceし、非秘密値が環境へ入ることを確認。

- キー登録の短縮コマンド `secret add <ENV_NAME> --service ...` を追加。表示名は環境変数名から自動生成し、`--purpose` は既定 `api`。値は従来どおり隠し入力で受け取り、argv・画面・シェル履歴に残さない。隔離ホームの実CLIで `TEST_KEY` を登録し、出力・一覧に生値が出ないことを確認。120 tests OK。

- 2026-10-30 ユーザーが5キーを再登録（Cloudflare系4、NVIDIA 1）。`secret list` で5件active、代表コマンドで全5キーが子プロセス環境に存在することを確認した。値は表示していない。外部APIの認証・権限は未検証。

## 11. knowledge-only記憶への整理（2026-10-30）

- 背景: `project_decision` 25件がarchitecture/README/IMPLEMENTATIONの設計判断の複製になっており、knowledgeではなく文書スナップショットだった。
- RAG stateをtarで退避した後、`project_decision` 25件とprojectスコープの`requirement` 2件を`rag delete --yes`で削除した。
- 整理後は13件（個人7・共通教訓6）、projectスコープ0件、tombstones 27、revision 85。
- さらに「README・architecture・IMPLEMENTATIONの内容、件数、実装手順を記憶せず、再利用可能な一般化された知識だけを保存する」というpersonal requirementを1件追加し、14件（個人8・共通教訓6）、revision 87。
- CLIは結果を常にJSONで返し、`--json`は存在しない。`rag list --json`と`secret list --json`はexit 2になることを実測した。

## 12. 全環境の記憶利用ルール（2026-10-30）

- 方針: 記憶はknowledge（再利用できる原則・教訓・判断基準）だけ。README・architecture・IMPLEMENTATION等の文書内容、件数、実装手順、一時ログ、テスト結果は保存しない。
- dsh共通: `~/.dsh/AGENTS.md` 第4節、`~/.dsh/RULE.md` 第17条、`~/.dsh/rules/50-ultra-rag.md`、`~/.dsh/skills/ultra-rag-memory/SKILL.md` を更新。全プロファイル（`dsh-tui` / `headless` / `web`）に適用。
- リポジトリ: `AGENTS.md` に「記憶の使い方」を追加。`project_decision`は`scope=project`必須、普遍化は`lesson`/`shared_lesson`へ明示的に昇華する規約にした。
- 他環境: PCの`~/.codex/AGENTS.md`、`~/.gemini/GEMINI.md`、`~/.claude/CLAUDE.md`にもknowledge-only規約を配置。MCPが使える場合はultra-RAGを正本とし、使えない場合は独自メモリへ一般化知識だけを保存する条件付きにした。
- サイズ: dshの常時注入rules合計は7,542 bytes（上限8KB以内）。skillは4,204 bytes。
- 検証: サーバーとPCの`AGENTS.md` / `RULE.md` / `50-ultra-rag.md` / `SKILL.md`のSHA-256が一致。リポジトリ`AGENTS.md`もサーバーとPCで一致。Codex/Gemini/ClaudeはPC上の配置と該当行を確認。dshの実TUI対話は未自動化。

## 13. 自宅鯖dshのMCP復旧（2026-10-30）

- 症状: 自宅鯖dshで`mcp__ultra-rag__*`が提供されず、記憶ブロックも注入されない、という申告。
- 原因: サーバーの`dsh-tui` / `headless` / `web`の`cordis.patch.yml`にultra-RAG overlayが存在せず、PC側だけに登録されていた。加えて3プロファイルとも`{{cwd}}`が未解決だった。
- 修正: 3プロファイルへ`ultra-rag-memory`と`mcp-ultra-rag`をLinuxパス（`/home/agent/ultra-RAG`、`/srv/ultra-rag`）で追加し、`{{cwd}}`をliteralへ置換。変更前に`.bak-ultrarag-*`を保持。
- 検証: `dsh --profile dsh-tui|headless|web --dump-config`で両エントリを確認。直接MCP stdioクライアントで19ツール（`memory_snapshot`等）を取得。`dsh.service`を再起動し、webはHTTP 200、`run_mcp.py`がサービス配下で起動。
- 制約: 既存セッションには遡及しない。新しいセッションを開始する。headlessは`ANTIGRAVITY_API_KEY`未設定でモデル実行に到達できないため、モデル経由のMCP呼び出しは未検証。MCPサーバー自体とプロファイル合成は実測済み。

## 14. GitHub公開（2026-10-30）

- 公開用ディレクトリを別途作成し、内部ドキュメント・実行データ・`.env`・サーバー固有パスを除外した。
- GitHub `puyokura/ultra-rag` をpublicで作成。
- puyokuraサイトへの配置は指示外だったため撤回。作成した`public_html/index.html`と`public_html/ultra-rag/`だけを削除し、`public_html`を711へ戻した。ルートは403、`/ultra-rag/`は404を確認。
- GitHub Actionsは`ULTRA_RAG_KEY_PROTECTOR=file-dev`と`ULTRA_RAG_ALLOW_INSECURE_KEYSTORE=1`をCI限定で使用。Python 3.11/3.12/3.13の120 testsが成功。
- 公開版はローカル優先。ホームサーバー同期は任意で、公開版の基本動作には不要。


## 15. 監査指摘の修正と公開（2026-11-01）

- 監査で起票した34件のうち31件を修正し、Issueへ実測を記録してクローズした。残るopenは #1（活動サマリー提案）、#34（意味検索・埋め込み設計提案）、#28（実装済み・再現未成立）。
- 採用権限（#2 #3 #13）: 採用はホストが観測したユーザー発言の証拠（session/message/範囲/digest/役割）または所有者操作のみ。MCPは提案専用とし、`memory_forget`/`memory_delete` は確認要求を返す。
- 保存ゲート（#4 #6 #7 #8 #9 #10 #11 #12 #15）: 保存直前の単一秘匿化、kind×scope許可表、`owner_session_id`、系統headの一意性、忘却済み証拠の再登録拒否、削除時の候補クローズとnote再生成、条件の三状態、全終端結果の操作台帳。
- 実行原子性（#19 #20 #21 #24 #25 #26 #27 #32）: exec_id単位のclaim、失効台帳の参照、成果物パスの成分検査、監督プロセスによる常駐所有、監視起動失敗の補償、子孫回収後の完了、回転世代の再照合、known_hostsの排他。
- クライアント・秘匿（#5 #14 #16 #17 #18 #22 #23 #29 #30 #31 #33）: キューは検証済み抜粋のみ、turn同一性の統一、hook結果の観測、改訂束縛キャッシュ、overflow伝搬、マスキング失敗時の取り込み拒否、メタデータ秘匿、sudo/askpassの非漏えい、sudoのローカル限定。
- 検証: PC（Windows, Python 3.13）171 tests OK（POSIX専用1件skip）、Debianサーバー（Python 3.13）171 tests OK（Windows専用2件skip）。各修正は未修正コードでの失敗を実測してから適用した（#28を除く）。
- 配送: GitHub masterへコードとテストのみを反映（commit 602a623）。公開README・SECURITY・CI設定・内部文書は公開していない。CI（Python 3.11/3.12/3.13）は成功。
- 未検証: 実sudoの認証必要経路、実SSHパスワード認証、実外部モデル、長期運用。#28は `_claim` を実装したが、未修正コードで同時応答を再現できず、赤→緑の証拠が取れていない。

## 16. #28の再現確定（2026-11-01 追補）

- 第15章の「#28は再現未成立」を撤回する。未修正経路（pending確認後にprovidedを書く実装）を再構成し、同時応答が2件成功して `AssertionError: 2 != 1` で落ちることを実測した。修正後は成功応答が1件のみ。
- 回帰テストは `tests/test_regression_password.py` に固定。PC・サーバーとも171 tests OK（サーバー2件skip）。commit `4e0f811` で公開し、Issue #28 をクローズした。

## 17. 反映と v0.1.1 公開（2026-11-01）

- サーバーの `ultra-rag-http.service` / `ultra-rag-lease.service` を 16:49:29 に再起動し、両方 `active`、`healthz` が `{"ok": true, "revision": 256}` を返すことを実測した。
- 再起動後にサーバーで171 tests OK（2 skip）。PCは171 tests OK（1 skip）。MCP stdioの実プロセス試験で `memory_remember` が `pending`、`memory_confirm` で `adopted` になることを確認。
- ライブホームのsessionスコープ記憶はPC・サーバーとも0件で、所有者必須化による影響はなかった。
- 設計提案 #1 / #34 を決定してクローズし、実装を #35（activity_summary）、#36（検索スコープ拡張）、#37（埋め込み評価）へ分割した。architecture 第27章に決定を記録。
- `pyproject.toml` を 0.1.1 へ更新。公開READMEにMCPの提案専用挙動を追記し、commit 625a84f を push。CI（3.11/3.12/3.13）成功。
- GitHub Release `v0.1.1` を公開（タイトルはバージョン番号のみ）。タグはRelease作成時に 625a84f を指して作成された。

## 18. 読み取り専用Web画面 /ui（2026-11-01）

- 既存HTTPサービスへ `GET /ui`（単一HTML）と `GET /ui/data`（JSON）を追加した。書き込み経路はない。
- 認証は付けず、LAN内IPからのみ許可する（private/loopback以外は403）。所有者の判断として記録する。
- 表示: プロフィール、禁止・必須、記憶一覧、候補。検索、状態・範囲フィルタ、IDコピー。全文表示で省略しない。
- 実測: PC 174 tests OK（POSIX専用1件skip）、Debian 174 tests OK（Windows専用2件skip）。再起動後に `http://192.168.1.200:8765/ui` が200（11,638 bytes）、`/ui/data` が200（revision 267、記憶114・候補6・セッション28）、`/v1/revision` は無認証401、`healthz` は200。
- 注意: 同一LANの端末は認証なしで閲覧できる。外部公開・ポート転送をしない。ページングは未実装。


## 19. 自動採用ルールとUI承認（2026-11-01）

- 自動採用は「覚えておいて」の明示だけに限定した。`覚えて` / `記憶して` の他の形は確認待ち候補になる。
- `kind="activity_summary"` を追加し、活動記録は承認なしで採用する。`autoload=False` とし、プロフィール・制約・既定検索へ出さない。
- プロフィール注入を、`profile/current.md` の読み取りから、採用済み記録の `render_profile` による描画へ変更した。Markdownファイルは成果物として残るが注入元ではない。
- `/ui` に確認待ち候補の承認・却下を追加した。`X-Ultra-RAG-UI: 1` ヘッダ必須、LAN内限定・認証なし。本文編集・削除は不可。
- 実測: PC 180 tests OK（1 skip）、Debian 180 tests OK（2 skip）。再起動後に `/ui` 200（13,012 bytes、承認表示3箇所）、`/ui/data` 200、`/v1/revision` 401、`healthz` 200。`/ui/decide` はヘッダなし403、ヘッダあり200。存在しないIDは状態変更なし。

## 20. /ui が表示できなくなった障害（2026-11-01）

- 症状: `/ui` はHTTP 200を返すが、ブラウザではデータが表示されない。
- 原因: 承認ダイアログのJS文字列で、改行を `\n` と書いた。`web_ui.py` のPython文字列がこれを実改行へ変換し、インラインスクリプトが `SyntaxError: Invalid or unexpected token` になった。サーバー側は正常、HTMLテキストのテストも通っていたため、構文エラーを検出できなかった。
- 修正: ダイアログ文言から改行を外した。`tests/test_web_ui.py` に、配信HTMLの `<script>` を `node --check` で構文検査する回帰テストを追加した。
- 実測: 配信ページのスクリプト構文検査0、ヘッドレスブラウザのDOMで `revision 278 / 記憶 120 / 候補 6` と120件表示を確認。commit `df6eb24` を公開しCI成功。


## 21. 注入分割・活動サマリー・検索スコープ・UI刷新（2026-11-01）

- 注入: 常時は核プロフィール（思想・設計・好み）と全禁止・必須だけにした。教訓・関連・候補・通知は `memory_search` / `memory_candidates` で都度取得する。`snapshot` に `include_lessons` / `include_related` / `include_pending` を追加し、設定でも既定を変えられる。ライブ実測 26,212→13,892文字（核7,994＋制約5,710＋案内）。
- #35: `activity` オブジェクト（date/overview/achievements/decisions/remaining）を契約・保存・同期・MCP・CLI（`--activity-json`）へ追加。作成・更新時に必須検証、`autoload=False`、プロフィール非注入。既存の構造化前レコードは読み取り互換。
- #36: 検索から `autoload=1` 制限を外し、未注入の採用済み記憶を含めた。抜粋を `excerpts_fts` に索引し、`session_id` 指定時のみ同一セッションの抜粋を返す。tombstone・他セッション・他プロジェクトは除外。literal参照は不変。
- UI: 既存の暗い配色・13px/1.45・スリムなテーブル・状態タグ・IDコピーを維持した。状態・種別・範囲の3フィルタ、活動記録タブ、活動の構造化表示を追加。承認・却下は維持。全面改装は行わない。
- 実測: PC 189 tests OK（1 skip）、Debian 189 tests OK（2 skip）。`/ui` 200、`/ui/data` 200、ヘッドレスDOMで「改訂290・記憶123・活動1・候補0」と種別フィルタ（活動/設計/教訓/思想/好み/禁止/必須）を確認。
- 既知の互換: 構造化前の activity_summary がライブに1件あり、読み込み時に検証で落ちたため、読み取りのみ許容するよう修正した。


## 22. 意味検索の任意実装（2026-11-01）

- モデル: `cl-nagoya/ruri-v3-310m`（Apache-2.0、768次元）。4モデル比較の結果、自然文とキーワードの両方で最も安定し、EmbeddingGemma 2より索引・クエリ・RSSが小さい。
- 索引: 検索DB内の `embeddings` 表にベクトルと `text_hash` だけを保存する。原文列は持たない。`source_kind` は `memory` / `excerpt`。
- 増分: 文・条件・例外のハッシュが同じ行は再推論しない。状態・範囲・プロジェクト・改訂だけが変わった行は、ベクトルを再利用してメタデータを更新する。忘却・却下・削除は索引から除去する。
- 無効化: モデル・プロンプト・次元の変更で全再構築する。`--rebuild` でも全再構築。
- 検索: literal上位とベクトル上位をRRF（k=60）で融合する。埋め込みの未導入・ロード失敗・次元不一致・破損は例外を握ってliteralのみを返す。既定は `embedding_enabled=false`。
- 設定: `rag.embedding_enabled` / `embedding_model` / `embedding_device` / `embedding_batch_size`。CLIは `rag embed-index`（`--rebuild`）。
- 実測（PC）: 201 tests OK（1 skip）。実モデル（ruri-v3-310m）で3文書の索引構築0.216秒、自然文「作業を元に戻せるようにするには？」「索引が壊れたらどうする？」がいずれも正解を1位に返した。
- 実測（Debian）: 201 tests OK（2 skip）。100文書の索引構築29.2秒、クエリ5回は395〜431ms、常駐RSS 1.9GB。1000文書は298.4秒・2.93MiB。Python 3.13.5、torch 2.15.0+cu132、sentence-transformers 6.2.0。
- 品質評価（Issue #37）: 30文書・自然文30問でRecall@1 1.000（literal 0.000）、キーワード30問で0.933（literal 1.000）。PCの1000件索引は38.3秒・2.93MiB、クエリ92.7ms。
- 未検証: 長文・抜粋混在時の品質、実セッションデータでの評価。既定はオフのままで、本番の検索経路はliteralのまま。
