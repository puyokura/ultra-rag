# ultra-RAG 使い方ドキュメント

DeepSeek Harness（dsh）向けの、個人メモリ・セッション書き溜め・認証支援の実装と運用手順。

- 製品仕様の正本: [architecture.md](architecture.md)
- 作業手順・判断基準: [AGENTS.md](AGENTS.md)
- 実装・検証の記録: [IMPLEMENTATION.md](IMPLEMENTATION.md)
- 旧README（設計引き継ぎ文書）: [README.original.md](README.original.md)

このREADMEは「実際にどう使うか」の入口である。仕様の契約・選定理由・未決定のU項目は architecture と IMPLEMENTATION を参照する。

---

## 0. まずこれだけ読めば使える

### 0.1 この環境の役割

| 場所 | 役割 | ホーム |
| --- | --- | --- |
| 自宅Debian鯖 | 正本。RAG・保管庫・監査・HTTP API・systemdサービス | `/srv/ultra-rag` |
| ローカルPC | 同期複製と未同期キュー。dsh/dsh-tuiの実行端末 | `%LOCALAPPDATA%\ultra-rag` |

コードは実行データと分離する。サーバーのコードは `<SERVER_REPO>`、PCのコードは `<PC_REPO>`。実行データのホームをコード置き場にしない。

現在のバージョン: dsh `0.2.1-rc.2`、dsh-tui `0.17.0`、Python 3.13。MCPは `mcp>=1.0,<2` を使う。

### 0.2 すでに設定済みのこの環境で使う

PCでdshを起動するだけで、プロファイルに登録済みのultra-RAGプラグインとMCPが読み込まれる。

```powershell
# TUI
dsh-tui

# 非対話（headless）
dsh --profile headless "調べたいこと"

# Web UI
dsh web
```

起動前後に確実に同期したい場合:

```powershell
<PC_REPO>\bin\ultra-rag-dsh-tui.cmd
<PC_REPO>\bin\ultra-rag-dsh.cmd web
```

dsh起動中は、各ユーザー要求の前に `rag sync once` が実行される。前ターンの未同期キューをサーバーへ送り、サーバー側の更新を取り込む。5分間隔のWindowsタスク `ultra-RAG Sync` も同じ同期を行う。

### 0.3 動作確認の3コマンド

```powershell
$client = "$env:LOCALAPPDATA\ultra-rag"

# 同期状態。revision、unsynced、remoteが見える
<PC_REPO>\bin\ultra-rag.cmd --home $client rag sync status

# サーバー死活。okとrevisionが返る
curl.exe http://<SERVER_LAN_IP>:8765/healthz

# dsh-tuiプロファイルにMCPツールが見えているか
dsh-tui cli mcp status
```

### 0.4 何がどこへ保存されるか

```text
会話
  └─ dsh plugin (agent/pre-step)
       ├─ 起動時: サーバー正本のプロフィール・禁止・必須・関連記憶を注入
       └─ 要求前: rag sync once でサーバーと同期
  └─ dsh plugin (session/event turn/end)
       └─ 重要段落だけを抜粋し、PCの未同期キューへ
            └─ 次の要求前/5分タスクでサーバーへ送信
                 └─ サーバーが正本。PCは次のpullで複製を更新
```

- 全文の会話ログをRAGへ複製しない。保存するのは、決定・方針・禁止・好みなどの重要段落と、検証済みの出典だけ。
- 秘密値はRAG・索引・監査へ保存しない。
- サーバーで削除した記憶はPCへ伝播し、tombstoneが再取り込みを拒否する。

### 0.5 本書の読み方

| 知りたいこと | 読む節 |
| --- | --- |
| 全体像・用語 | 1〜2 |
| 新規に導入する | 3〜4 |
| PCと鯖を同期する | 5 |
| 記憶を登録・検索・更新する | 6 |
| dsh/dsh-tuiへ接続する | 7 |
| APIキーを安全に使う | 8 |
| sudo/SSHのパスワードを渡す | 9 |
| HTTP API・MCPツール | 10 |
| 試験・バックアップ・障害対応 | 11〜12 |
| セキュリティ境界・現在の配備 | 13〜14 |
| U項目の決定 | 15 |
| 用語集 | 17 |

---

## 1. これは何か

3つの機能を一つのモノレポで提供する。

| 機能 | できること | 秘密値の扱い |
| --- | --- | --- |
| RAG | 個人プロフィール、禁止・必須条件、関連記憶、セッション抜粋、過去ログ取り込み、PC同期 | 秘密を保存・索引化しない |
| キー利用 | 登録キーの一覧、選択したキーだけを実行時に供給、監査、停止 | 値は実行層だけが復号する。AIに返す取得APIはない |
| パスワード入力 | sudo / SSH の一回の認証要求へ、端末の隠し入力から渡す | 値を保存しない。一回だけ消費する |

正本は自宅鯖（この環境では `/srv/ultra-rag`）。PCは同期複製と未同期キューを持つ。キーの正本はサーバーに置き、PCのclientホームにも初期化時のローカル保管庫はあるが、サーバーのキーは複製しない。

```
PC (Windows)                               自宅鯖 (Debian)
 %LOCALAPPDATA%\ultra-rag                   /srv/ultra-rag
   ├─ revisions/  同期済み複製                ├─ revisions/  正本
   ├─ control/unsynced.jsonl 未同期提案        ├─ control/ 改訂・tombstone・APIトークン
   ├─ control/sync-token                      ├─ index/ 再構築可能な索引
   ├─ secret/  ローカル保管庫（通常は空）       ├─ secret/ 保管庫・端末・監査
   └─ password/ ローカル要求                   └─ password/ 要求・専用known_hosts
```

---

## 2. 構成

```text
ultra-RAG/
  README.md                 この使い方ドキュメント
  README.original.md        旧README（内容は変更せず退避）
  AGENTS.md                 作業ガイド
  architecture.md           製品仕様の正本
  IMPLEMENTATION.md         実装・検証・未検証事項
  rag/                      RAG本体、HTTP、MCP、同期、取り込み、dshプラグイン
  secret-core/              保管庫、端末認証、監査、実行、監視、systemdランチャー
  key-use/                  キー一覧・実行の薄いサービス層
  password-input/           sudo / SSH の非公開入力
  contracts/                型・JSON Schema・プロジェクトID
  cli/                      `ultra-rag` CLI
  scripts/run_mcp.py        MCP stdio の起動スクリプト
  bin/                      Windows/POSIX ランチャー
  tests/                    単体・統合・実プロセス試験
```

実行データの正本とコードは分ける。`/srv/ultra-rag` をコード置き場にしない。

---

## 3. インストール

### 3.1 必要環境

- Python 3.11以上（検証: Windows 3.13.14 / Debian 3.13.5）
- SQLite FTS5（trigram対応）
- Node.js 20以上（dshプラグインを使う場合）
- dsh 0.2.1系（MCP接続を使う場合）
- Debianでは `systemd-creds`（ユーザー単位の鍵保護）

Python依存は `pyproject.toml` に固定している。特にMCPは `mcp>=1.0,<2` を使う。`mcp 2.x` は `FastMCP` のAPIが変わり、この実装は動かない。

### 3.2 リポジトリ取得とvenv

