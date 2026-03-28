# Claude Codeへの引き継ぎプロンプト

以下をそのままClaude Codeの最初のプロンプトとして貼り付けてください。

---

このプロジェクトは `state-verify` という状態遷移の網羅検証ツールです。
CLAUDE.md にアーキテクチャと使い方が書いてあるので、まず読んでください。

## やりたいこと

### Phase 1: MCPサーバーの動作検証

1. まず `pip install mcp pyyaml` でセットアップ
2. `.mcp.json` が正しく設定されているか確認し、MCPサーバーとして接続
3. 注文管理spec（`examples/order-states.yaml`）を使って以下を実行:
   - `sv_enumerate` でマトリクスが表示されることを確認
   - `sv_next` で最初のセルが返ることを確認
   - そのセルのpromptに対して回答を生成し、`sv_record` で記録
   - `sv_coverage` で進捗が更新されることを確認
4. 問題があれば修正してください

### Phase 2: メタ検証（MCP実装自体の検証）

1. `.mcp.json` の `--spec` を `examples/mcp-implementation.yaml` に切り替え
2. `sv_enumerate` で 14遷移×6関心事=84セルのマトリクスを確認
3. `sv_next` → 分析 → `sv_record` のループを回して、MCP実装の問題点を洗い出す
4. 特に以下のセルを優先的に検証:
   - `t2:error_response` (不正spec読み込み時のエラー応答)
   - `t13:input_validation` (存在しないセルIDでのrecord)
   - `t14:idempotency` (同一セルへの再record)
   - `t12:output_contract` (全セル検証済み時のsv_next応答)
5. 発見した問題は実際にコードを修正してください

### Phase 3: 自動巡回で注文管理を完全検証

1. specを `examples/order-states.yaml` に戻す
2. `sv_next` → 分析 → `sv_record` を全64セルに対して自動実行
3. `sv_coverage` が100%になったら `sv_export` で結果をエクスポート
4. `sv_tlaplus` でTLA+仕様を生成

各Phaseの完了後に進捗を報告してください。
