# Shared by the serving cluster scripts. Sourced, not run.
# Reads the repository's .env (the same file the drill uses) and applies serving defaults.
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ENV_FILE="${SERVING_ENV_FILE:-.env}"
[ -f "$ENV_FILE" ] || { echo "FATAL: $ENV_FILE not found. cp .env.example .env and fill it in."; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${DO_TOKEN:?set DO_TOKEN in .env}"
export DIGITALOCEAN_ACCESS_TOKEN="$DO_TOKEN"

STATE_DIR="${SERVING_STATE_DIR:-.state}"
SERVING_CLUSTER="${SERVING_CLUSTER:-spot-drill-serving}"
SERVING_REGION="${SERVING_REGION:-${REGION:-}}"
SERVING_K8S_VERSION="${SERVING_K8S_VERSION:-latest}"
SERVING_GPU_SIZE="${SERVING_GPU_SIZE:-${SPOT_SIZE_SLUG:-}}"
SERVING_GPU_MIN="${SERVING_GPU_MIN:-1}"
SERVING_GPU_MAX="${SERVING_GPU_MAX:-2}"
SERVING_CPU_SIZE="${SERVING_CPU_SIZE:-}"
SERVING_CPU_COUNT="${SERVING_CPU_COUNT:-1}"
SERVING_BUDGET_USD="${SERVING_BUDGET_USD:-80}"
SERVING_TAG="${DRILL_TAG:-spot-drill}-serving"
KEDA_VERSION="${KEDA_VERSION:-2.21.1}"
NS=vllm

size_price () {  # $1 = size slug -> hourly price from the live catalogue, empty if unknown
  doctl compute size list --format Slug,PriceHourly --no-header | awk -v s="$1" '$1==s{print $2}'
}

cluster_exists () {
  doctl kubernetes cluster list --format Name --no-header | grep -qx "$SERVING_CLUSTER"
}
