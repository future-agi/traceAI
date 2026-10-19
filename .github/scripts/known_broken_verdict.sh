#!/usr/bin/env bash
# The "Known-broken verdict" step of .github/workflows/python-tests.yml (tested in test_python_test_plan.py).
# Reads PKG_NAME, REASON, INSTALL_POETRY, INSTALL_HATCH, CORE_INSTALL, TESTS, OWN_TESTS and CORE_TESTS from the environment.
set -eo pipefail

echo "outcomes: install (poetry)=$INSTALL_POETRY install (hatch)=$INSTALL_HATCH fi_instrumentation=$CORE_INSTALL tests=$TESTS"
install_failed=""
[ "$INSTALL_POETRY" = "failure" ] && install_failed="${install_failed:+$install_failed, }install (poetry)"
[ "$INSTALL_HATCH" = "failure" ] && install_failed="${install_failed:+$install_failed, }install (hatch)"
[ "$CORE_INSTALL" = "failure" ] && install_failed="${install_failed:+$install_failed, }fi_instrumentation install"
# Any other Tests outcome (empty, or a renamed step id) is a wiring bug: fail rather than warn.
case "$TESTS" in
  success|failure|cancelled|skipped) ;;
  *)
    echo "::error::$PKG_NAME: unexpected Tests outcome '$TESTS'; check the verdict step's env in python-tests.yml"
    exit 1
    ;;
esac
# The Tests step is skipped only when an earlier step (checkout or setup) failed outright, or
# the job was cancelled: that is not a pass, and the job fails with it.
if [ "$TESTS" = "skipped" ]; then
  echo "::error::$PKG_NAME is known broken ($REASON), but its tests did not run (an earlier step failed, or the job was cancelled)"
  exit 1
fi
# The tests passed (whatever an install step did): the line has to come off the list.
if [ "$TESTS" = "success" ]; then
  if [ "$OWN_TESTS" != "true" ] && [ -z "$CORE_TESTS" ]; then
    echo "::error::$PKG_NAME is on the known-broken list but has no tests; remove its line from .github/python-tests-known-broken.txt"
  else
    echo "::error::$PKG_NAME is on the known-broken list but its tests pass; remove its line from .github/python-tests-known-broken.txt${install_failed:+ (failed in this run: $install_failed)}"
  fi
  exit 1
fi
# The tests failed. After a failed install that may not be the listed reason: say so.
if [ -n "$install_failed" ]; then
  echo "::warning::$PKG_NAME is known broken, but in this run these steps failed: $install_failed; its test results may not reflect the listed reason ($REASON)"
  exit 0
fi
echo "::warning::$PKG_NAME is known broken ($REASON): its failures do not fail the check"
