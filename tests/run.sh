#!/bin/sh
# Run from a disposable Python environment; never install/load this plugin in CTFd.
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
CTFD_TEST_SOURCE=${CTFD_TEST_SOURCE:-$repo_root/.test-env/CTFd}
if [ ! -f "$CTFD_TEST_SOURCE/CTFd/__init__.py" ]; then
    echo "Set CTFD_TEST_SOURCE to a separate CTFd 3.8.8 source checkout (see README)." >&2
    exit 2
fi
mkdir -p .test-env/state/logs .test-env/state/uploads
export CTFD_TEST_SOURCE
export PYTHONPATH="$CTFD_TEST_SOURCE:$(dirname -- "$repo_root")${PYTHONPATH:+:$PYTHONPATH}"
export SECRET_KEY=isolated-plugin-tests-only
export FLASK_SKIP_DOTENV=1
export LOG_FOLDER="$repo_root/.test-env/state/logs"
export UPLOAD_FOLDER="$repo_root/.test-env/state/uploads"
export TMPDIR="$repo_root/.test-env/state"
exec "${PYTHON:-python3}" "$repo_root/tests/bootstrap.py" --import-mode=importlib --basetemp="$repo_root/.test-env/pytest" "$@" "$repo_root/tests"
