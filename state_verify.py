#!/usr/bin/env python3
"""
state-verify: Programmatic state transition verification for LLM-assisted development.

Enumerates all (transition × concern) cells from a YAML DSL definition,
generates focused prompts for each cell, tracks verification coverage,
and optionally generates TLA+ specifications for formal model checking.
"""

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml


# ─── Data Loading ─────────────────────────────────────────────────────────────

def load_spec(spec_path: str) -> dict:
    """Load and validate a verification matrix spec.

    Required keys: name, rows, columns.
    Optional: states (for TLA+ generation), domain_context.

    Raises:
        ValueError: If the spec is empty, wrong type, or missing required keys.
        yaml.YAMLError: If the YAML is malformed.
        FileNotFoundError: If the file does not exist.
    """
    with open(spec_path, "r") as f:
        spec = yaml.safe_load(f)

    if spec is None:
        raise ValueError(f"Spec file is empty: {spec_path}")

    if not isinstance(spec, dict):
        raise ValueError(
            f"Spec must be a YAML mapping, got {type(spec).__name__}: {spec_path}"
        )

    required = ["name", "rows", "columns"]
    missing = [k for k in required if k not in spec]
    if missing:
        raise ValueError(f"Missing required keys in spec file: {', '.join(missing)}")

    # Expand paths into additional rows
    if "paths" in spec:
        row_map = {r["id"]: r for r in spec["rows"]}
        for path in spec["paths"]:
            seq = path["sequence"]
            steps_desc = " -> ".join(seq)
            # Collect all fields from referenced rows for context
            step_details = []
            for step_id in seq:
                if step_id in row_map:
                    r = row_map[step_id]
                    step_details.append(f"  {step_id}: {r.get('description', r['id'])}")
            # Collect from/to from first and last steps for template compatibility
            first_step = row_map.get(seq[0], {}) if seq else {}
            last_step = row_map.get(seq[-1], {}) if seq else {}
            path_row = {
                "id": f"path:{path['id']}",
                "path_id": path["id"],
                "sequence": steps_desc,
                "description": path.get("description", steps_desc),
                "steps": "\n".join(step_details),
                "is_path": "true",
                "from": first_step.get("from", ""),
                "to": last_step.get("to", ""),
                "trigger": "path",
                "transition_description": path.get("description", steps_desc),
            }
            # Copy any extra fields from the path definition
            for k, v in path.items():
                if k not in ("id", "sequence", "description") and isinstance(v, str):
                    path_row[k] = v
            spec["rows"].append(path_row)

    return spec


def get_store_path(spec_path: str) -> Path:
    """Get the verification store path (JSON) alongside the spec file."""
    spec_dir = Path(spec_path).parent
    spec_stem = Path(spec_path).stem
    return spec_dir / f".{spec_stem}.verified.json"


def _lock_path(spec_path: str) -> Path:
    """Get the lock file path for coordinating store access."""
    return get_store_path(spec_path).with_suffix(".lock")


