# state-verify

あらゆる2軸マトリクスを網羅検証するツール。
LLMが苦手な「全行×全列の列挙」をコードが担い、各セルの判断だけをLLMに委ねる。

## アーキテクチャ

```
YAML DSL (人間が定義) → Parser (コードが列挙) → Verification Matrix (N rows × M columns)
                                                        ↓
                                              LLM が1セルずつ focused に回答
                                              （{verified_context} で前セルの回答を自動参照）
                                                        ↓
                                              Coverage Report + TLA+ 仕様生成
                                                        ↓
                                              テスト生成（stateful-pbt / pytest / mutmut）
```

## ファイル構成

- `state_verify.py` — CLIツール本体
- `mcp_server.py` — MCPサーバー（FastMCP, stdio）。sv_guideでワークフローガイド提供
- `tests/mcp_e2e_test.py` — MCP E2Eテスト（20ケース）
- `tests/test_stateful_self.py` — Hypothesis Stateful PBT（self-test）
- `examples/`
  - `order-states.yaml` — 状態遷移検証（13行×8列=104セル、paths含む）
  - `mcp-implementation.yaml` — MCP実装検証（14行×6列=84セル）
  - `code-behavior.yaml` — コード自動生成spec（28行×5列=140セル）
  - `permission-matrix.yaml` — 権限マトリクス（12行×4列=48セル）
  - `validation-rules.yaml` — フォームバリデーション（7行×4列=28セル）
  - `api-error-handling.yaml` — 外部API連携エラー（11行×4列=44セル）
  - `data-migration.yaml` — スキーママイグレーション（5行×4列=20セル）
  - `notification-routing.yaml` — 通知ルーティング（12行×4列=48セル）

## セットアップ

```bash
pip install mcp pyyaml
```

## CLIとして使う

```bash
python3 state_verify.py -s examples/order-states.yaml enumerate
python3 state_verify.py -s examples/order-states.yaml next --format json
python3 state_verify.py -s examples/order-states.yaml record <row_id> <column_id> -r '<json>'
python3 state_verify.py -s examples/order-states.yaml coverage
python3 state_verify.py -s examples/order-states.yaml tlaplus
```

## 検証ワークフロー

1. `sv_guide` でワークフローを理解する
2. `sv_enumerate` でマトリクス全体像を把握
3. `sv_next` で次の未検証セルとfocused promptを取得
4. promptの内容を分析し、指定のJSON形式で回答を生成
5. `sv_record` で row_id, column_id, response_json を記録
6. `sv_coverage` で進捗確認
7. remaining が 0 になるまで 3-6 を繰り返す

## YAML DSLの書き方

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

# 任意: 遷移経路の検証（rowに自動展開 + TLA+にtemporal property生成）
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
- `{verified_context}` — **同一rowの検証済みセル回答が自動注入**（columns定義順で依存解決）
- `{row_id}`, `{column_id}`, `{key}` — セル識別子

### セル間依存の解決

columns の定義順がそのまま検証順序になる。prompt_template に `{verified_context}` を含めると、
同じrowの前のcolumnで検証済みの回答が自動的にコンテキストとして注入される。

例: preconditions → side_effects → error_handling の順で定義すると、
error_handling のpromptには preconditions と side_effects の回答が含まれる。

### paths（遷移経路の検証）

pathsセクションで遷移の順序を定義すると:
1. 各pathが新しいrowとして自動展開される
2. TLA+生成時に `~>` (leads-to) temporal propertyが自動生成される
3. 「この経路全体でこの関心事はどうか」をマトリクスで検証可能

## ユースケース例

| ユースケース | rows | columns | サンプルspec |
|---|---|---|---|
| 状態遷移検証 | 遷移(from/to/trigger) | 関心事(preconditions/error_handling/...) | order-states.yaml |
| 権限マトリクス | ロール×リソース | CRUD操作 | permission-matrix.yaml |
| バリデーション | フィールド間依存 | 入力パターン(null/boundary/type/dependency) | validation-rules.yaml |
| 外部連携エラー | API×障害パターン | 業務状態(payment/fulfillment/refund) | api-error-handling.yaml |
| データマイグレーション | テーブル×カラム変更 | エッジケース(null/invalid/fk/rollback) | data-migration.yaml |
| 通知ルーティング | イベント×チャネル | ユーザー設定(opt_out/quiet_hours/frequency/locale) | notification-routing.yaml |

## テスト生成パイプライン

sv_testsで検証済み結果からテスト生成:

```
sv_tests --framework pytest          → LLMへのテスト生成prompt（言語問わず）
sv_tests --framework stateful-pbt    → 状態マシン構造化データ + 変換prompt（言語非依存）
sv_tests --framework mutmut          → mutation testingワークフロー（プロパティ強度検証）
```

stateful-pbtは言語非依存の構造化JSONを出力。LLMがそれを任意の言語に変換:
- Python: Hypothesis RuleBasedStateMachine
- TypeScript: fast-check modelRun
- Go: rapid StateMachine
- Rust: proptest
- その他: conversion_prompt をLLMに渡して変換

## git ルール

**テスト未実施のpushは禁止。** コード変更後は以下を必ず実行:

```bash
# E2Eテスト（20ケース）
.venv/bin/python3 tests/mcp_e2e_test.py

# Stateful PBT
.venv/bin/python3 -m pytest tests/test_stateful_self.py -v
```

両方通過を確認してからcommit → push。例外なし。

## 注意事項

- verified.json は `.{spec名}.verified.json` として保存
- sv_record は上書き可能（overwrittenフラグで判別、warningも返却）
- 参照系ツール（enumerate, next, prompt, coverage, batch_prompts）は副作用なし
- ファイルロック（.lock）で並行アクセス保護、atomic writeでクラッシュ安全
- コードを読ませてYAML specを自動生成→検証ループを回す使い方も可能
