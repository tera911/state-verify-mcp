#!/usr/bin/env python3
"""
state-verify MCP server.

Exposes state transition verification as MCP tools for Claude Code.

Usage:
  python3 mcp_server.py --spec <path-to-yaml>

Tools provided:
  sv_enumerate    — Show verification matrix
  sv_next         — Get next unverified cell with focused prompt
  sv_prompt       — Get prompt for a specific cell
  sv_record       — Record verification result
  sv_coverage     — Show coverage report
  sv_tlaplus      — Generate TLA+ specification
  sv_batch_prompts — Get all unverified prompts
  sv_export       — Export full verification report
  sv_reset        — Clear all verification data
"""

import argparse
import functools
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))
from state_verify import (
    load_spec,
    load_store,
    save_store,
    get_all_cells,
    get_store_path,
    render_prompt,
    cell_key,
)


def _handle_errors(fn):
    """Catch exceptions at MCP tool boundary and return JSON error responses."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, FileNotFoundError, yaml.YAMLError) as e:
            return json.dumps({"status": "error", "error": str(e)})
        except json.JSONDecodeError as e:
            return json.dumps({
                "status": "error",
                "error": f"Store file corrupted: {e}. Consider running sv_reset.",
            })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": f"Unexpected error: {type(e).__name__}: {e}",
            })
    return wrapper

SPEC_PATH = os.environ.get("STATE_VERIFY_SPEC", "")

mcp = FastMCP(
    "state-verify",
    instructions=(
        "State transition verification tool. "
        "Use sv_next to get the next unverified cell, analyze it, "
        "then sv_record to save your analysis. "
        "Repeat until sv_coverage shows 100%."
    ),
)


def _resolve_spec(spec_path: str | None = None) -> str:
    path = spec_path or SPEC_PATH
    if not path:
        raise ValueError(
            "No spec file specified. Set STATE_VERIFY_SPEC env var "
            "or pass spec_path parameter."
        )
    if not Path(path).exists():
        raise FileNotFoundError(f"Spec file not found: {path}")
    return path


@mcp.tool()
@_handle_errors
def sv_enumerate(spec_path: str | None = None) -> str:
    """Show the full verification matrix with status indicators.

    Returns a JSON object with matrix data: name, dimensions, coverage,
    and a grid of transitions x concerns with verified status.

    Args:
        spec_path: Path to YAML spec file (optional if STATE_VERIFY_SPEC is set)
    """
    path = _resolve_spec(spec_path)
    spec = load_spec(path)
    store = load_store(path)
    cells = get_all_cells(spec)
    verified_keys = set(store.get("cells", {}).keys())
    total = len(cells)
    verified = sum(1 for c in cells if c["key"] in verified_keys)

    rows = spec.get("rows", spec.get("transitions", []))
    columns = spec.get("columns", spec.get("concerns", []))
    column_ids = [c["id"] for c in columns]
    matrix = []
    for r in rows:
        row_data = {"id": r["id"]}
        # Include all row fields for context
        for k, v in r.items():
            if k not in ("id", "prompt_template") and isinstance(v, str):
                row_data[k] = v
        row_data["cells"] = {
            c["id"]: cell_key(r["id"], c["id"]) in verified_keys
            for c in columns
        }
        matrix.append(row_data)

    return json.dumps({
        "status": "ok",
        "name": spec["name"],
        "rows_count": len(rows),
        "columns_count": len(columns),
        "total": total,
        "verified": verified,
        "coverage_percent": round(verified / total * 100, 1) if total else 0,
        "columns": column_ids,
        "matrix": matrix,
    }, ensure_ascii=False)


@mcp.tool()
@_handle_errors
def sv_next(
    spec_path: str | None = None,
    row_id: str | None = None,
    column_id: str | None = None,
) -> str:
    """Get the next unverified cell with its focused prompt.

    Returns cell key, row/column IDs, the full analysis prompt,
    and remaining count. Analyze the prompt, then call sv_record.

    Args:
        spec_path: Path to YAML spec file (optional if STATE_VERIFY_SPEC is set)
        row_id: Filter by row ID
        column_id: Filter by column ID
    """
    path = _resolve_spec(spec_path)
    spec = load_spec(path)
    store = load_store(path)
    cells = get_all_cells(spec)

    _null_cell = {
        "cell_key": None, "row_id": None, "column_id": None,
        "prompt": None, "remaining": 0,
    }

    if not cells:
        return json.dumps({"status": "empty", "message": "No cells in spec.", **_null_cell})

    unverified = [c for c in cells if c["key"] not in store.get("cells", {})]

    if not unverified:
        return json.dumps({"status": "complete", "message": "All cells verified!", **_null_cell})

    if row_id:
        unverified = [c for c in unverified if c["row_id"] == row_id]
    if column_id:
        unverified = [c for c in unverified if c["column_id"] == column_id]

    if not unverified:
        return json.dumps({"status": "filtered_empty", "message": "No matching unverified cells.", **_null_cell})

    cell = unverified[0]
    return json.dumps({
        "status": "pending",
        "cell_key": cell["key"],
        "row_id": cell["row_id"],
        "column_id": cell["column_id"],
        "prompt": render_prompt(cell, spec, store),
        "remaining": len(unverified) - 1,
    }, ensure_ascii=False)


@mcp.tool()
@_handle_errors
def sv_prompt(
    row_id: str,
    column_id: str,
    spec_path: str | None = None,
) -> str:
    """Get the focused prompt for a specific (row, column) cell.

    Args:
        row_id: Row ID
        column_id: Column ID
        spec_path: Path to YAML spec file (optional)
    """
    path = _resolve_spec(spec_path)
    spec = load_spec(path)
    store = load_store(path)
    cells = get_all_cells(spec)

    target_key = cell_key(row_id, column_id)
    cell = next((c for c in cells if c["key"] == target_key), None)

    if not cell:
        return json.dumps({"status": "error", "error": f"Cell '{target_key}' not found"})

    return json.dumps({
        "status": "ok",
        "cell_key": target_key,
        "row_id": row_id,
        "column_id": column_id,
        "prompt": render_prompt(cell, spec, store),
    }, ensure_ascii=False)


@mcp.tool()
@_handle_errors
def sv_record(
    row_id: str,
    column_id: str,
    response_json: str,
    spec_path: str | None = None,
) -> str:
    """Record a verification result for a specific cell.

    After analyzing a cell, call this with your structured JSON response.

    Args:
        row_id: Row ID
        column_id: Column ID
        response_json: Analysis result as JSON string matching the prompt format
        spec_path: Path to YAML spec file (optional)
    """
    path = _resolve_spec(spec_path)
    spec = load_spec(path)
    store = load_store(path)
    cells = get_all_cells(spec)

    target_key = cell_key(row_id, column_id)
    cell = next((c for c in cells if c["key"] == target_key), None)

    if not cell:
        return json.dumps({"status": "error", "error": f"Cell '{target_key}' not found"})

    warning = None
    try:
        response_data = json.loads(response_json)
    except json.JSONDecodeError:
        response_data = {"raw_text": response_json}
        warning = "response_json is not valid JSON; stored as raw_text"

    if "cells" not in store:
        store["cells"] = {}

    overwritten = target_key in store["cells"]
    store["cells"][target_key] = {
        "row_id": cell["row_id"],
        "column_id": cell["column_id"],
        "response": response_data,
        "verified_at": datetime.now().isoformat(),
    }

    save_store(path, store)
    total = len(cells)
    spec_keys = {c["key"] for c in cells}
    verified = len(set(store["cells"].keys()) & spec_keys)

    result = {
        "status": "recorded",
        "cell_key": target_key,
        "verified": verified,
        "total": total,
        "remaining": total - verified,
        "overwritten": overwritten,
    }
    if warning:
        result["warning"] = warning
    return json.dumps(result)


@mcp.tool()
@_handle_errors
def sv_coverage(spec_path: str | None = None) -> str:
    """Show coverage report with gap analysis.

    Returns overall progress, unverified cells grouped by transition,
    and per-concern coverage percentages.

    Args:
        spec_path: Path to YAML spec file (optional)
    """
    path = _resolve_spec(spec_path)
    spec = load_spec(path)
    store = load_store(path)
    cells = get_all_cells(spec)

    stored_keys = set(store.get("cells", {}).keys())
    spec_keys = {c["key"] for c in cells}
    verified_keys = stored_keys & spec_keys
    total = len(cells)
    verified = len(verified_keys)
    unverified = [c for c in cells if c["key"] not in stored_keys]

    gaps = {}
    for c in unverified:
        gaps.setdefault(c["row_id"], []).append(c["column_id"])

    col_cov = {}
    for c in spec["columns"]:
        cc = [cell for cell in cells if cell["column_id"] == c["id"]]
        cv = sum(1 for cell in cc if cell["key"] in verified_keys)
        col_cov[c["id"]] = {
            "verified": cv,
            "total": len(cc),
            "percent": round(cv / len(cc) * 100 if cc else 0),
        }

    return json.dumps({
        "status": "ok",
        "name": spec["name"],
        "total": total,
        "verified": verified,
        "remaining": total - verified,
        "coverage_percent": round(verified / total * 100, 1) if total else 0,
        "unverified_by_row": gaps,
        "coverage_by_column": col_cov,
    }, ensure_ascii=False)


@mcp.tool()
@_handle_errors
def sv_tlaplus(spec_path: str | None = None, output_path: str | None = None) -> str:
    """Generate TLA+ formal specification from the state machine.

    Produces a TLA+ module with state definitions, transitions,
    type invariants, and reachability properties.

    Args:
        spec_path: Path to YAML spec file (optional)
        output_path: If provided, save TLA+ to this file
    """
    path = _resolve_spec(spec_path)
    import io
    from contextlib import redirect_stdout

    # Always capture to stdout (output=None), then optionally save to file
    class FakeArgs:
        spec = path
        output = None

    from state_verify import cmd_tlaplus

    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_tlaplus(FakeArgs())

    tla_content = buf.getvalue()

    if output_path:
        with open(output_path, "w") as f:
            f.write(tla_content)

    result = {"status": "ok", "tla_spec": tla_content}
    if output_path:
        result["saved_to"] = output_path
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
@_handle_errors
def sv_batch_prompts(spec_path: str | None = None) -> str:
    """Get all unverified prompts at once.

    Returns every remaining cell with its prompt. Useful for planning
    or understanding the full scope of remaining work.

    Args:
        spec_path: Path to YAML spec file (optional)
    """
    path = _resolve_spec(spec_path)
    spec = load_spec(path)
    store = load_store(path)
    cells = get_all_cells(spec)

    unverified = [c for c in cells if c["key"] not in store.get("cells", {})]
    output = []
    for cell in unverified:
        output.append({
            "cell_key": cell["key"],
            "row_id": cell["row_id"],
            "column_id": cell["column_id"],
            "prompt": render_prompt(cell, spec, store),
        })

    return json.dumps({"status": "ok", "count": len(output), "cells": output}, ensure_ascii=False)


@mcp.tool()
@_handle_errors
def sv_export(spec_path: str | None = None) -> str:
    """Export all verification results as a structured report.

    Returns complete verification state including all recorded responses.

    Args:
        spec_path: Path to YAML spec file (optional)
    """
    path = _resolve_spec(spec_path)
    spec = load_spec(path)
    store = load_store(path)
    cells = get_all_cells(spec)

    spec_keys = {c["key"] for c in cells}
    report = {
        "status": "ok",
        "name": spec["name"],
        "generated_at": datetime.now().isoformat(),
        "total_cells": len(cells),
        "verified_cells": len(set(store.get("cells", {}).keys()) & spec_keys),
        "rows": [],
    }

    for r in spec["rows"]:
        r_data = {"id": r["id"]}
        for k, v in r.items():
            if k != "id" and isinstance(v, str):
                r_data[k] = v
        r_data["columns"] = {}
        for c in spec["columns"]:
            key = cell_key(r["id"], c["id"])
            if key in store.get("cells", {}):
                r_data["columns"][c["id"]] = store["cells"][key]["response"]
            else:
                r_data["columns"][c["id"]] = None
        report["rows"].append(r_data)

    return json.dumps(report, ensure_ascii=False)


@mcp.tool()
@_handle_errors
def sv_reset(spec_path: str | None = None) -> str:
    """Reset all verification data. Cannot be undone.

    Args:
        spec_path: Path to YAML spec file (optional)
    """
    path = _resolve_spec(spec_path)
    store_path = get_store_path(path)

    try:
        store_path.unlink()
        return json.dumps({"status": "reset", "message": "Verification store cleared."})
    except FileNotFoundError:
        return json.dumps({"status": "no_store", "message": "No store found."})


@mcp.tool()
@_handle_errors
def sv_tests(
    spec_path: str | None = None,
    row_id: str | None = None,
    framework: str = "pytest",
) -> str:
    """Generate test code from verified analysis results.

    Frameworks:
    - pytest, jest, rspec, go_test: Unit/integration test prompts
    - stateful-pbt: Language-agnostic stateful PBT spec (state machine + invariants + conversion prompt)
    - mutmut: Mutation testing workflow for verifying property strength

    Args:
        spec_path: Path to YAML spec file (optional)
        row_id: Generate tests for a specific row only (optional)
        framework: Test framework (pytest, jest, rspec, go_test, stateful-pbt, mutmut)
    """
    path = _resolve_spec(spec_path)
    spec = load_spec(path)
    store = load_store(path)
    cells = get_all_cells(spec)
    stored = store.get("cells", {})

    if framework == "stateful-pbt":
        return _build_stateful_pbt(spec, stored)

    if framework == "mutmut":
        return _build_mutmut_config(spec, stored)

    rows = spec["rows"]
    columns = spec["columns"]

    if row_id:
        rows = [r for r in rows if r["id"] == row_id]
        if not rows:
            return json.dumps({"status": "error", "error": f"Row '{row_id}' not found"})

    test_plans = []
    for r in rows:
        verified_cols = {}
        for c in columns:
            key = cell_key(r["id"], c["id"])
            if key in stored:
                verified_cols[c["id"]] = stored[key].get("response", {})

        if not verified_cols:
            continue

        test_plans.append({
            "row_id": r["id"],
            "row_fields": {k: v for k, v in r.items() if k != "id" and isinstance(v, str)},
            "verified_columns": verified_cols,
            "test_prompt": _build_test_prompt(r, verified_cols, spec, framework),
        })

    return json.dumps({
        "status": "ok",
        "framework": framework,
        "total_rows": len(test_plans),
        "test_plans": test_plans,
    }, ensure_ascii=False)


def _build_test_prompt(row: dict, verified_cols: dict, spec: dict, framework: str) -> str:
    """Build a prompt for generating test code from verified analysis."""
    row_desc = row.get("description", row["id"])
    row_fields = "\n".join(f"  {k}: {v}" for k, v in row.items() if k != "id" and isinstance(v, str))

    col_summaries = []
    for col_id, response in verified_cols.items():
        resp_str = json.dumps(response, ensure_ascii=False, indent=2) if isinstance(response, dict) else str(response)
        if len(resp_str) > 500:
            resp_str = resp_str[:500] + "\n  ... (truncated)"
        col_summaries.append(f"### {col_id}\n{resp_str}")

    return f"""以下の検証結果に基づいて、{framework} のテストコードを生成してください。