```bash
cd <REPO親ディレクトリ>
git clone <このリポジトリ> ultra-RAG   # 既に配置済みなら不要
cd ultra-RAG
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Windowsでは次でもよい。

```powershell
cd <REPO>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

インストール後は `ultra-rag` コマンド、または `python -m ultra_rag_cli.main` が使える。

### 3.3 サーバーの本番ホーム

この環境の本番ホームは `/srv/ultra-rag`。コードは `/srv/ultra-rag` の外に置く。

```bash
sudo mkdir -p /srv/ultra-rag
sudo chown <USER>:<GROUP> /srv/ultra-rag
sudo chmod 700 /srv/ultra-rag

cd <REPO>
. .venv/bin/activate
python -m ultra_rag_cli.main --home /srv/ultra-rag init --host server
python -m ultra_rag_cli.main --home /srv/ultra-rag doctor
```

初期化で次が作られる。

- `config.json`、`rag/`、`secret/`、`password/`、`run/`、`control/`
- APIトークン `control/api-token`（600）
- 共有鍵で封緘された保管庫のマスターキー（Linux: `systemd-creds --user`、Windows: DPAPI）
- この端末の登録とローカルトークン
- 利用許可lease

`/srv/ultra-rag` は `700`、トークンと封緘鍵は `600` にする。

### 3.4 PCクライアントのホーム

Windowsの既定は `%LOCALAPPDATA%\ultra-rag`。

```powershell
bin\ultra-rag.cmd --home "$env:LOCALAPPDATA\ultra-rag" init --role client
```

クライアントは正本を持たない。最初の `sync pull` まではRAGは空で、読み取りは失敗する。

---

## 4. サーバーを常駐させる（systemd）

この環境では次の3つを **systemサービス** として有効化済み。

| unit | 役割 | 状態 |
| --- | --- | --- |
| `ultra-rag-http.service` | LAN内のHTTP API（`192.168.1.200:8765`） | enabled / active |
| `ultra-rag-lease.service` | 5分の利用許可leaseを60秒ごとに更新 | enabled / active |
| `ultra-rag-edit.service` | 直接編集ワークスペースの走査と取り込み | enabled / active |

unit例は `secret-core/systemd/` にある。`ultra-rag-edit.service.example` も含む。実際の配置では `<SERVER_REPO>/.venv/bin/python`、`/srv/ultra-rag`、LAN IPに置き換える。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ultra-rag-lease.service ultra-rag-http.service ultra-rag-edit.service

systemctl is-enabled ultra-rag-http.service ultra-rag-lease.service ultra-rag-edit.service
systemctl is-active  ultra-rag-http.service ultra-rag-lease.service ultra-rag-edit.service
sudo journalctl -u ultra-rag-http.service -f
sudo journalctl -u ultra-rag-lease.service -f
sudo journalctl -u ultra-rag-edit.service -f
```

### 4.1 LAN内だけで公開する

外部公開はしない。現在は **LANアドレスに直接バインド** している。

```text
ultra-rag-http.service:
  --host 192.168.1.200 --port 8765
```

- `0.0.0.0` にしない。LAN IPにだけlistenする。
- ルーターで8765番のポート転送をしない。
- サーバーのIPが変わったらunitの `--host` を直して `daemon-reload` → `restart`。
- 認証はBearerトークン必須。`/healthz` だけは認証なしで `ok` と改訂番号を返す。

LAN内の疎通確認:

```bash
curl http://<SERVER_LAN_IP>:8765/healthz
curl -o /dev/null -w '%{http_code}\n' http://<SERVER_LAN_IP>:8765/v1/revision   # 401
```

---

## 5. PCとサーバーの同期

### 5.1 トークンの配置

サーバーの `control/api-token` を、PCの `control/sync-token` に置く。値は表示しない。

```powershell
$client = "$env:LOCALAPPDATA\ultra-rag"
ssh agent "cat /srv/ultra-rag/control/api-token" |
  Set-Content -NoNewline "$client\control\sync-token"
icacls "$client\control\sync-token" /inheritance:r /grant:r "$($env:USERNAME):F"
```

### 5.2 同期コマンド

```powershell
$client = "$env:LOCALAPPDATA\ultra-rag"
$remote = "http://192.168.1.200:8765"
$token  = "$client\control\sync-token"

# 取得のみ
bin\ultra-rag.cmd --home $client rag sync pull --remote $remote --token-file $token

# 取得 → 未同期提案の送信 → 再取得
bin\ultra-rag.cmd --home $client rag sync once --remote $remote --token-file $token

# 状態
bin\ultra-rag.cmd --home $client rag sync status
```

- PCでの `rag remember` は「未同期提案」としてキューに入る。サーバーで採用されるまで正本にはならない。
- 通信断でもPCは最後に同期した内容を参照でき、変更はキューに残る。
- サーバーで削除した記憶は次の同期でPCからも消え、tombstoneが再取り込みを拒否する。
- 競合する更新は自動上書きせず、サーバーで競合保留になる。
- `config.json` の `sync_remote` を設定しておけば、`--remote` を省略して `rag sync once` だけで実行できる。このPCは設定済み。
- 同期は5分間隔の `ultra-RAG Sync` タスクと、`bin\ultra-rag-dsh*.cmd` の起動前後でも自動実行される。

### 5.3 手動でやる場合

```powershell
# サーバー側で記憶を追加
ssh agent "cd <REPO> && . .venv/bin/activate && \
  python -m ultra_rag_cli.main --home /srv/ultra-rag rag remember '方針' --kind preference"

# PCへ取得
bin\ultra-rag.cmd --home $client rag sync pull --remote $remote --token-file $token
```

---

## 6. RAGの使い方

### 6.1 記憶を登録する

```powershell
# 明言された方針（そのまま採用）
bin\ultra-rag.cmd --home $client rag remember "実測を設計より優先する" --kind design_principle

# 必須条件・禁止
bin\ultra-rag.cmd --home $client rag remember "必ず実プロセスで検証する" --kind requirement
bin\ultra-rag.cmd --home $client rag remember "秘密値を会話へ貼らない" --kind prohibition

# 案件限定
bin\ultra-rag.cmd --home $client rag remember "この案件ではSQLiteを使う" \
  --kind project_decision --scope project --project-id proj_xxxxxxxxxxxxxxxx

