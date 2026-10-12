#!/usr/bin/env bash
# Install the chart in a kind cluster and check that it works.
#
# What this shows: the images build, the install job migrates a real PostgreSQL and
# enrols the runner, every pod becomes ready as a non-root user on a read-only root
# filesystem (`helm test`), the runner connects to the API over TLS and is recognised
# by its key, a second `helm upgrade` changes nothing it should not, and a database
# restored from a backup still verifies and is still protected.
#
# What it does not show: anything about a real ServiceNow, Autosys or Splunk. No
# connector is configured, so no connector call is made.
#
# Needs docker, kind, kubectl, helm and python with requirements.txt installed.
# KIND, HELM, KUBECTL and PYTHON name the commands if they are not on PATH.
set -euo pipefail

KIND="${KIND:-kind}"
HELM="${HELM:-helm}"
KUBECTL="${KUBECTL:-kubectl}"
PYTHON="${PYTHON:-python}"
CLUSTER="${CLUSTER:-l1-install-test}"
NS=aiops
RELEASE=aiops
FULL="${RELEASE}-l1-and-l2-ai-ops"
CHART=deploy/helm/l1-and-l2-ai-ops
# Keys and certificates are written here and removed afterwards. WORK names an empty
# directory to use instead of a temporary one.
if [ -n "${WORK:-}" ]; then mkdir -p "$WORK"; else WORK="$(mktemp -d)"; fi
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n== %s\n' "$*"; }
k() { "$KUBECTL" --context "kind-${CLUSTER}" -n "$NS" "$@"; }

say "Images"
docker build -q -t l1-and-l2-ai-ops:ci .
docker build -q --target ui -t l1-and-l2-ai-ops-ui:ci .

say "Cluster"
"$KIND" get clusters | grep -qx "$CLUSTER" || "$KIND" create cluster --name "$CLUSTER" --wait 120s
"$KIND" load docker-image --name "$CLUSTER" l1-and-l2-ai-ops:ci l1-and-l2-ai-ops-ui:ci
# A cluster left from an earlier run still has that run's database and that run's keys.
# This test makes new keys every time, so it starts from an empty namespace.
"$KUBECTL" --context "kind-${CLUSTER}" delete namespace "$NS" --ignore-not-found --wait=true
"$KUBECTL" --context "kind-${CLUSTER}" create namespace "$NS"

say "PostgreSQL, with the two roles an operator creates"
k apply -f tools/ci/postgres.yaml
k rollout status deployment/postgres --timeout=180s
k exec deploy/postgres -- psql -U postgres -v ON_ERROR_STOP=1 -c "
  DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'l1_app') THEN
      CREATE ROLE l1_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aiops_login') THEN
      CREATE ROLE aiops_login LOGIN PASSWORD 'install-test-app' IN ROLE l1_app;
    END IF;
  END \$\$;"

say "Keys and certificates (made here; in a deployment each is made where it is kept)"
for name in dispatch grant runner; do
  "$PYTHON" -m src.l1_agent.cli runner keygen --out "$WORK/$name.key" >/dev/null
done
"$PYTHON" tools/ci/make_tls.py "$WORK/tls" \
  "${FULL}-api.${NS}.svc" "${FULL}-api" "${FULL}-api.${NS}.svc.cluster.local"

secret() { k create secret generic "$@" --dry-run=client -o yaml | k apply -f -; }
secret aiops-database \
  --from-literal=url="postgresql+asyncpg://aiops_login:install-test-app@postgres:5432/postgres" \
  --from-literal=migrationUrl="postgresql+asyncpg://postgres:install-test@postgres:5432/postgres"
secret aiops-secrets --from-literal=webhookSecret="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')"
secret aiops-dispatch-key --from-file=dispatch.key="$WORK/dispatch.key"
secret aiops-grant-key --from-file=grant.key="$WORK/grant.key"
secret aiops-runner-key --from-file=runner.key="$WORK/runner.key"
secret aiops-api-tls --from-file=tls.crt="$WORK/tls/tls.crt" --from-file=tls.key="$WORK/tls/tls.key" \
  --from-file=ca.crt="$WORK/tls/ca.crt"