## 対象: {row_desc}
{row_fields}

## ドメインコンテキスト
{spec.get('domain_context', '(なし)')}

## 検証済み分析結果
{chr(10).join(col_summaries)}

## テストコード生成の指示
上記の分析結果から、以下を含む {framework} テストを生成してください:
1. 正常系テスト — 各検証項目の主要な正常パスを確認
2. 異常系テスト — エラーハンドリング、境界値、エッジケースのテスト
3. アサーション — 分析結果で特定されたinvariant/precondition/postconditionを検証
4. モック — 外部依存（API、DB等）のモック設定

テストコードのみを出力してください（説明不要）。"""


def _build_stateful_pbt(spec: dict, stored: dict) -> str:
    """Build a language-agnostic stateful PBT spec from verified cells."""

    states = spec.get("states", {})
    if isinstance(states, dict):
        state_names = list(states.keys())
    elif isinstance(states, list):
        state_names = states
    else:
        state_names = []

    transitions = [r for r in spec["rows"] if "from" in r and "to" in r and r.get("is_path") != "true"]

    # Extract invariants, preconditions, race conditions from verified cells
    invariants = []
    for r in spec["rows"]:
        for col in spec["columns"]:
            key = cell_key(r["id"], col["id"])
            if key in stored:
                resp = stored[key].get("response", {})
                if isinstance(resp, dict):
                    for inv in resp.get("invariants", []):
                        invariants.append({
                            "source": f"{r['id']}:{col['id']}",
                            "condition": inv.get("condition", ""),
                            "check": inv.get("check_query", ""),
                        })

    rules = []
    for t in transitions:
        guards = []
        key = cell_key(t["id"], "preconditions")
        if key in stored:
            resp = stored[key].get("response", {})
            if isinstance(resp, dict):
                guards = resp.get("guard_checks", resp.get("preconditions", []))

        side_effects = []
        key = cell_key(t["id"], "side_effects")
        if key in stored:
            resp = stored[key].get("response", {})
            if isinstance(resp, dict):
                side_effects = resp.get("side_effects", [])

        rules.append({
            "id": t["id"],
            "name": t.get("trigger", t["id"]),
            "from": t["from"],
            "to": t["to"],
            "description": t.get("description", ""),
            "guards": guards[:5],
            "side_effects": [s.get("action", "") for s in side_effects[:5]] if isinstance(side_effects, list) else [],
        })

    race_conditions = []
    for t in transitions:
        key = cell_key(t["id"], "concurrency")
        if key in stored:
            resp = stored[key].get("response", {})
            if isinstance(resp, dict):
                for rc in resp.get("race_conditions", []):
                    race_conditions.append({
                        "transition": t["id"],
                        "scenario": rc.get("scenario", ""),
                        "mitigation": rc.get("mitigation", ""),
                    })

    # Build conversion prompt
    prompt = f"""以下のstateful PBT仕様をあなたのプロジェクトの言語・フレームワークに合わせて実装してください。