# 推測は確認待ち候補にする
bin\ultra-rag.cmd --home $client rag remember "簡潔な回答を好むかもしれない" --evidence extracted
```

`kind` は `philosophy`、`design_principle`、`preference`、`prohibition`、`requirement`、`lesson`、`project_decision`。
`scope` は `personal`、`shared_lesson`、`project`、`session`。

### 6.2 検索・確認

```powershell
bin\ultra-rag.cmd --home $client rag search "実測"
bin\ultra-rag.cmd --home $client rag list --include-pending
bin\ultra-rag.cmd --home $client rag get mem_...
bin\ultra-rag.cmd --home $client rag explain mem_...      # 出典の抜粋まで
bin\ultra-rag.cmd --home $client rag snapshot --query "実測" --text
bin\ultra-rag.cmd --home $client rag reference --session-id <SESSION_ID>
bin\ultra-rag.cmd --home $client rag reference --query "設計"
```

`snapshot` はプロフィール、全適用条件、関連記憶、確認待ち候補、更新通知を返す。`--text` はそのままモデルへ渡せる形で表示する。

CLIは結果を常にJSONで返す。`--json`はCLIのオプションではない。`rag list --json`や`secret list --json`は引数エラー（exit 2）になる。`dsh --json`とは別物なので混同しない。

### 6.3 候補の確認

```powershell
# 50件単位。却下済みも保持され、ページを進めれば全件へ到達できる
bin\ultra-rag.cmd --home $client rag candidates --page 1
bin\ultra-rag.cmd --home $client rag candidates --page 2
bin\ultra-rag.cmd --home $client rag confirm mem_... --decision adopt
bin\ultra-rag.cmd --home $client rag confirm mem_... --decision reject
```

候補は確認するまでプロフィール・禁止・必須へ入らない。モデルが推測した好みは自動採用しない。`reject` は記録を削除せず `rejected` として保持し、後から一覧・監査で確認できる。整理は既存の `adopt` / `reject` / `forget` / `delete` だけを使う。

### 6.4 更新・忘却・削除

```powershell
# 方針変更。旧版はsupersededとして残る
bin\ultra-rag.cmd --home $client rag update mem_... --statement "新しい方針" --target-revision 3

# 自動参照を停止。記録は明示参照で残る
bin\ultra-rag.cmd --home $client rag forget mem_...

# 記録ごと削除。版・抜粋・候補も削除し、再取り込みをtombstoneで拒否
bin\ultra-rag.cmd --home $client rag delete mem_... --yes
```

### 6.5 過去セッションの取り込み

```powershell
# 件数確認だけ
bin\ultra-rag.cmd --home $client rag import --dsh-home "$env:USERPROFILE\.dsh" --dry-run

# 実行（重要段落の抜粋と確認待ち候補だけを保存。全文複製はしない）
bin\ultra-rag.cmd --home $client rag import --dsh-home "$env:USERPROFILE\.dsh" --limit 5
```

- dshの `session.v4.jsonl.zstd` を読む。subagentセッションは理由付きで除外する。
- 取り込み台帳で同じファイルを二重処理しない。
- 秘密パターンは保存前に `[REDACTED]` へ置換し、編集済み抜粋として原文一致と区別する。
- 古い方針は確認待ち候補になり、自動では現在の方針にしない。

### 6.6 プロジェクトID

両端末で同じIDを使うため、リポジトリ直下にマーカーを置ける。

```powershell
bin\ultra-rag.cmd --home $client rag project-id <PROJECT_PATH> --write
```

マーカーがない場合は git remote のURL、それもなければディレクトリ名から生成する。ディレクトリ名だけのIDは別端末で衝突し得るため、マーカーかremoteを推奨する。

### 6.7 Markdown直接編集（U1）

採用済み記憶を専用領域へ書き出し、エディタで編集して取り込む。編集は即時確定ではなく、既存の検証・競合を通る提案になる。

```powershell
bin\ultra-rag.cmd --home $client rag edit export mem_...
bin\ultra-rag.cmd --home $client rag edit scan
bin\ultra-rag.cmd --home $client rag edit watch --interval 2
bin\ultra-rag.cmd --home $client rag edit status
```

- front matter の `memory_id` と `base_revision` を維持する。ID・種別・スコープ・プロジェクトを変える編集は構造破壊として拒否する。
- ファイル削除は正本の削除ではない。削除は `rag delete` を使う。
- 採用・統合された編集だけがプロフィールへ反映される。編集中は非公開。

---

## 7. dshとの接続

### 7.1 overlayの生成と登録

`rag overlay` は実パス入りのpatchを**生成するだけ**で、プロファイルを変更しない。

```powershell
<PC_REPO>\bin\ultra-rag.cmd --home "$env:LOCALAPPDATA\ultra-rag" `
  rag overlay --output "$env:LOCALAPPDATA\ultra-rag\dsh-overlay.yml"
```

生成物は次の2つを挿入する。

- `ultra-rag-memory`: `agent/pre-step` で記憶を注入し、`session/event` の `turn/end` で重要段落を保存するCordisプラグイン。`sync_remote` 設定時は `syncBeforeTurn: true` も付く
- `mcp-ultra-rag`: 記憶15ツールとキー4ツールを公開するMCP stdioサーバー

一時的に試す場合:

```powershell
dsh --profile <PROFILE> --patch "$env:LOCALAPPDATA\ultra-rag\dsh-overlay.yml"
```

このPCでは常時使うため、`dsh-tui` / `headless` / `web` の
`%USERPROFILE%\.dsh\profiles\<PROFILE>\cordis.patch.yml` 末尾へ同じ内容を登録済み。
変更前ファイルは `.bak-ultrarag-*` として残している。通常の `dsh` / `dsh-tui` 起動で自動的に読み込まれる。

dsh上のツール名は `mcp__ultra-rag__memory_remember` のように名前空間が付く。モデルはこの名前で呼ぶ。

MCPの意味は提案と確定で分かれる。`memory_remember` と `memory_update` は提案であり、保存層は「ホストが観測したユーザー発言の証拠（session/message/範囲/digest/役割）」または「所有者操作」がある場合だけ採用する。`memory_forget` と `memory_delete` は確認要求を返し、状態を変えない。確定は所有者CLIまたはhookが観測したユーザー発言で行う。

### 7.2 注入されるもの

`agent/pre-step` で次を含む1つのnoticeメッセージを差し込む。

常時に差し込むのは次の2つ。

- 核プロフィール（思想・設計理念・好み）
- 適用される全禁止・必須（検索順位に依存しない）

教訓・関連記憶・確認待ち候補・更新通知は常時注入しない。必要時に `memory_search` と `memory_candidates` で取得する。ブロック末尾に件数の案内を1行だけ付ける。実測で 26,212文字から 13,892文字へ減った。

古い記憶ブロックは追記せず、最新ブロックで置換する。ターン中の作業には差し込まない。

### 7.2.1 意味検索（任意・既定オフ）

既定の検索はFTS5 trigramとLIKEのliteral検索である。意味検索は次の設定を明示した場合だけ有効になる。オフのままではモデルを読み込まない。

```json
"rag": {
  "embedding_enabled": true,
  "embedding_model": "cl-nagoya/ruri-v3-310m",
  "embedding_device": "cpu",
  "embedding_batch_size": 16
}
```

- モデルは `cl-nagoya/ruri-v3-310m`（Apache-2.0、768次元）。実行には `sentence-transformers` と `torch` が別途必要。
- 初回とモデル変更時は `python -m ultra_rag_cli.main rag embed-index` で索引を作る。`--rebuild` で全再構築。
- 索引は再生成可能な派生物で、SQLiteの `embeddings` 表にベクトルとハッシュだけを保存する。原文は保存しない。
- 埋め込みの失敗・未導入・破損時はliteral検索へ自動で戻る。既定経路の挙動は変わらない。
- 実測（CPU）: PCは1000件で38.3秒・2.93MiB、クエリ92.7ms。Debianは1000件で298.4秒、100件のクエリ395〜431ms、常駐1.9GB。自然文30問のRecall@1は1.000、literalは0.000。キーワード30問はRecall@1 0.933。

