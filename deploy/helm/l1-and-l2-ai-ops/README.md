# l1-and-l2-ai-ops

L1 and L2 AI-Ops for one tenant, in your own cluster: the API, workers, the dashboard, and a runner that makes the connector calls.

**Read [docs/operator-guide.md](../../../docs/operator-guide.md) before installing.** It has the steps in order: the database roles, the three key pairs, the Secrets this chart expects to find, TLS between the runner and the API, and what to do after the install.

This chart has been installed in a local kind cluster by `tools/ci/kind_install.sh`. It has not been installed in a managed Kubernetes service.

## What it does and does not do

- It creates no secret. Every credential and key is in a Secret you make first and name in the values.
- Every container runs as a non-root user with a read-only root filesystem, no added capabilities and no Kubernetes API token. The values schema refuses changes that would undo that, and `helm test` checks it from inside a running container.
- Before each install and upgrade a job applies the schema migrations, makes sure the tenant exists and enrols the runner's public key. It is safe to run again.
- The key that authorises a write is mounted in workers only. The runner gets no database credential. Only the install job gets the migration role.
- Network policies let nothing connect to a worker or a runner, and let only the dashboard, the runner and the peers you name reach the API. They are enforced only if your cluster's network plug-in enforces them.
- It does not restrict outbound traffic. Add egress rules for the runner.
- It does not deploy PostgreSQL, an ingress controller or a secrets manager.
- The runner starts capped at reads (`runner.maxTier: T0`). Section 5 of the operator guide says what has to be true before it makes a change.

## Values you must set

| Value | What |
|---|---|
| `image.repository`, `ui.image.repository` | Where the two images are. Prefer `digest` to `tag` |
| `database.existingSecret` | Secret with `url` (application role) and `migrationUrl` (migration role) |
| `secrets.existingSecret` | Secret with `webhookSecret`, at least 32 characters |
| `controlPlane.dispatchKeySecret` | Secret with `dispatch.key` |
| `runner.keySecret`, `runner.publicKey` | Secret with `runner.key`, and its public half |
| `runner.dispatchPublicKey` | The public half of `dispatch.key` |
| `api.tls.secretName` or `runner.controlPlaneUrl` | How the runner reaches the API over TLS |

For writes, also `controlPlane.grantKeySecret` and `runner.grantPublicKey`. `values.yaml` describes every value.

To run without a runner, with connector calls made in the workers, set `controlPlane.runnerMode: embedded`. Connector credentials are then read by the workers.

## Checks

```bash
helm lint --strict . -f ci/test-values.yaml
HELM_BIN=$(command -v helm) pytest ../../../tests/deploy        # the rendered manifests
bash ../../../tools/ci/kind_install.sh                          # a real install in kind (run from the repository root)
helm test <release> -n <namespace> --logs                       # after any install
```
