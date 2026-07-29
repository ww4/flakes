#!/usr/bin/env bash
# Run TSV cases (expect<TAB>label<TAB>command, \n = newline) against a guard.
GUARD="${1:?guard path}"; CASES="${2:?tsv path}"
pass=0; fail=0
while IFS=$'\t' read -r expect label raw; do
  [ -z "$expect" ] && continue
  cmd=$(printf '%b' "$raw")
  out=$(jq -n --arg c "$cmd" --arg d "/home/claude" \
        '{tool_name:"Bash",cwd:$d,tool_input:{command:$c}}' | bash "$GUARD" 2>/dev/null)
  if printf '%s' "$out" | jq -e '.hookSpecificOutput.permissionDecision=="deny"' >/dev/null 2>&1; then
    got=deny; why=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason' | cut -c1-40)
  else got=pass; why=""; fi
  if [ "$got" = "$expect" ]; then printf '  [OK]   %-46s %s\n' "$label" "$got"; pass=$((pass+1))
  else printf '  [BAD]  %-46s got=%s want=%s %s\n' "$label" "$got" "$expect" "$why"; fail=$((fail+1)); fi
done < "$CASES"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