### 7.3 実モデルの資格情報（本人が設定する方法）

この機構はモデルのAPIキーを読み書きしない。dsh側の既存の資格情報経路を使う。

1. `dsh web` を起動し、Models / Settings の画面からプロバイダのAPIキーを登録する。
2. または、dshが参照する環境変数（例: `COMMANDCODE_API_KEY`）を、dshを起動する本人のシェル/サービスへ設定する。
3. `~/.dsh/.credentials.yaml` をAIに読ませない。値をプロンプト・リポジトリ・unitファイルへ書かない。

検証環境では外部モデルの実資格情報が未設定のため、ローカルantigravityプロキシにダミー環境値を渡して実dsh接続を確認した。本人が資格情報を設定した後、同じoverlayで実プロバイダを試せる。

### 7.4 headlessプロファイルの `{{cwd}}` エラー

headlessプロファイルが `system-prompt` の `personaSuffix` で未登録変数 `{{cwd}}` を参照している場合、dshは起動に失敗する。

このPCではプロファイルの当該1箇所だけを `Working directory: the current workspace.` へ置換して解消した。プロファイルを直接変更したくない場合は、overlayで次を追加して回避できる。

```yaml
- id: system-prompt
  config:
    personaPrefix: >-
      You are an autonomous engineer.
    personaSuffix: >-
      Respond to the task.
```

### 7.5 実稼働構成（自宅鯖 + ローカルdsh/dsh-tui）

ローカルPCは自宅鯖の同期複製を持ち、dsh/dsh-tuiはその複製をMCPとプラグインから使う。サーバーが正本で、PCの変更はキューを経由して送られる。

このPCでは次を設定済み。

- `%LOCALAPPDATA%\ultra-rag\config.json` の `sync_remote` に `http://192.168.1.200:8765` を設定。
- `ultra-rag.overlay.yml` 相当の設定を `dsh-tui`、`headless`、`web` の各 `cordis.patch.yml` 末尾へ追加。既存設定は変更せず、バックアップを `.bak-ultrarag-*` として保持。
- `ultra-rag-memory`（`agent/pre-step` と `session/event`）と `mcp-ultra-rag`（RAG 15ツール + キー4ツール）を登録。
- プラグインの `syncBeforeTurn: true` により、各ユーザー要求の前に `rag sync once` を実行。前ターンのキューを送信し、サーバーの更新を取り込む。
- 正規タスク `ultra-RAG Sync` を5分間隔で実行。最終結果は `0`。ログは `%LOCALAPPDATA%\ultra-rag\control\sync-task.log`。ログオン時タスクは管理者権限が必要なため作成せず、dsh起動前後のランチャー同期で補う。
- 通常の `dsh` / `dsh-tui` に加え、起動前後で同期する `bin\ultra-rag-dsh.cmd` / `bin\ultra-rag-dsh-tui.cmd` も利用できる。

headlessプロファイルは `{{cwd}}` を解釈できず起動に失敗したため、当該1箇所だけ `Working directory: the current workspace.` へ置換した。プロファイルのバックアップは既存の `.bak-*` に加えて作成済み。

実測:

- `dsh-tui cli mcp status` が `mcp__ultra-rag__*` の15ツールを列挙。
- `dsh --profile headless` から `mcp__ultra-rag__memory_snapshot` を実際に呼び、`revision=48` を受信。
- ローカルとサーバーの双方で `rag sync once` が成功し、サーバー改訂48を確認。
- `dsh-tui --dump-config` と `dsh --profile dsh-tui/headless/web --dump-config` が `ultra-rag-memory` / `mcp-ultra-rag` を含むことを確認。
- dsh-tuiの実TUI対話は端末UIのため自動試験していない。TUIプロファイルのMCP登録と設定合成までは実測済み。

モデル資格情報はこの機構では保存しない。直接のheadless実行ではローカルantigravityプロキシにダミー環境値を渡して実走を確認したが、本人用の実資格情報の設定は別途必要である。

### 7.6 SKILLと常時カスタム指示（記憶をすぐ使う）

dshには、knowledgeをすぐ参照・保存するための指示を導入済み。README等の文書内容は記憶せず、再利用できる原則・教訓・判断基準だけを保存する。

1. **SKILL**: `~/.dsh/skills/ultra-rag-memory/SKILL.md`
   - knowledgeの参照・保存・更新・忘却の手順
   - `memory_snapshot` / `memory_search` / `memory_get` / `memory_explain` の使い分け
   - 明言方針は `explicit_statement`、推測は `extracted` 候補
   - 保存後のstatus確認、未同期を「正本保存済み」と報告しない規律
   - 秘密値・個人情報・文書内容を保存しない禁止
2. **常時カスタム指示**: `~/.dsh/rules/50-ultra-rag.md`
   - dsh-rulesが毎ターン注入する。rules合計は7,542 bytesで上限8KB以内
   - knowledge-only、scope、MCP参照、`--json`不在、保存時のevidence_kindを明示
3. **全体規約**: `~/.dsh/AGENTS.md` 第4節と `~/.dsh/RULE.md` 第17条
   - 全dshプロファイル（`dsh-tui` / `headless` / `web`）に共通
   - `project_decision`は`scope=project`必須、普遍化は`lesson`/`shared_lesson`へ明示的に昇華
4. **他環境**: リポジトリ`AGENTS.md`、Codex `~/.codex/AGENTS.md`、Gemini `~/.gemini/GEMINI.md`、Claude `~/.claude/CLAUDE.md`にも同じknowledge-only規約を配置

自宅鯖の`dsh-tui` / `headless` / `web`にもLinuxパスで同じoverlayを登録済み。`dsh --dump-config`で`ultra-rag-memory`と`mcp-ultra-rag`を確認できる。既存セッションには遡及しないため、設定変更後は新しいセッションを開始する。

使い方:

```text
# 自動: 「覚えて」「思い出して」「前に言った」「方針」「禁止」「必須」「プロファイル」
今の記憶の改訂番号を確認して

# 明示: スキル名を指定
ultra-rag-memoryスキルを使って、この案件の方針を確認して
```

確認:

```powershell
dsh-tui cli skills list
# ultra-rag-memory が user-dsh として表示される

dsh --profile headless --json "今の記憶の改訂番号を確認して、数値だけ答えてください。"
# dsh自身の--json。ultra-RAG CLIへ渡す--jsonではない
```

CLIは結果を常にJSONで返し、`--json`は受け付けない。dshから記憶を参照するときはMCPツールを使い、`rag list --json`のような存在しない引数を捏造しない。

### 7.7 読み取り専用Web画面（/ui）

自宅鯖のHTTPサービスに、記憶をブラウザで見る画面を追加した。

