#!/usr/bin/env bash
set -euo pipefail

# state-verify MCP セットアップスクリプト
# Claude Code に state-verify MCP サーバーを登録する

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_SERVER="$SCRIPT_DIR/mcp_server.py"

echo "state-verify MCP Setup"
echo "======================"
echo ""

# Check dependencies
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required"
    exit 1
fi

python3 -c "import yaml" 2>/dev/null || {
    echo "Installing PyYAML..."
    pip install pyyaml --break-system-packages -q
}

python3 -c "from mcp.server.fastmcp import FastMCP" 2>/dev/null || {
    echo "Installing MCP SDK..."
    pip install mcp --break-system-packages -q
}

# Determine spec path
SPEC_PATH="${1:-}"
if [ -z "$SPEC_PATH" ]; then
    if [ -f "$SCRIPT_DIR/examples/order-states.yaml" ]; then
        SPEC_PATH="$SCRIPT_DIR/examples/order-states.yaml"
        echo "Using default spec: $SPEC_PATH"
    else
        echo "Usage: $0 <path-to-spec.yaml>"
        echo ""
        echo "Example:"
        echo "  $0 ./my-project/states.yaml"
        exit 1
    fi
fi

SPEC_PATH="$(cd "$(dirname "$SPEC_PATH")" && pwd)/$(basename "$SPEC_PATH")"

if [ ! -f "$SPEC_PATH" ]; then
    echo "Error: Spec file not found: $SPEC_PATH"
    exit 1
fi

echo "Spec file: $SPEC_PATH"
echo "MCP server: $MCP_SERVER"
echo ""

# Check if claude CLI is available
if command -v claude &>/dev/null; then
    echo "Registering MCP server with Claude Code..."
    
    JSON_CONFIG=$(cat <<EOF
{"type":"stdio","command":"python3","args":["$MCP_SERVER","--spec","$SPEC_PATH"]}
EOF
)
    
    claude mcp add-json state-verify "$JSON_CONFIG" 2>&1 && {
        echo ""
        echo "✅ MCP server registered successfully!"
        echo ""
        echo "Available tools in Claude Code:"
        echo "  - state_verify_enumerate  : マトリクス概要"
        echo "  - state_verify_next       : 次の未検証セル取得"
        echo "  - state_verify_record     : 検証結果の記録"
        echo "  - state_verify_coverage   : カバレッジレポート"
        echo "  - state_verify_prompt     : 特定セルのプロンプト"
        echo "  - state_verify_tlaplus    : TLA+ 仕様生成"
        echo "  - state_verify_export     : 結果エクスポート"
        echo ""
        echo "Claude Code で /mcp と入力してサーバーの接続状態を確認できます。"
    } || {
        echo ""
        echo "claude mcp コマンドでのセットアップに失敗しました。"
        echo "手動で設定する場合は以下を ~/.claude.json の mcpServers に追加してください:"
        echo ""
        echo "$JSON_CONFIG"
    }
else
    echo "claude CLI が見つかりません。"
    echo ""
    echo "手動セットアップ:"
    echo "以下のコマンドを実行してください:"
    echo ""
    echo "  claude mcp add-json state-verify \\"
    echo "    '{\"type\":\"stdio\",\"command\":\"python3\",\"args\":[\"$MCP_SERVER\",\"--spec\",\"$SPEC_PATH\"]}'"
    echo ""
    echo "または ~/.claude.json を直接編集:"
    echo ""
    cat <<EOF
{
  "mcpServers": {
    "state-verify": {
      "type": "stdio",
      "command": "python3",
      "args": ["$MCP_SERVER", "--spec", "$SPEC_PATH"]
    }
  }
}
EOF
fi

echo ""
echo "テスト実行:"
echo "  python3 $MCP_SERVER --spec $SPEC_PATH"
echo "  (Ctrl+C で終了)"