def load_store(spec_path: str) -> dict:
    """Load verification results store with shared file lock."""
    store_path = get_store_path(spec_path)
    if not store_path.exists():
        return {"cells": {}, "metadata": {"created_at": datetime.now().isoformat()}}

    lock_path = _lock_path(spec_path)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        with open(store_path, "r") as f:
            return json.load(f)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def save_store(spec_path: str, store: dict):
    """Save verification results store atomically with exclusive file lock.

    Uses a dedicated lock file for cross-process coordination
    and write-to-temp-then-rename for crash safety.
    """
    store_path = get_store_path(spec_path)
    store.setdefault("metadata", {})["updated_at"] = datetime.now().isoformat()
    store_dir = store_path.parent

    lock_path = _lock_path(spec_path)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd, tmp_path = tempfile.mkstemp(dir=store_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(store, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, store_path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ─── Cell Key ─────────────────────────────────────────────────────────────────

def cell_key(row_id: str, column_id: str) -> str:
    return f"{row_id}:{column_id}"


def get_all_cells(spec: dict) -> list[dict]:
    """Generate all (row × column) cells from the spec.

    All row fields (except 'id') become template variables for prompt rendering.
    """
    cells = []
    for row in spec["rows"]:
        row_id = row["id"]
        row_vars = {k: v for k, v in row.items() if k not in ("id",)}
        # Alias: description → transition_description for template compat
        if "description" in row_vars and "transition_description" not in row_vars:
            row_vars["transition_description"] = row_vars["description"]

        for col in spec["columns"]:
            cell = {
                "row_id": row_id,
                "column_id": col["id"],
                "prompt_template": col.get("prompt_template", ""),
                "key": cell_key(row_id, col["id"]),
            }
            cell.update(row_vars)
            cells.append(cell)
    return cells


# ─── Prompt Generation ────────────────────────────────────────────────────────

def _build_verified_context(cell: dict, spec: dict, store: dict) -> str:
    """Build context string from previously verified cells in the same row.

    Columns are processed in spec-defined order, so earlier columns'
    responses become context for later columns.
    """
    if not store or "cells" not in store:
        return ""

    row_id = cell["row_id"]
    current_col = cell["column_id"]
    lines = []

    for col in spec["columns"]:
        if col["id"] == current_col:
            break  # Only include columns that come before this one
        key = cell_key(row_id, col["id"])
        if key in store["cells"]:
            resp = store["cells"][key].get("response", {})
            resp_str = json.dumps(resp, ensure_ascii=False, indent=2) if isinstance(resp, dict) else str(resp)
            lines.append(f"[{col['id']}]\n{resp_str}")

    if not lines:
        return ""
    return "--- 同一行の検証済みセル ---\n" + "\n\n".join(lines) + "\n--- ここまで ---"


def render_prompt(cell: dict, spec: dict, store: dict | None = None) -> str:
    """Render a focused prompt for a single verification cell.

    All cell fields (row fields + key/ids) are available as template variables:
    - {domain_context}: spec-level context
    - {verified_context}: previously verified cells for the same row (auto-generated)
    """
    template = cell["prompt_template"]
    fmt_vars = {k: v for k, v in cell.items() if k != "prompt_template"}
    fmt_vars["domain_context"] = spec.get("domain_context", "")
    fmt_vars["verified_context"] = _build_verified_context(cell, spec, store or {})
    return template.format(**fmt_vars)


# ─── Commands ─────────────────────────────────────────────────────────────────

def _row_label(row: dict) -> str:
    """Generate a display label for a matrix row."""
    if "from" in row and "to" in row:
        return f"{row['from']} -> {row['to']}"
    # Generic: use description or id
    return row.get("description", row.get("id", "?"))[:30]


def cmd_enumerate(args):
    """Show the verification matrix."""
    spec = load_spec(args.spec)
    store = load_store(args.spec)
    cells = get_all_cells(spec)
    rows = spec.get("rows", spec.get("transitions", []))
    columns = spec.get("columns", spec.get("concerns", []))

    total = len(cells)
    verified = sum(1 for c in cells if c["key"] in store.get("cells", {}))

    print(f"\n{'=' * 80}")
    print(f"  {spec['name']} — Verification Matrix")
    print(f"  {len(rows)} rows × {len(columns)} columns = {total} cells")
    print(f"  Verified: {verified}/{total} ({verified/total*100:.1f}%)" if total > 0 else "")
    print(f"{'=' * 80}\n")

    # Header
    col_ids = [c["id"] for c in columns]
    header = f"{'row':<35}"
    for cid in col_ids:
        short = cid[:8]
        header += f" {short:>10}"
    print(header)
    print("-" * len(header))

    # Rows
    for r in rows:
        label = _row_label(r)
        row_str = f"{label:<35}"
        for c in columns:
            key = cell_key(r["id"], c["id"])
            if key in store.get("cells", {}):
                row_str += f" {'✅':>9}"
            else:
                row_str += f" {'⬜':>9}"
        print(row_str)

    print()


def cmd_next(args):
    """Get the next unverified cell with its prompt."""
    spec = load_spec(args.spec)
    store = load_store(args.spec)
    cells = get_all_cells(spec)

    unverified = [c for c in cells if c["key"] not in store.get("cells", {})]

    if not unverified:
        print("All cells verified! ✅")
        return

    if args.row:
        unverified = [c for c in unverified if c["row_id"] == args.row]
    if args.column:
        unverified = [c for c in unverified if c["column_id"] == args.column]

    if not unverified:
        print("No matching unverified cells found.")
        return

    cell = unverified[0]
    prompt = render_prompt(cell, spec, store)

    if args.format == "prompt":
        print(prompt)
    elif args.format == "json":
        print(json.dumps({
            "cell_key": cell["key"],
            "row_id": cell["row_id"],
            "column_id": cell["column_id"],
            "prompt": prompt,
            "remaining": len(unverified) - 1,
        }, ensure_ascii=False, indent=2))
    elif args.format == "meta":
        print(f"Cell:       {cell['key']}")
        print(f"Row:        {cell['row_id']}")
        print(f"Column:     {cell['column_id']}")
        print(f"Remaining:  {len(unverified) - 1}")


def cmd_prompt(args):
    """Generate prompt for a specific cell."""
    spec = load_spec(args.spec)
    store = load_store(args.spec)
    cells = get_all_cells(spec)

    target_key = cell_key(args.row, args.column)
    cell = next((c for c in cells if c["key"] == target_key), None)

    if not cell:
        print(f"Error: Cell '{target_key}' not found.", file=sys.stderr)
        sys.exit(1)

    print(render_prompt(cell, spec, store))


def cmd_record(args):
    """Record a verification result for a cell."""
    spec = load_spec(args.spec)
    store = load_store(args.spec)
    cells = get_all_cells(spec)

    target_key = cell_key(args.row, args.column)
    cell = next((c for c in cells if c["key"] == target_key), None)

    if not cell:
        print(f"Error: Cell '{target_key}' not found.", file=sys.stderr)
        sys.exit(1)

    if args.response:
        response_text = args.response
    elif args.response_file:
        with open(args.response_file, "r") as f:
            response_text = f.read()
    else:
        print("Reading response from stdin (Ctrl+D to finish)...")
        response_text = sys.stdin.read()

    try:
        response_data = json.loads(response_text)
    except json.JSONDecodeError:
        response_data = {"raw_text": response_text}

    if "cells" not in store:
        store["cells"] = {}

    store["cells"][target_key] = {
        "row_id": cell["row_id"],
        "column_id": cell["column_id"],
        "response": response_data,
        "verified_at": datetime.now().isoformat(),
    }

    save_store(args.spec, store)
    total = len(cells)
    spec_keys = {c["key"] for c in cells}
    verified = len(set(store["cells"].keys()) & spec_keys)
    print(f"Recorded: {target_key}")
    print(f"Progress: {verified}/{total} ({verified/total*100:.1f}%)")


def cmd_coverage(args):
    """Show coverage report with gap analysis."""
    spec = load_spec(args.spec)
    store = load_store(args.spec)
    cells = get_all_cells(spec)

    total = len(cells)
    stored_keys = set(store.get("cells", {}).keys())
    spec_keys = {c["key"] for c in cells}
    verified_keys = stored_keys & spec_keys
    verified = len(verified_keys)
    unverified = [c for c in cells if c["key"] not in stored_keys]

    print(f"\n{'=' * 60}")
    print(f"  Coverage Report: {spec['name']}")
    print(f"{'=' * 60}")
    print(f"  Total cells:    {total}")
    print(f"  Verified:       {verified}")
    print(f"  Remaining:      {total - verified}")
    print(f"  Coverage:       {verified/total*100:.1f}%" if total > 0 else "")
    print(f"{'=' * 60}\n")

    if unverified:
        print("Unverified cells:")
        print("-" * 50)

        by_row = {}
        for c in unverified:
            by_row.setdefault(c["row_id"], []).append(c["column_id"])

        for row_id, cols in by_row.items():
            print(f"  {row_id}")
            for col in cols:
                print(f"    - {col}")
        print()

    # Coverage by column
    print("Coverage by column:")
    print("-" * 50)
    for c in spec["columns"]:
        col_cells = [cell for cell in cells if cell["column_id"] == c["id"]]
        col_verified = sum(1 for cell in col_cells if cell["key"] in verified_keys)
        pct = col_verified / len(col_cells) * 100 if col_cells else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {c['id']:<20} {bar} {col_verified}/{len(col_cells)} ({pct:.0f}%)")
    print()


def cmd_batch_prompts(args):
    """Generate all unverified prompts as a JSONL file for batch processing."""
    spec = load_spec(args.spec)
    store = load_store(args.spec)
    cells = get_all_cells(spec)

    unverified = [c for c in cells if c["key"] not in store.get("cells", {})]

    if not unverified:
        print("All cells verified!", file=sys.stderr)
        return

    output = []
    for cell in unverified:
        output.append({
            "cell_key": cell["key"],
            "row_id": cell["row_id"],
            "column_id": cell["column_id"],
            "prompt": render_prompt(cell, spec, store),
        })

    if args.output:
        with open(args.output, "w") as f:
            for item in output:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"Written {len(output)} prompts to {args.output}", file=sys.stderr)
    else:
        for item in output:
            print(json.dumps(item, ensure_ascii=False))


def cmd_tlaplus(args):
    """Generate TLA+ specification from the state machine definition."""
    spec = load_spec(args.spec)
    store = load_store(args.spec)

    import re as _re
    module_name = _re.sub(r'[^A-Za-z0-9]', '', spec["name"])

    # Collect states
    states = spec.get("states", {})
    if isinstance(states, dict):
        state_names = list(states.keys())
    elif isinstance(states, list):
        state_names = states
    else:
        state_names = []

    if not state_names:
        raise ValueError("Spec has no states defined; cannot generate TLA+ spec")

    rows = spec["rows"]

    # Collect invariants from any verified cell whose response has "invariants" key
    invariants = []
    for r in rows:
        for col in spec["columns"]:
            key = cell_key(r["id"], col["id"])
            if key in store.get("cells", {}):
                resp = store["cells"][key].get("response", {})
                if isinstance(resp, dict) and "invariants" in resp:
                    for inv in resp["invariants"]:
                        invariants.append({
                            "row": r.get("description", r["id"]),
                            "condition": inv.get("condition", ""),
                        })

    # Build TLA+ spec
    tla = []
    tla.append(f"---- MODULE {module_name} ----")
    tla.append(f"EXTENDS Naturals, Sequences, FiniteSets, TLC")
    tla.append("")
    tla.append("VARIABLES state")
    tla.append("")

    # States as constants
    states_str = ", ".join(f'"{s}"' for s in state_names)
    tla.append(f"States == {{{states_str}}}")
    tla.append("")

    # Init
    initial_state = state_names[0]
    tla.append(f"Init ==")
    tla.append(f'  /\\ state = "{initial_state}"')
    tla.append("")

    # Rows as individual actions (only non-path rows with from/to)
    transition_rows = [r for r in rows if "from" in r and "to" in r and r.get("is_path") != "true"]
    for r in transition_rows:
        action_name = r["id"].replace("-", "_").title().replace("_", "")
        if r.get("trigger"):
            action_name = r["trigger"].replace("-", "_").title().replace("_", "")
        tla.append(f"\\* {r.get('description', r['id'])}")
        tla.append(f"{action_name} ==")
        tla.append(f'  /\\ state = "{r["from"]}"')
        tla.append(f'  /\\ state\' = "{r["to"]}"')
        tla.append("")

    # Next state relation
    tla.append("Next ==")
    action_names = []
    for r in transition_rows:
        action_name = r["id"].replace("-", "_").title().replace("_", "")
        if r.get("trigger"):
            action_name = r["trigger"].replace("-", "_").title().replace("_", "")
        action_names.append(action_name)
    if action_names:
        tla.append("  \\/ " + "\n  \\/ ".join(action_names))
    else:
        tla.append("  FALSE \\* No transitions defined")
    tla.append("")

    # Spec
    tla.append("Spec == Init /\\ [][Next]_state")
    tla.append("")

    # Type invariant
    tla.append("TypeInvariant ==")
    tla.append("  state \\in States")
    tla.append("")

    # Terminal states don't have outgoing transitions
    terminal_states = set(state_names) - {r["from"] for r in transition_rows}
    if terminal_states:
        tla.append("\\* Terminal states (no outgoing transitions)")
        tla.append(f"TerminalStates == {{{', '.join(chr(34)+s+chr(34) for s in terminal_states)}}}")
        tla.append("")

    # Reachability check: can all states be reached?
    tla.append("\\* Reachability properties (use TLC model checker)")
    for s in state_names:
        prop_name = f"CanReach{s.replace('_', ' ').title().replace(' ', '')}"
        tla.append(f'{prop_name} == <>(state = "{s}")')
    tla.append("")

    # Safety invariants from verified cells
    if invariants:
        tla.append("\\* Invariants derived from verification")
        for i, inv in enumerate(invariants):
            tla.append(f"\\* From: {inv['row']}")
            tla.append(f"\\* {inv['condition']}")
        tla.append("")

    # Path-based temporal properties from paths section
    if spec.get("paths"):
        tla.append("\\* Path properties (from paths section)")
        row_map = {r["id"]: r for r in rows if "from" in r and "to" in r}
        for p in spec["paths"]:
            seq = p["sequence"]
            # Build a sequence of states this path visits
            path_states = []
            for step_id in seq:
                if step_id in row_map:
                    r = row_map[step_id]
                    if not path_states:
                        path_states.append(r["from"])
                    path_states.append(r["to"])
            if len(path_states) >= 2:
                prop_name = f"Path{p['id'].replace('-', '_').title().replace('_', '')}"
                tla.append(f"\\* {p.get('description', p['id'])}")
                # Generate leads-to chain: state1 ~> state2 ~> ... ~> stateN
                for i in range(len(path_states) - 1):
                    tla.append(f'{prop_name}Step{i+1} == (state = "{path_states[i]}") ~> (state = "{path_states[i+1]}")')
                # Full path reachability
                tla.append(f'{prop_name}Full == (state = "{path_states[0]}") ~> (state = "{path_states[-1]}")')
                tla.append("")

    # Suggested invariants (common patterns)
    tla.append("\\* === Suggested invariants (customize these) ===")
    tla.append("\\* Add domain-specific variables and invariants below.")
    tla.append("\\* Example with payment and inventory tracking:")
    tla.append("\\*")
    tla.append("\\* VARIABLES state, payment_status, inventory_reserved")
    tla.append("\\*")
    tla.append('\\* PaymentInvariant == (state = "shipped") => (payment_status = "paid")')
    tla.append('\\* InventoryInvariant == (state = "cancelled") => (inventory_reserved = FALSE)')
    tla.append('\\* RefundInvariant == (state = "refunded") => (payment_status = "refunded")')
    tla.append("")

    tla.append(f"==== \\* END MODULE {module_name} ====")

    output_text = "\n".join(tla)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_text)
        print(f"TLA+ spec written to {args.output}", file=sys.stderr)
    else:
        print(output_text)