- URL: `http://192.168.1.200:8765/ui`
- 表示: プロフィール、禁止・必須、記憶一覧、活動記録、確認待ち候補。検索と、状態・種別・範囲の絞り込み、記憶IDのコピーができる。
- 活動記録は日付・概要・達成・決定・残件を表示する。
- 確認待ち候補は、画面から承認または却下できる。
- 原文は省略せず全文を表示する。長い文・パスは折り返し、表は横スクロールで保護する。
- 認証は付けていない。サーバーがLANアドレスにだけバインドされていることを前提にした所有者の判断である。**同じLANの端末からは誰でも閲覧でき、候補の承認・却下も実行できる。外部公開しない。**
- 承認・却下のPOSTは `X-Ultra-RAG-UI: 1` ヘッダを必須にし、単純なクロスサイトPOSTを弾く。
- `/ui` は閲覧と候補の承認・却下だけを行う。記憶の本文編集・削除の経路はない。`/v1` のAPIは従来どおりBearerトークン必須（未認証は401）。
- データは記憶・候補・プロフィール・制約だけで、キー名・値・パスワード要求は含めない。
- 実装は `rag/web_ui.py` と `rag/http_server.py` のルート。試験は `tests/test_web_ui.py`。

---

## 8. APIキー・トークンの使い方

### 8.1 本人がキーを登録する

値はargvに書かない。端末の隠し入力を使う。

**最短の1件登録**:

```bash
python -m ultra_rag_cli.main --home /srv/ultra-rag secret add NVIDIA_API_KEY --service nvidia
# 表示された "Secret value (input hidden):" にキーを貼り付ける
```

- 表示名は`nvidia-api-key`のように自動生成される。変えたい場合は`--name`を付ける。
- `--purpose`の既定は`api`。
- 値は画面・シェル履歴・argvに残らない。ファイル作成も不要。
- 複数キーをまとめて登録する場合は、次の`.env`を使う。
- `--value-stdin`は所有者の自動化専用。通常の登録では使わない。

```bash
# サーバーで実行
cd <REPO>; . .venv/bin/activate
python -m ultra_rag_cli.main --home /srv/ultra-rag secret register \
  --name openai-main --service openai --purpose api \
  --env-name OPENAI_API_KEY
# プロンプトに値を入力（画面・履歴に残らない）
```

所有者の自動化で標準入力を使う場合だけ `--value-stdin` を明示する。AIへ値を渡すための経路ではない。

**必ず保管庫の所有者ユーザーで実行する。** Debianの保管庫は `systemd-creds --user` で封緘されるため、rootや別ユーザーでは復号できない。rootで実行すると次の明確なエラーになる。

```text
ultra-rag: this vault is sealed with systemd-creds --user for the user that owns the vault (uid ...);
run as that user instead of uid 0
```

`whoami` が保管庫所有者（この構成では `agent`）であることを確認し、`sudo` を付けずに実行する。rootのシェルからは `sudo -u agent -i` などで所有者へ切り替える。

`--env-name` は**秘密値の名前**であり、用途名ではない。OpenAIのAPIキーなら `OPENAI_API_KEY`、CloudflareのアカウントIDなら秘密ではないため `CLOUDFLARE_ACCOUNT_ID` をキーとして登録しない。`--service` と `--purpose` は検索・監査用のラベルである。

キー登録の実測（ダミー値）:

1. 所有者ユーザーで `secret register` → `secret list` に1件
2. `secret exec` で子プロセスへ供給し、値のSHA-256一致を子プロセス内で確認 → `finished` / exit 0
3. 出力・監査に生値が含まれないことを確認
4. `secret delete --yes` 後、`secret list` が0件

### 8.2 .envから一括登録

`.env` の `KEY_NAME=value` 形式をそのまま登録できる。値は画面・argv・出力に渡さない。

```dotenv
# .env（値はダミー）
NVIDIA_API_KEY=key-~~~
CLOUDFLARE_API_TOKEN=token-~~~
```

```bash
python -m ultra_rag_cli.main --home /srv/ultra-rag secret import-env .env \
  --service nvidia --purpose api
```

- `KEY_NAME` が環境変数名、表示名は `key-name`（小文字化・`_`を`-`へ変換）。`--prefix` で表示名に接頭辞を付けられる。
- `--service` の既定は `dotenv`、`--purpose` の既定は `env`。実際の用途に合わせて指定する。
- 空行・`#`コメント・`export KEY=value`・シングル/ダブルクォートに対応。変数展開やコマンド実行はしない。
- 重複キー、空値、小文字キー、閉じていない引用符は登録前にエラーにする。全件を検証してから登録するため、検証エラーでは部分登録しない。
- 登録結果はID・表示名・環境変数名・状態だけで、値は返さない。
- 同じ環境変数名が既に登録済みの場合は拒否する。
- `.env` は所有者だけが読める権限で保管し、登録後に不要なら削除する。

### 8.3 非秘密設定用 `.env.public`

秘密用の `.env` と同じディレクトリに、非秘密設定用の `.env.public` を置ける。テンプレートはリポジトリ直下の `.env.public.example`。

```bash
cp .env.public.example .env.public
# 値を編集した後、CLIやdshを起動するシェルで読み込む
set -a
. ./.env.public
set +a
```

- APIキー・パスワード・トークン・秘密鍵・アカウントIDは`.env`だけに置き、`.env.public`へは書かない。
- `.env.public`はアプリが自動読込しない。CLIやdshを起動する前にシェルで読み込む。
- `.env`は所有者だけが読める権限（600）、`.env.public`は非秘密のため通常の設定ファイル権限でよい。
- `.env`と`.env.public`はGit管理対象外。テンプレートの`.env.example`と`.env.public.example`だけを共有する。
- `.env.public`の値は保管庫へ登録しない。`secret import-env`へ渡すのは秘密用の`.env`だけ。

### 8.4 一覧・利用・監査



```bash
python -m ultra_rag_cli.main --home /srv/ultra-rag secret list
python -m ultra_rag_cli.main --home /srv/ultra-rag secret refresh

python -m ultra_rag_cli.main --home /srv/ultra-rag secret exec \
  --command 'curl -sS https://api.example.com/v1/models' \
  --key key_... --timeout 60

# 入力を専用workspaceへコピーし、明示した成果物だけを回収する
python -m ultra_rag_cli.main --home /srv/ultra-rag secret exec \
  --command 'python report.py' --key key_... \
  --input ./config.json --input ./templates \
  --deliverable report.md --deliver-to /srv/ultra-rag/out

# 常駐実行の成果物を後から明示回収する
python -m ultra_rag_cli.main --home /srv/ultra-rag secret artifacts exec_...
```

- `secret list` はID・名前・用途・状態だけ返す。値は返さない。
- `secret exec` は選択したキーだけを子プロセスの環境へ供給する。コマンドは値を読める。
- `--input` は明示パスのみ。ディレクトリは再帰コピーするが、シンボリックリンク・junctionは追わない。
- 成果物は `--deliverable` で明示したものだけ。1ファイルずつ原子的に置換し、失敗・時間切れ・停止時はworkspaceを保持してパスと理由を返す。常駐実行は終了時または `secret artifacts` で回収する。
- 終了時に既知の値を完全一致・base64・JSONエスケープの範囲でマスキングする。マスキング自体に失敗した場合は生出力を返さず `masking_failed` とし、成果物も取り込まない。分割・暗号化・変換・外部送信は検出しない。
- 監査にはキーID・端末ID・実行ID・時刻・状態だけを残し、コマンド本文・生出力・値は残さない。

### 8.5 実行の制御

