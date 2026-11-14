#!/bin/zsh
# Start Airflow 3 (api-server on :8080, scheduler, dag-processor, triggerer) from its own venv.
# The admin password is generated into $AIRFLOW_HOME/simple_auth_manager_passwords.json.generated
set -euo pipefail
cd "${0:A:h}/.."
set -a; source .env; set +a
export PATH="$DATA_ROOT/venv-airflow/bin:$PATH"  # standalone launches its parts via `airflow`
export AIRFLOW__CORE__DAGS_FOLDER="$PWD/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS=admin:admin
# macOS: forked task processes crash without these
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES NO_PROXY="*"
exec airflow standalone