## 状態マシン仕様: {spec['name']}

### States ({len(state_names)}個)
{json.dumps(state_names, ensure_ascii=False)}

初期状態: {state_names[0] if state_names else 'N/A'}

### Rules ({len(rules)}個)
各ruleは状態遷移を表します。テストフレームワークのstateful testing機能で実装してください。
- Python: hypothesis.stateful.RuleBasedStateMachine
- TypeScript: fast-check fc.modelRun / fc.commands
- Go: rapid.StateMachine
- Rust: proptest
- その他: ランダムな操作列を生成して各ステップ後にinvariantをチェック

### Invariants ({len(invariants)}個)
**全ruleの実行後に毎回チェックすべき不変条件。** 1つでも破れたらテスト失敗。

### Race Conditions ({len(race_conditions)}個)
並行実行時に注意すべき競合シナリオ。stateful testで再現を試みる。

## 実装の指示
1. 各ruleのfrom状態をpreconditionとして設定（その状態でなければスキップ）
2. ruleの実行後にto状態に遷移
3. 全invariantを毎ステップ後に検証
4. テストフレームワークにランダムなrule列を生成させる（最低20ステップ×50例）
5. guards/side_effectsはrule内でSUT（テスト対象システム）に対して実行

テストコードのみを出力してください。"""

    return json.dumps({
        "status": "ok",
        "framework": "stateful-pbt",
        "state_machine": {
            "name": spec["name"],
            "initial_state": state_names[0] if state_names else None,
            "states": state_names,
            "rules": rules,
            "invariants": invariants[:20],
            "race_conditions": race_conditions[:10],
        },
        "conversion_prompt": prompt,
        "known_frameworks": {
            "python": "hypothesis RuleBasedStateMachine",
            "typescript": "fast-check fc.modelRun / fc.commands",
            "go": "rapid StateMachine",
            "rust": "proptest",
            "ruby": "Rantly + custom state machine",
        },
    }, ensure_ascii=False)


def _build_mutmut_config(spec: dict, stored: dict) -> str:
    """Build mutation testing guidance from verified spec."""

    # Collect all properties that should detect mutations
    properties = []
    for r in spec["rows"]:
        for col in spec["columns"]:
            key = cell_key(r["id"], col["id"])
            if key in stored:
                resp = stored[key].get("response", {})
                if isinstance(resp, dict):
                    for inv in resp.get("invariants", []):
                        properties.append({
                            "source": f"{r['id']}:{col['id']}",
                            "condition": inv.get("condition", ""),
                        })
                    for guard in resp.get("guard_checks", []):
                        properties.append({
                            "source": f"{r['id']}:{col['id']}",
                            "condition": guard if isinstance(guard, str) else str(guard),
                        })

    return json.dumps({
        "status": "ok",
        "framework": "mutmut",
        "description": (
            "Mutation testing verifies that your properties are strong enough. "
            "If a mutation survives (code is changed but tests still pass), "
            "the property is too weak — go back to state-verify and deepen the analysis."
        ),
        "properties_to_protect": len(properties),
        "setup": [
            "pip install mutmut",
            "mutmut run --paths-to-mutate=src/",
            "mutmut results",
            "mutmut html  # Visual report",
        ],
        "workflow": [
            "1. sv_tests --framework pytest で通常テストを生成",
            "2. sv_tests --framework hypothesis-stateful でPBTを生成",
            "3. mutmut run でmutation testを実行",
            "4. 生き残ったmutant = プロパティが弱い箇所",
            "5. state-verify の該当セルに戻って分析を深める",
            "6. テストを強化して再度 mutmut run",
        ],
        "key_properties": properties[:30],
    }, ensure_ascii=False)


@mcp.tool()
def sv_guide() -> str:
    """Matrix verification workflow guide — call this first.

    Call this when:
    - 状態遷移・権限・バリデーション等の設計を網羅検証したいとき
    - 新機能の設計にバグや漏れがないか確認したいとき
    - 既存コードから検証specを自動生成して検証したいとき
    - 「検証して」「レビューして」「網羅チェックして」と依頼されたとき

    Returns the full workflow, tool descriptions, and YAML DSL reference.
    """
    return json.dumps({
        "status": "ok",
        "guide": {
            "overview": (
                "state-verify はあらゆる2軸マトリクスを網羅検証するツールです。"
                "YAML DSL で rows × columns を定義すると検証マトリクスが生成されます。"
                "各セルに focused prompt が自動生成され、分析結果を記録することで100%カバレッジを達成します。"
                "セル間依存は {verified_context} で前のセルの回答を自動注入。"
                "paths セクションで遷移経路の検証も可能です。"
            ),
            "workflow": [
                "1. sv_guide を呼ぶ（今ここ）",
                "2. sv_enumerate でマトリクス全体像を把握",
                "3. sv_next で次の未検証セル + prompt を取得",
                "4. prompt を分析し JSON 形式で回答",
                "5. sv_record で row_id, column_id, response_json を記録",
                "6. sv_coverage で進捗確認",
                "7. remaining=0 まで 3-6 を繰り返す",
                "8. sv_export でレポート出力",
                "9. sv_tests で検証結果からテストコード生成（実装との橋渡し）",
                "10. sv_tlaplus で TLA+ 生成（states定義があれば）",
            ],
            "tools": {
                "sv_guide": "ワークフローガイド（最初に呼ぶ）",
                "sv_enumerate": "マトリクス全体像をJSONで返す",
                "sv_next": "次の未検証セル+prompt。row_id/column_idでフィルタ可能",
                "sv_prompt": "特定セルのpromptを取得（再確認用）",
                "sv_record": "検証結果を記録",
                "sv_coverage": "カバレッジレポート",
                "sv_batch_prompts": "全未検証promptを一括取得",
                "sv_export": "全結果をJSONレポートとして出力",
                "sv_tests": "検証結果からテスト生成（pytest/jest等のprompt, stateful-pbtの構造化データ, mutmutのワークフロー）",
                "sv_tlaplus": "TLA+仕様を自動生成",
                "sv_reset": "検証データ全消去（取り消し不可）",
            },
            "yaml_dsl": {
                "required": ["name", "rows", "columns"],
                "optional": ["states", "domain_context", "paths"],
                "template_variables": [
                    "rowの全フィールドが自動的にテンプレート変数になる",
                    "{domain_context} — specの背景情報",
                    "{verified_context} — 同一rowの検証済みセル回答（自動注入）",
                    "{row_id}, {column_id}, {key} — セル識別子",
                ],
                "example": (
                    "name: My Matrix\n"
                    "domain_context: |\n"
                    "  背景情報\n"
                    "rows:\n"
                    "  - id: row1\n"
                    "    field_a: value_a\n"
                    "    field_b: value_b\n"
                    "columns:\n"
                    "  - id: col1\n"
                    "    prompt_template: |\n"
                    "      {field_a} x {field_b} を検証。\n"
                    "      前のセルの回答: {verified_context}\n"
                    "      コンテキスト: {domain_context}\n"
                    "      JSON: {{\"result\": \"...\"}}\n"
                    "paths:  # 任意: 遷移経路を検証\n"
                    "  - id: happy\n"
                    "    sequence: [row1, row2]\n"
                    "    description: '正常フロー'\n"
                ),
            },
            "use_cases": [
                "状態遷移: rows=遷移(from/to/trigger), columns=関心事",
                "権限マトリクス: rows=ロール×リソース, columns=CRUD",
                "バリデーション: rows=フィールド間依存, columns=入力パターン",
                "外部連携エラー: rows=API×障害パターン, columns=業務状態",
                "データマイグレーション: rows=テーブル×カラム変更, columns=エッジケース",
                "通知ルーティング: rows=イベント×チャネル, columns=ユーザー設定",
            ],
            "tips": [
                "{verified_context} をprompt_templateに含めると、同一rowの前のcolumnの回答がコンテキストとして自動注入される",
                "columns の定義順が検証順序を決める — 前のcolumnの回答が後のcolumnのコンテキストに",
                "paths セクションで遷移経路を定義すると、pathがrowとして自動展開+TLA+にtemporal property生成",
                "sv_record は上書き可能（overwritten フラグで判別）",
                "コードを読ませてYAML specを自動生成→検証ループを回すことも可能",
            ],
        },
    }, ensure_ascii=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="state-verify MCP server")
    parser.add_argument("--spec", "-s", help="Default spec file path")
    args = parser.parse_args()

    if args.spec:
        SPEC_PATH = str(Path(args.spec).resolve())
        os.environ["STATE_VERIFY_SPEC"] = SPEC_PATH

    mcp.run(transport="stdio")
