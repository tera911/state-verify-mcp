# state-verify

あらゆる2軸マトリクスを網羅検証するツール。
LLMが苦手な「全行×全列の列挙」をコードが担い、各セルの判断だけをLLMに委ねる。

## 問題

LLMに「全状態遷移×全関心事を検討して」と依頼すると、必ず漏れが発生する。
権限マトリクス、バリデーションルール、外部API連携エラーなども同様。
人間と同じく、LLMは「列挙的思考」が苦手。

## 解決策

**列挙はコードが担い、判断だけをLLMに委ねる。**

1. 検証対象をYAML DSLで `rows × columns` として定義
2. ツールがマトリクスを機械的に生成（例: 12行 × 4列 = 48セル）
3. 各セルに対してfocused promptを自動生成
4. LLMは1セルずつ分析 → `{verified_context}` で前セルの回答を自動参照
5. カバレッジレポートで漏れを可視化
6. TLA+ 仕様を自動生成（遷移経路のtemporal property含む）
7. 検証結果からテストコード生成用promptを出力

## セットアップ

```bash
pip install pyyaml mcp
```

### MCPサーバー（推奨）

```bash
# ユーザースコープ（全プロジェクトで使える）
claude mcp add state-verify -s user -- \
  /path/to/state-verify/.venv/bin/python3 \
  /path/to/state-verify/mcp_server.py

# プロジェクトスコープ（specファイル固定）
claude mcp add state-verify -s project -- \
  python3 mcp_server.py --spec examples/order-states.yaml
```

## 使い方

### MCPツール（11個）

| ツール | 説明 |
|--------|------|
| `sv_guide` | ワークフローガイド（最初に呼ぶ） |
| `sv_enumerate` | マトリクス全体像をJSONで返す |
| `sv_next` | 次の未検証セル + focused prompt |
| `sv_prompt` | 特定セルのpromptを取得 |
| `sv_record` | 検証結果を記録 |
| `sv_coverage` | カバレッジレポート |
| `sv_batch_prompts` | 全未検証promptを一括取得 |
| `sv_export` | 全結果をJSONレポートとして出力 |
| `sv_tests` | 検証結果からテストコード生成用prompt |
| `sv_tlaplus` | TLA+仕様を自動生成 |
| `sv_reset` | 検証データ全消去 |

### 検証ワークフロー

```
sv_guide → sv_enumerate → sv_next → 分析 → sv_record → sv_coverage → 繰り返し
```

`sv_guide` を呼べばClaude Codeが自律的に巡回を開始できる。

### CLI

```bash
python3 state_verify.py -s examples/order-states.yaml enumerate
python3 state_verify.py -s examples/order-states.yaml next --format json
python3 state_verify.py -s examples/order-states.yaml record t1 preconditions -r '{"preconditions": [...]}'
python3 state_verify.py -s examples/order-states.yaml coverage
python3 state_verify.py -s examples/order-states.yaml tlaplus
```

## YAML DSL

```yaml
name: My Verification Matrix
domain_context: |
  背景情報をここに書く

rows:
  - id: row1
    field_a: value_a    # 任意のフィールド → テンプレート変数 {field_a}
    field_b: value_b    # {field_b}
    description: "行の説明"

columns:
  - id: col1
    prompt_template: |
      {field_a} について {field_b} の観点で検証してください。
      前セルの検証結果: {verified_context}
      コンテキスト: {domain_context}
      JSON形式で回答: {{"key": "value"}}

# 任意: 遷移経路の検証
paths:
  - id: happy_path
    sequence: [row1, row2, row3]
    description: "正常フロー"

# 任意: TLA+生成に必要
states:
  state_a:
    description: "状態Aの説明"
```

### テンプレート変数

- rowの全フィールドが自動的にテンプレート変数になる
- `{domain_context}` — specの背景情報
- `{verified_context}` — 同一rowの検証済みセル回答が自動注入（columns定義順で依存解決）
- `{row_id}`, `{column_id}`, `{key}` — セル識別子

### セル間依存の解決

columnsの定義順がそのまま検証順序。`{verified_context}` をprompt_templateに含めると、同じrowの前のcolumnで検証済みの回答が自動的にコンテキストとして注入される。

### paths（遷移経路の検証）

pathsセクションで遷移の順序を定義すると:
1. 各pathが新しいrowとして自動展開
2. TLA+生成時に `~>` (leads-to) temporal propertyが自動生成
3. 「この経路全体でこの関心事はどうか」をマトリクスで検証可能

## ユースケース

| ユースケース | rows | columns | サンプル |
|---|---|---|---|
| 状態遷移検証 | 遷移(from/to/trigger) | 関心事(preconditions/error_handling/...) | order-states.yaml |
| 権限マトリクス | ロール×リソース | CRUD操作 | permission-matrix.yaml |
| バリデーション | フィールド間依存 | 入力パターン | validation-rules.yaml |
| 外部連携エラー | API×障害パターン | 業務状態 | api-error-handling.yaml |
| データマイグレーション | テーブル×カラム変更 | エッジケース | data-migration.yaml |
| 通知ルーティング | イベント×チャネル | ユーザー設定 | notification-routing.yaml |
| コード実装検証 | コードの状態×操作 | 検証観点 | code-behavior.yaml |

## TLA+ との連携

```bash
python3 state_verify.py -s examples/order-states.yaml tlaplus -o OrderManagement.tla
tlc OrderManagement.tla  # TLC で検証
```

- **TypeInvariant**: state が定義済みの状態集合に含まれるか
- **Reachability**: 全ての状態に到達可能か
- **Path properties**: pathsから自動生成された `~>` temporal property
- **Safety invariants**: 検証済みセルの `invariants` キーから自動抽出

## アーキテクチャ

```
YAML DSL (rows × columns)
    ↓
Parser (コードが列挙)
    ↓
Verification Matrix (N rows × M columns)
    ↓
LLM が1セルずつ focused に回答
（{verified_context} で前セルの回答を自動参照）
    ↓
Coverage Report + TLA+ 仕様 + テストコード生成
```

列挙と網羅性保証 → コード（決定的）
個別の判断・推論 → LLM（得意なことだけ）
不変条件の検証 → TLA+ モデルチェッカー（形式的）
仕様と実装の橋渡し → sv_tests（テストコード生成）