def cmd_export(args):
    """Export all verified results as a structured report."""
    spec = load_spec(args.spec)
    store = load_store(args.spec)
    cells = get_all_cells(spec)

    spec_keys = {c["key"] for c in cells}
    report = {
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

    output_text = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_text)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output_text)


def cmd_reset(args):
    """Reset verification store (clear all recorded results)."""
    store_path = get_store_path(args.spec)
    if store_path.exists():
        if not args.yes:
            answer = input(f"Reset all verification data for {args.spec}? [y/N] ")
            if answer.lower() != "y":
                print("Aborted.")
                return
        store_path.unlink()
        print("Verification store reset.")
    else:
        print("No verification store found.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="state-verify",
        description="Programmatic state transition verification for LLM-assisted development.",
    )
    parser.add_argument("--spec", "-s", default="order-states.yaml",
                        help="Path to the state machine YAML spec")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # enumerate
    sub = subparsers.add_parser("enumerate", aliases=["enum"],
                                help="Show verification matrix")

    # next
    sub = subparsers.add_parser("next",
                                help="Get next unverified cell")
    sub.add_argument("--format", choices=["prompt", "json", "meta"], default="json",
                     help="Output format")
    sub.add_argument("--row", "-r", help="Filter by row ID")
    sub.add_argument("--column", "-c", help="Filter by column ID")

    # prompt
    sub = subparsers.add_parser("prompt",
                                help="Generate prompt for a specific cell")
    sub.add_argument("row", help="Row ID (e.g., t1)")
    sub.add_argument("column", help="Column ID (e.g., preconditions)")

    # record
    sub = subparsers.add_parser("record",
                                help="Record verification result")
    sub.add_argument("row", help="Row ID")
    sub.add_argument("column", help="Column ID")
    sub.add_argument("--response", "-r", help="Response text or JSON string")
    sub.add_argument("--response-file", "-f", help="Read response from file")

    # coverage
    sub = subparsers.add_parser("coverage", aliases=["cov"],
                                help="Show coverage report")

    # batch-prompts
    sub = subparsers.add_parser("batch-prompts", aliases=["batch"],
                                help="Generate all unverified prompts as JSONL")
    sub.add_argument("--output", "-o", help="Output file path")

    # tlaplus
    sub = subparsers.add_parser("tlaplus", aliases=["tla"],
                                help="Generate TLA+ specification")
    sub.add_argument("--output", "-o", help="Output file path")

    # export
    sub = subparsers.add_parser("export",
                                help="Export verification report as JSON")
    sub.add_argument("--output", "-o", help="Output file path")

    # reset
    sub = subparsers.add_parser("reset",
                                help="Reset verification store")
    sub.add_argument("--yes", "-y", action="store_true",
                     help="Skip confirmation")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    cmd_map = {
        "enumerate": cmd_enumerate, "enum": cmd_enumerate,
        "next": cmd_next,
        "prompt": cmd_prompt,
        "record": cmd_record,
        "coverage": cmd_coverage, "cov": cmd_coverage,
        "batch-prompts": cmd_batch_prompts, "batch": cmd_batch_prompts,
        "tlaplus": cmd_tlaplus, "tla": cmd_tlaplus,
        "export": cmd_export,
        "reset": cmd_reset,
    }

    handler = cmd_map.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