install() {
  "$HELM" upgrade --install "$RELEASE" "$CHART" --kube-context "kind-${CLUSTER}" -n "$NS" \
    -f "$CHART/ci/test-values.yaml" \
    --set image.repository=l1-and-l2-ai-ops --set image.tag=ci \
    --set ui.image.repository=l1-and-l2-ai-ops-ui --set ui.image.tag=ci \
    --set api.replicas=1 --set worker.replicas=1 --set ui.replicas=1 \
    --set runner.publicKey="$(cat "$WORK/runner.key.pub")" \
    --set runner.dispatchPublicKey="$(cat "$WORK/dispatch.key.pub")" \
    --set runner.grantPublicKey="$(cat "$WORK/grant.key.pub")" \
    --wait --timeout 6m
}

say "helm install"
install || { k get pods; k describe pods | tail -60; k logs -l app.kubernetes.io/component=install --all-containers --tail=60 || true; exit 1; }

say "helm test: non-root, read-only root filesystem, API answers"
"$HELM" test "$RELEASE" --kube-context "kind-${CLUSTER}" -n "$NS" --logs

say "What each container is really running as"
for component in api worker runner ui; do
  pod="$(k get pod -l "app.kubernetes.io/component=${component}" -o jsonpath='{.items[0].metadata.name}')"
  uid="$(k exec "$pod" -- id -u)"
  [ "$uid" != "0" ] || { echo "$component runs as root"; exit 1; }
  if k exec "$pod" -- sh -c 'touch /written-by-test' 2>/dev/null; then
    echo "$component can write to its root filesystem"; exit 1
  fi
  echo "$component: uid $uid, root filesystem read only"
done

say "The runner is enrolled, connected over TLS and recognised by its key"
connected=""
for _ in $(seq 1 30); do
  listing="$(k exec deploy/"${FULL}"-api -- python -m src.l1_agent.cli runner list)"
  if printf '%s\n' "$listing" | grep -q "runner-1.*connected"; then connected=yes; break; fi
  sleep 2
done
printf '%s\n' "$listing"
[ -n "$connected" ] || { k logs deploy/"${FULL}"-runner --tail=40; exit 1; }

say "What is left to set up, as the product reports it"
k exec deploy/"${FULL}"-api -- python -m src.l1_agent.cli onboarding || [ $? -eq 3 ]

say "The audit log of the new tenant verifies"
k exec deploy/"${FULL}"-api -- python -m src.l1_agent.cli audit verify

say "helm upgrade: the install job runs again and changes nothing"
install
k exec deploy/"${FULL}"-api -- python -m src.l1_agent.cli runner list | grep -c "runner-1" | grep -qx 1
events="$(k exec deploy/"${FULL}"-api -- python -m src.l1_agent.cli audit verify | sed -n 's/^events checked: \([0-9]*\).*/\1/p')"
echo "audit events after the second install: $events"
[ "$events" = "2" ] || { echo "expected tenant.created and runner.enrolled, once each"; exit 1; }

say "Restore drill: back the database up, restore it beside the original, verify the copy"
k exec deploy/postgres -- sh -c '
  set -e
  pg_dump -U postgres --format=custom --file=/tmp/backup.dump postgres
  dropdb -U postgres --if-exists restored
  createdb -U postgres restored
  pg_restore -U postgres --dbname=restored --exit-on-error /tmp/backup.dump'
restored="postgresql+asyncpg://aiops_login:install-test-app@postgres:5432/restored"
# As the application role, against the copy: the audit chain still verifies, row level
# security came back with the tables, and the audit log still cannot be changed.
k exec deploy/"${FULL}"-api -- env DATABASE_URL="$restored" python -m src.l1_agent.cli audit verify
k exec deploy/"${FULL}"-api -- env DATABASE_URL="$restored" python -m src.l1_agent.cli runner list \
  | grep -q "runner-1"
if k exec deploy/postgres -- psql -U aiops_login -d restored -v ON_ERROR_STOP=1 \
    -c "DELETE FROM audit_events" >/dev/null 2>&1; then
  echo "the application role could delete audit events in the restored database"; exit 1
fi
visible="$(k exec deploy/postgres -- psql -U aiops_login -d restored -At -c "SELECT count(*) FROM audit_events")"
[ "$visible" = "0" ] || { echo "rows were visible with no tenant set: $visible"; exit 1; }
echo "restored copy: chain verifies, audit log append-only, nothing visible without a tenant"

say "Passed"
