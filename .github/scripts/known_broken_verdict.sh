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
# A failed install fails the check, listed or not: the package's tests never ran against a working
# install, so neither a pass nor a failure of them says anything about the listed reason.
# The install steps have no continue-on-error, so a failed install skips the Tests step; this
# check comes before the "skipped" one so the error names the install, whatever TESTS says.
if [ -n "$install_failed" ]; then
  echo "::error::$PKG_NAME: $install_failed failed; a failed install fails the check, listed or not"
  exit 1
fi
# The Tests step is skipped when checkout or setup failed outright (a failed install is handled above), or
# the job was cancelled: that is not a pass, and the job fails with it.
if [ "$TESTS" = "skipped" ]; then
  echo "::error::$PKG_NAME is known broken ($REASON), but its tests did not run (an earlier step failed, or the job was cancelled)"
  exit 1
fi
# The tests passed: the line has to come off the list.
if [ "$TESTS" = "success" ]; then
  if [ "$OWN_TESTS" != "true" ] && [ -z "$CORE_TESTS" ]; then
    echo "::error::$PKG_NAME is on the known-broken list but has no tests; remove its line from .github/python-tests-known-broken.txt"
  else
    echo "::error::$PKG_NAME is on the known-broken list but its tests pass; remove its line from .github/python-tests-known-broken.txt"
  fi
  exit 1
fi
echo "::warning::$PKG_NAME is known broken ($REASON): its failures do not fail the check"