```bash
# 実行状態。旧世代のIDは unknown になり、再実行しない
python -m ultra_rag_cli.main --home /srv/ultra-rag secret exec-status exec_...

# 所有者による停止
python -m ultra_rag_cli.main --home /srv/ultra-rag secret stop exec_... --reason owner_stop

# 表示名だけ変更（値・暗号文は変更しない。再登録不要）
python -m ultra_rag_cli.main --home /srv/ultra-rag secret rename key_... --name nvidia-main --service nvidia

# キー差し替え（既存の旧キー実行は監視が停止。新しい供給は新値）
python -m ultra_rag_cli.main --home /srv/ultra-rag secret rotate key_...

# キー削除（新規供給を拒否し、既存実行を停止）
python -m ultra_rag_cli.main --home /srv/ultra-rag secret delete key_... --yes
```

- 単発実行には時間上限がある。期限切れ・削除・差し替え・端末停止・lease失効では成果物を自動取り込みしない。
- 成果物は正常終了時だけ、指定した `--deliver-to` へ反映する。既存ファイルは上書きし、取り込めるものだけ反映する。
- 照会・停止は発行元端末に限定する。別端末から明示的に操作する場合だけ `--allow-other-device` を付ける。拒否時は `rejected` と理由を返す。
- 停止要求は冪等で、複数セッションから同時に送っても停止ファイルは1つに集約される。停止理由は最初の要求ではなく最後に受理した理由を保持する。
- `secret rename` は表示名・`--service`・`--purpose` のメタデータだけを更新し、封緘値は変更しない。値の再入力は不要。
- 同時実行数は `config.json` の `secret.max_concurrent`（既定4）。上限超過は `concurrency_limit` で拒否する。CPU・メモリの上限は本機構では設けず、OSのcgroup / Job Objectへ委ねる。
- 管理下の実行は独立監視プロセスが停止条件を監視する。監視自体が止まった場合は管理側が子プロセスを停止する。

### 8.6 端末

```bash
python -m ultra_rag_cli.main --home /srv/ultra-rag secret devices list
python -m ultra_rag_cli.main --home /srv/ultra-rag secret devices register --name laptop --yes
python -m ultra_rag_cli.main --home /srv/ultra-rag secret devices stop --device-id dev_... --yes
```

登録トークンは一度だけ表示される。停止した端末は新規供給を拒否し、その端末の管理下実行を停止する。

### 8.7 systemd起動の処理へ供給する

unitの `ExecStart` から前景ランチャーを呼ぶ。キー値をunitや `EnvironmentFile` に書かない。

```ini
ExecStart=<REPO>/.venv/bin/python -m secret_core.keyrun \
  --home /srv/ultra-rag --key key_... -- /usr/local/bin/your-service
```

例は `secret-core/systemd/ultra-rag-keyrun.service.example`。unitの登録・enableは本人が行う。本機構は勝手にunitを作らない。

### 8.8 利用許可lease

`ultra-rag-lease.service` が60秒ごとにleaseを更新する。停止すると最後の更新から5分で次が起きる。

- 新規のキー供給を拒否
- 既存の管理下実行を停止し、一時ファイルを回収対象にする

手動更新は `secret refresh`。

### 8.9 保管庫の再作成（U7）

封緘鍵を失った場合や、旧端末・旧実行を完全に切り離す場合は、保管庫を再作成する。旧データは削除せず `secret/archive/` へ退避する。

```bash
python -m ultra_rag_cli.main --home /srv/ultra-rag secret recreate --confirm
```

- 実行中の管理下プロセスを停止してから、旧vault・端末トークン・実行状態・監査をarchiveへ移す。停止できなければ再作成を中止する。
- 旧exec_id・旧端末トークン・旧キーIDは無効。`exec-status` と `devices list` は `invalid` を返す。自動再実行はしない。
- 新しい端末IDがconfigへ払い出される。端末とキーは本人が再登録する。監査・メタデータ・archiveのmanifest（SHA-256）は保持する。

---

## 9. パスワード入力（sudo / SSH）

値は会話・AIのツール引数・RAG・保管庫へ保存しない。端末の隠し入力から、一回の認証要求へ渡す。

### 9.1 要求と入力

```bash
# SSHの例。exec_idは実行（または依頼）ごとに一意にする
python -m ultra_rag_cli.main --home /srv/ultra-rag password request \
  --adapter ssh --host <HOST> --port 22 --account <USER> --command "true" \
  --exec-id exec_...

python -m ultra_rag_cli.main --home /srv/ultra-rag password list
python -m ultra_rag_cli.main --home /srv/ultra-rag password provide pw_...

# 応答が失われた場合も再送しない。状態だけを照会する
python -m ultra_rag_cli.main --home /srv/ultra-rag password status --exec-id exec_...
```

- 要求の発行から5分で失効する。
- 要求は発行端末トークンと `exec_id` に紐付く。別端末・別exec_idからは照会・入力できない。
- 値は端末のTTY隠し入力だけから受け取る。`--password-stdin` は廃止し、非TTYからの入力は拒否する。
- 値は一度だけ消費される。失敗・取消後の再試行は新しい要求にする。送信直後に応答を失っても自動再送しない。
- 認証区間の生出力は返さず、`authenticated` / `authentication_failed` / `host_key_verification_failed` などの状態だけ返す。

### 9.2 SSHホスト鍵

```bash
python -m ultra_rag_cli.main --home /srv/ultra-rag password hostkey scan --host <HOST>
python -m ultra_rag_cli.main --home /srv/ultra-rag password hostkey check --host <HOST>
python -m ultra_rag_cli.main --home /srv/ultra-rag password hostkey accept --host <HOST> --fingerprint SHA256:...
python -m ultra_rag_cli.main --home /srv/ultra-rag password hostkey list

# サーバー正本のホスト鍵記録をPCへ同期
python -m ultra_rag_cli.main --home $client password sync --remote http://192.168.1.200:8765 \
  --token-file "$env:LOCALAPPDATA\ultra-rag\control\sync-token"
```

- 未確認・変更された鍵は自動承認しない。本人がscan結果を確認してacceptする。
- 識別は `host:port` と鍵種別。同じ接続先で異なる鍵を同時に受け入れられ、両方を保持する。接続時は `password request --host-key-fingerprint SHA256:...` で本人が選ぶ。指定がなく候補が複数なら `host_key_selection_required` で拒否する。選択時はその鍵だけを書いた一時 `known_hosts` で接続し、別の鍵が提示された場合は認証前に拒否する。
- 記録は追記のみで、削除・訂正のCLIは提供しない。誤登録は本人がアーカイブと `known_hosts` を確認して手動で整理する。
- 記録は本機構専用の `known_hosts` に置き、通常の `~/.ssh/known_hosts` は変更しない。
- `check` は保存済み指紋と現在のscan結果を比較し、`unchanged` / `changed` / `unknown` を返す。

### 9.3 実認証の検証状況

この環境のsudoはNOPASSWDのため、実sudoアダプターはPTYの隠し入力から実際に起動し、`authenticated` まで確認した。ただしパスワード検証そのものの証拠ではない。実SSHでは実ホスト鍵のaccept・選択・一時known_hostsを確認し、誤パスワードを `authentication_failed` と分類した。正しいSSHパスワードでの認証成功は、パスワードが未提供のため未検証である。

---

## 10. HTTP API と MCP

### 10.1 HTTP API

