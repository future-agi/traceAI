#!/usr/bin/env bash
# Legacy entrypoint retained for callers; REASON/known_broken never waive a failure.
# Every package calls this gate, with exact JUnit and actual installed runtime data.
set -euo pipefail
for outcome in "${INSTALL_POETRY:-}" "${INSTALL_HATCH:-}" "${CORE_INSTALL:-}"; do
  case "$outcome" in
    success|skipped) ;;
    *) echo "::error::${PKG_NAME:-package}: install/setup did not succeed"; exit 1 ;;
  esac
done
case "${TESTS:-}" in
  success|failure) ;;
  *) echo "::error::${PKG_NAME:-package}: tests did not run to completion"; exit 1 ;;
esac
: "${INTEGRATION_ID:?missing integration ID}" "${JUNIT_DIR:?missing JUnit directory}" "${TEST_CONTEXT:?missing execution context}"
: "${EXPECTED_SUITES:?missing planned suite count}" "${TEST_EXIT_CODE:?missing runner exit code}"
shopt -s nullglob
files=("$JUNIT_DIR"/*.xml)
if [ "${#files[@]}" -ne "$EXPECTED_SUITES" ] || [ "$EXPECTED_SUITES" -eq 0 ]; then
  echo "::error::${PKG_NAME:-package}: expected $EXPECTED_SUITES nonempty suites, found ${#files[@]}; not tested"
  exit 1
fi
args=()
maximum=0
for file in "${files[@]}"; do
  [ -f "$file.exit-code" ] || { echo "::error::missing invocation exit status for $file"; exit 1; }
  rc=$(cat "$file.exit-code")
  case "$rc" in 0|1|2|3|4|5) ;; *) echo "::error::invalid/crashed invocation exit status"; exit 1 ;; esac
  [ "$rc" -le "$maximum" ] || maximum=$rc
  args+=(--junit "$file" --exit-code "$rc")
done
[ "$maximum" = "$TEST_EXIT_CODE" ] || { echo "::error::aggregate/invocation exit mismatch"; exit 1; }
exec "${VALIDATOR_PYTHON:-python3}" "${GITHUB_WORKSPACE:-$(pwd)}/.github/compat/validate.py" verdict \
  --root "${GITHUB_WORKSPACE:-$(pwd)}" --integration-id "$INTEGRATION_ID" \
  --context "$TEST_CONTEXT" "${args[@]}"
