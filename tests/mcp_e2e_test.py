#!/usr/bin/env python3
"""End-to-end MCP protocol test for state-verify."""
import subprocess, json, sys

SPEC = "examples/order-states.yaml"

requests = [
    # Initialize
    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}},
    # 1. sv_guide
    {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"sv_guide","arguments":{}}},
    # 2. sv_enumerate
    {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"sv_enumerate","arguments":{}}},
    # 3. sv_next
    {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"sv_next","arguments":{}}},
    # 4. sv_next with filter
    {"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"sv_next","arguments":{"row_id":"t1"}}},
    # 5. sv_prompt
    {"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"sv_prompt","arguments":{"row_id":"t1","column_id":"preconditions"}}},
    # 6. sv_record (path row)
    {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"sv_record","arguments":{"row_id":"path:happy_path","column_id":"preconditions","response_json":"{\"test\":true}"}}},
    # 7. sv_coverage
    {"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"sv_coverage","arguments":{}}},
    # 8. sv_batch_prompts
    {"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"sv_batch_prompts","arguments":{}}},
    # 9. sv_export
    {"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"sv_export","arguments":{}}},
    # 10. sv_tests (pytest)
    {"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"sv_tests","arguments":{"row_id":"t1"}}},
    # 11. sv_tests (hypothesis-stateful)
    {"jsonrpc":"2.0","id":20,"method":"tools/call","params":{"name":"sv_tests","arguments":{"framework":"hypothesis-stateful"}}},
    # 12. sv_tests (mutmut)
    {"jsonrpc":"2.0","id":21,"method":"tools/call","params":{"name":"sv_tests","arguments":{"framework":"mutmut"}}},
    # 13. sv_tlaplus
    {"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"sv_tlaplus","arguments":{}}},
    # 12. sv_tlaplus with output
    {"jsonrpc":"2.0","id":13,"method":"tools/call","params":{"name":"sv_tlaplus","arguments":{"output_path":"/private/tmp/claude-501/test_tla.tla"}}},
    # 13. sv_reset
    {"jsonrpc":"2.0","id":14,"method":"tools/call","params":{"name":"sv_reset","arguments":{}}},
    # Error cases
    # 14. Invalid cell
    {"jsonrpc":"2.0","id":15,"method":"tools/call","params":{"name":"sv_record","arguments":{"row_id":"bad","column_id":"bad","response_json":"{}"}}},
    # 15. Invalid spec path
    {"jsonrpc":"2.0","id":16,"method":"tools/call","params":{"name":"sv_enumerate","arguments":{"spec_path":"/nonexistent.yaml"}}},
    # 16. Invalid JSON response
    {"jsonrpc":"2.0","id":17,"method":"tools/call","params":{"name":"sv_record","arguments":{"row_id":"t1","column_id":"preconditions","response_json":"not json"}}},
    # 17. sv_next on empty (after reset)
    {"jsonrpc":"2.0","id":18,"method":"tools/call","params":{"name":"sv_next","arguments":{}}},
    # 18. sv_reset again (no store)
    {"jsonrpc":"2.0","id":19,"method":"tools/call","params":{"name":"sv_reset","arguments":{}}},
]

input_data = "\n".join(json.dumps(r) for r in requests)
proc = subprocess.run(
    [".venv/bin/python3", "mcp_server.py", "--spec", SPEC],
    input=input_data, capture_output=True, text=True, timeout=30,
)

TOOL_NAMES = {
    2: "sv_guide", 3: "sv_enumerate", 4: "sv_next", 5: "sv_next(filter)",
    6: "sv_prompt", 7: "sv_record", 8: "sv_coverage", 9: "sv_batch_prompts",
    10: "sv_export", 11: "sv_tests(pytest)", 12: "sv_tlaplus", 13: "sv_tlaplus(output)",
    14: "sv_reset",
    15: "err:invalid_cell", 16: "err:bad_spec", 17: "err:bad_json",
    18: "sv_next(post_reset)", 19: "sv_reset(no_store)",
    20: "sv_tests(hypothesis)", 21: "sv_tests(mutmut)",
}

passed = 0
failed = 0
for line in proc.stdout.strip().split("\n"):
    try:
        resp = json.loads(line)
    except json.JSONDecodeError:
        continue

    rid = resp.get("id")
    if rid == 1:  # skip initialize
        continue

    name = TOOL_NAMES.get(rid, f"id={rid}")
    result = resp.get("result", {})
    content = result.get("content", [{}])
    text = content[0].get("text", "") if content else ""

    try:
        data = json.loads(text)
        status = data.get("status", "no-status")
    except (json.JSONDecodeError, TypeError):
        status = "PARSE_ERROR"

    # Expected statuses
    expected = {
        2: "ok", 3: "ok", 4: "pending", 5: "filtered_empty",  # 64/104 verified (paths unverified), t1 all cols done
        6: "ok", 7: "recorded", 8: "ok", 9: "ok",
        10: "ok", 11: "ok", 12: "ok", 13: "ok", 14: "reset",
        15: "error", 16: "error", 17: "recorded",  # bad json still records with warning
        18: "pending", 19: "reset",  # id=17 re-created store, so reset succeeds
        20: "ok", 21: "ok",  # hypothesis-stateful, mutmut
    }

    exp = expected.get(rid, "?")
    ok = status == exp
    mark = "✅" if ok else "❌"
    if ok:
        passed += 1
    else:
        failed += 1
    extra = ""
    if rid == 17:
        extra = f" warning={data.get('warning','none')}"
    if rid == 13:
        extra = f" saved_to={data.get('saved_to','none')} tla_len={len(data.get('tla_spec',''))}"
    if rid == 20:
        extra = f" states={len(data.get('states',[]))} transitions={data.get('transitions',0)} code_len={len(data.get('code',''))}"
    if rid == 21:
        extra = f" properties={data.get('properties_to_protect',0)}"
    print(f"  {mark} {name:25s} status={status:15s} (expected={exp}){extra}")

print(f"\n{'='*50}")
print(f"  Passed: {passed}/{passed+failed}, Failed: {failed}")