`127.0.0.1` またはLAN IPで起動する。Bearerトークン必須。

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/healthz` | 認証なしの死活確認 |
| GET | `/v1/revision` | 現在改訂 |
| POST | `/v1/snapshot` | プロフィール・条件・関連記憶 |
| POST | `/v1/search` | 範囲制御検索 |
| POST | `/v1/remember` | 記憶登録 |
| POST | `/v1/update` | 改訂 |
| POST | `/v1/forget` / `/v1/delete` / `/v1/confirm` | 忘却・削除・候補確認 |
| POST | `/v1/turn` | 一往復の抜粋保存 |
| POST | `/v1/explain` | 出典表示 |
| POST | `/v1/reference` | 過去セッションの明示参照 |
| GET | `/v1/sync/changes?since=N` | 差分同期 |
| POST | `/v1/sync/apply` | クライアント複製の適用 |
| GET | `/v1/verify` | manifest検証 |
| POST | `/v1/index/rebuild` | 索引再構築 |
| GET | `/v1/hostkeys` | サーバー正本のSSHホスト鍵記録（公開鍵のみ） |

値・パスワードを返すエンドポイントはない。

### 10.2 MCPツール

RAG（15）:

- `memory_snapshot`、`memory_search`、`memory_get`、`memory_explain`
- `memory_explicit_reference`、`memory_remember`、`memory_update`
- `memory_forget`、`memory_delete`、`memory_candidates`、`memory_confirm`
- `memory_status`、`memory_record_turn`、`memory_rebuild_index`、`memory_verify`

キー（4）:

- `key_list`（メタデータのみ）
- `key_exec`（選択キーだけを供給。値は引数・戻り値に含めない）
- `exec_status`
- `exec_artifacts`（常駐実行の成果物を明示回収）

MCPサーバー単体起動:

```bash
python scripts/run_mcp.py --home /srv/ultra-rag
```

---

## 11. テスト

```bash
cd <REPO>
. .venv/bin/activate
python -m unittest discover -s tests -t .
```

実測結果（2026-10-30）:

- Windows: 120 tests / OK
- Debian `/srv/ultra-rag` 環境: 120 tests / OK（Windows専用2件をskip）
- 実dsh起動・MCP呼び出し・turn保存: 実モデル（ローカルプロキシ）とテスト用モデルの両方で確認
- 実システムでのHTTP同期・削除伝播: 確認済み
- 実sudoアダプター: NOPASSWD環境でPTY隠し入力から`authenticated`を確認（パスワード検証の証拠ではない）
- 正しい実SSHパスワードでの認証成功、実外部モデル: 未検証

---

## 12. 運用

### 12.1 バックアップ

RAGの正本は `/srv/ultra-rag` の可読テキストとcontrol。索引は再構築できる。

```bash
sudo systemctl stop ultra-rag-http.service
sudo tar -C /srv -czf /path/to/backup/ultra-rag-$(date +%Y%m%d).tar.gz ultra-rag
sudo systemctl start ultra-rag-http.service
```

封緘された保管庫はホスト・ユーザーに結び付く。別ホストへコピーしても復号できない前提で扱う。復元時は削除制御（tombstone）を先に確認し、削除済みの本文を復活させない。

### 12.2 監査

```bash
python -m ultra_rag_cli.main --home /srv/ultra-rag secret audit list
python -m ultra_rag_cli.main --home /srv/ultra-rag secret audit delete --yes
```

監査には秘密・コマンド本文・生出力を保存しない。開始記録を書けない場合は新規供給を拒否する。

### 12.3 障害時の見方

- `doctor` でRAG改訂、lease鮮度、端末認証、保管庫保護を確認する。
- `journalctl -u ultra-rag-http.service` / `ultra-rag-lease.service` を見る。
- `mcp 2.x` が入るとMCPが起動しない。`pip show mcp` が `1.30.x` であることを確認する。
- LANから届かない場合は、unitの `--host`、サーバーIP、ルーターの隔離、Windowsファイアウォールを確認する。外部公開はしない。
- dshで記憶が注入されない場合は、overlayの `home`、`ULTRA_RAG_HOME`、Pythonパス、`rag doctor` を確認する。

---

## 13. セキュリティ境界

- キーを供給した任意コマンドは値を読める。マスキングは誤出力の低減であり、暗号化・分割・外部送信による漏えいを防がない。
- 同一ユーザーの別プロセスからの完全な隔離は未実装。専用ディレクトリ・環境・プロセスツリーの分離まで。
- 保管庫の復旧鍵はない。封緘鍵を失うとキーの再登録が必要。
- 削除は本機構の保存領域と派生物が対象。発行元のAPIキー失効、外部送信済みの値、dshの元セッションログの削除ではない。
- Markdown直接編集は専用領域の提案として取り込み、採用まで非公開。ファイル削除は記録削除ではない。
- 意味検索・埋め込みは任意機能として実装済み。既定はFTS5 trigramとLIKEのみで、`rag.embedding_enabled` を有効にした場合だけruri-v3-310mを併用する。
- パスワード入力は端末CLIのみ。Web/GUI画面は作らない決定（U4）。
- 監査・保管庫の管理は所有者CLIのみを想定する。同一OSユーザーでAIがシェルを実行できる環境では、OS権限による完全な管理分離にはならない。

---

## 14. 現在の配備状態

- サーバー: `/srv/ultra-rag` を正本ホームとして初期化
- コード: `<REPO>`（`/srv/ultra-rag` の外）
- systemd: `ultra-rag-http.service`、`ultra-rag-lease.service`、`ultra-rag-edit.service` を enabled / active
- LAN: `192.168.1.200:8765` にのみバインド。外部公開なし
- PC: `%LOCALAPPDATA%\ultra-rag` をclientとして初期化し、同期トークンを配置済み
- U1〜U8: 実装・サーバー実測・全120 tests OK。`/v1/hostkeys` と `secret recreate` を含む
- ホスト鍵: `192.168.1.200:22` の実鍵1件をサーバー正本へ登録し、PCへ同期済み
- 再起動: 実ホスト再起動後に3サービスactive、healthz 200、旧exec_idはunknownを確認
- キー: 5件（Cloudflare系4件、NVIDIA 1件。登録メタデータと子プロセスへの供給を確認。外部APIの有効性は未検証）
- 記憶: 36件（個人28・共通教訓8、projectスコープ0件）。正確な改訂は`healthz`で確認する。文書内容を保存しないknowledge方針を適用
- dsh: `dsh-tui` / `headless` / `web` にoverlayを登録。headlessの実モデルからMCP `memory_snapshot` を呼び revision=125 を確認。`dsh-tui` はMCP 15ツール登録と設定合成を確認
- 未検証: 正しい実SSHパスワードでの認証成功、実外部モデル、長期運用、Linuxの実行主体分離

---

## 15. U項目の決定（完了）

U1〜U8は2026-10-30に確定し、実装・サーバー実測・回帰テストまで完了した。詳細は `IMPLEMENTATION.md` 第7章。

- U1: 直接編集はID+基底改訂付きの提案として自動取り込み。採用まで非公開。
- U2: 明示入力・明示成果物、再帰コピー、リンク非追跡、原子的置換、失敗時保持、常駐の明示回収。
- U3: マスキング失敗時は生出力なし・`masking_failed`・成果物不取り込み。
- U4: 端末の隠し入力のみ。端末トークン+exec_idで紐付け。応答喪失時は状態照会のみ。
- U5: `max_concurrent` を維持。CPU/メモリ上限はOSへ委譲。発行元端末限定、明示指定時のみ他端末、停止は冪等。
- U6: host:port+鍵種別で識別。異鍵併存、接続時選択、追記専用、鯖正本からPCへ同期。
- U7: 再作成で旧ID・旧トークンを無効化しarchive保持。端末・キーは再登録。無効参照は `invalid`。
- U8: 候補は却下も保持し50件/頁で全件到達。抜粋は原文一致のみ。プロフィールは重複排除のみで、超過は分割を明示。

残る未検証は、正しい実SSHパスワードでの認証成功、実外部モデル接続、長期運用、OSレベルの実行主体分離である。意味検索は任意機能として実装済みで、既定はオフのまま。

---

## 16. ドキュメントの関係

- `README.md`（本書）: 使い方・運用手順
- `README.original.md`: 旧README。内容は変更せず退避
- `AGENTS.md`: AIの作業手順・判断基準
- `architecture.md`: 製品仕様・受入条件の正本
- `IMPLEMENTATION.md`: 実装・実測・未検証事項の記録
- `contracts/README.md`: 機能間の型・状態

---

## 17. 用語集と早見表

### 17.1 用語

| 用語 | 意味 |
| --- | --- |
| 正本 | 自宅鯖の `/srv/ultra-rag`。確定した記憶・監査・保管庫の唯一の正 |
| 複製 | PCの `%LOCALAPPDATA%\ultra-rag`。最後に同期した内容と未同期キューを持つ |
| revision | RAGの改訂番号。記憶・セッション・削除のたびに増える |
| manifest | 改訂ごとのファイルSHA-256一覧。改ざん・欠損の検出に使う |
| current pointer | 現在の確定改訂を指す `control/current.json` |
| profile | 個人プロフィール。採用済みの人物像・方針をモデルへ注入する |
| constraints | 適用される禁止・必須。検索順位に依存せず全件を注入する |
| candidate | 確認待ち・競合・却下済みの記憶。採用されるまで方針にならない |
| adopted | 採用済み。プロフィールまたは制約へ反映される |
| forgotten | 自動参照を停止。記録は明示参照用に残る |
| deleted | 記録・版・抜粋・候補を削除。tombstoneで再取り込みを拒否する |
| tombstone | 削除済みの指紋。同期で復活させないための制御 |
| unsynced | PCで作られ、まだサーバーへ送られていない提案・ターン |
| lease | キー供給を許可する期限。5分。`ultra-rag-lease.service` が更新する |
| device | 端末。端末トークンで本人を認証する |
| exec_id | キー実行の識別子。状態照会・停止・端末紐付けに使う |
| overlay | dshプロファイルへ追加するCordis/MCP設定。`rag overlay` が生成する |
| SKILL | dshの手順書。`ultra-rag-memory` が記憶の参照・保存手順を提供する |
| カスタム指示 | `~/.dsh/rules/*.md`。dsh-rulesが毎ターン注入する運用ルール |
| MCP | Model Context Protocol。dshからRAGツールを呼ぶstdioサーバー |
| turn-end | 一往復の終了イベント。重要段落の抜粋保存に使う |

### 17.2 目的別コマンド

```powershell
$client = "$env:LOCALAPPDATA\ultra-rag"
$repo = "<PC_REPO>"

# 同期
& "$repo\bin\ultra-rag.cmd" --home $client rag sync once
& "$repo\bin\ultra-rag.cmd" --home $client rag sync status

# 記憶
& "$repo\bin\ultra-rag.cmd" --home $client rag remember "方針" --kind preference
& "$repo\bin\ultra-rag.cmd" --home $client rag search "方針"
& "$repo\bin\ultra-rag.cmd" --home $client rag snapshot --text
& "$repo\bin\ultra-rag.cmd" --home $client rag candidates --page 1

# dsh
dsh-tui
dsh --profile headless "..."
& "$repo\bin\ultra-rag-dsh.cmd" web
```

```bash
# サーバー
cd <SERVER_REPO>
. .venv/bin/activate
python -m ultra_rag_cli.main --home /srv/ultra-rag doctor
python -m ultra_rag_cli.main --home /srv/ultra-rag secret list
systemctl is-active ultra-rag-http.service ultra-rag-lease.service ultra-rag-edit.service
curl http://127.0.0.1:8765/healthz
```

### 17.3 障害時の切り分け順

1. `curl http://<SERVER_LAN_IP>:8765/healthz` が返るか
2. PCで `rag sync status` の `remote` と `unsynced` を確認
3. `dsh-tui cli mcp status` に `mcp__ultra-rag__*` があるか
4. `python -m ultra_rag_cli.main --home <CLIENT_HOME> doctor` を実行
5. サーバーで `systemctl is-active` と `journalctl -u ultra-rag-http.service` を確認
6. dshで記憶が出ない場合は、プロファイルのoverlayと `ULTRA_RAG_HOME` を確認

### 17.4 コマンド体系

すべてのコマンドは `--home` で対象ホームを指定する。PCはクライアントホーム、サーバーは `/srv/ultra-rag` を指定する。

| グループ | コマンド | 用途 |
| --- | --- | --- |
| 全体 | `init` / `doctor` | 初期化、状態診断 |
| RAG | `rag snapshot` / `search` / `list` / `get` / `explain` | 参照 |
| RAG | `rag remember` / `update` / `forget` / `delete` | 登録・更新・忘却・削除 |
| RAG | `rag candidates` / `confirm` | 候補の確認・採用/却下 |
| RAG | `rag reference` / `import` | セッション参照・過去ログ取り込み |
| RAG | `rag edit` / `status` / `verify` / `rebuild-index` | 直接編集・状態・検証・索引再構築 |
| RAG | `rag sync pull/push/once/status` | PCと鯖の同期 |
| RAG | `rag project-id` / `overlay` / `serve` | 案件ID・dsh overlay・HTTP起動 |
| キー | `secret list` / `add` / `register` / `import-env` / `rename` / `rotate` / `delete` | キー管理 |
| キー | `secret exec` / `exec-status` / `artifacts` / `stop` | 実行・照会・成果物・停止 |
| キー | `secret devices` / `recreate` / `audit` / `refresh` | 端末・再作成・監査・lease |
| パスワード | `password request` / `list` / `status` / `cancel` / `provide` | 要求と一回入力 |
| パスワード | `password hostkey` / `password sync` | ホスト鍵の確認・同期 |

`python -m ultra_rag_cli.main --help`、`rag --help`、`secret --help`、`password --help` で各引数を確認できる。

---

## 18. 公開版

- GitHub: https://github.com/puyokura/ultra-rag
- 公開版はローカル優先で、PC単体で動作する。ホームサーバー同期は任意。
- 内部用の`IMPLEMENTATION.md`、`AGENTS.md`、`architecture.md`、実行データ、`.env`は公開リポジトリに含めない。
- systemd例は`%h`ベース、READMEは実ホストのIP・絶対パスを含めない。
- GitHub Actions: Python 3.11 / 3.12 / 3.13で120 tests OK。

