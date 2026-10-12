"""The Helm chart, rendered with its default values (Phase 6 exit criterion 3).

`helm template` is run on the chart with the least a real install has to set
(`ci/test-values.yaml`, which touches no security setting), and the manifests
that come out are checked. The chart also carries a `helm test` pod that makes
the same checks from inside a running container; CI runs it in a kind cluster.

Needs the `helm` command (or HELM_BIN). Without it these tests are skipped,
unless L1_REQUIRE_HELM=1 (set in CI), in which case they fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "l1-and-l2-ai-ops"
VALUES = CHART / "ci" / "test-values.yaml"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
WORKLOADS = ("Deployment", "Job", "Pod")


def helm(*args: str) -> subprocess.CompletedProcess:
    if not HELM:
        if os.environ.get("L1_REQUIRE_HELM") == "1":
            pytest.fail("L1_REQUIRE_HELM=1 but the helm command was not found")
        pytest.skip("the helm command is not installed")
    return subprocess.run([HELM, *args], capture_output=True, text=True, timeout=120)


def render(*sets: str, values: bool = True) -> List[Dict[str, Any]]:
    args = ["template", "rel", str(CHART), "--namespace", "aiops"]
    if values:
        args += ["-f", str(VALUES)]
    for item in sets:
        args += ["--set", item]
    done = helm(*args)
    assert done.returncode == 0, done.stderr
    return [doc for doc in yaml.safe_load_all(done.stdout) if doc]


def refused(*sets: str, values: bool = True) -> str:
    args = ["template", "rel", str(CHART), "--namespace", "aiops"]
    if values:
        args += ["-f", str(VALUES)]
    for item in sets:
        args += ["--set", item]
    done = helm(*args)
    assert done.returncode != 0, "the chart rendered; it should have refused these values"
    return done.stderr


def pod_spec(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc["spec"] if doc["kind"] == "Pod" else doc["spec"]["template"]["spec"]


def workloads(docs: List[Dict[str, Any]]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    for doc in docs:
        if doc["kind"] in WORKLOADS:
            yield f"{doc['kind']}/{doc['metadata']['name']}", pod_spec(doc)


def containers(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(spec.get("initContainers", [])) + list(spec["containers"])


def named(docs: List[Dict[str, Any]], kind: str, suffix: str) -> Dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]
    assert len(found) == 1, f"{kind} *{suffix}: {[d['metadata']['name'] for d in found]}"
    return found[0]


def env_of(container: Dict[str, Any]) -> Dict[str, Any]:
    return {e["name"]: e for e in container.get("env", [])}


@pytest.fixture(scope="module")
def docs() -> List[Dict[str, Any]]:
    return render()


class TestLint:
    def test_the_chart_passes_strict_lint(self):
        done = helm("lint", "--strict", str(CHART), "-f", str(VALUES))
        assert done.returncode == 0, done.stdout + done.stderr

    def test_the_test_values_set_nothing_about_security(self):
        text = VALUES.read_text(encoding="utf-8")
        for word in ("securityContext", "runAsNonRoot", "readOnlyRootFilesystem", "networkPolicy"):
            assert word not in text, f"{word}: the checks below would not be about the defaults"


class TestEveryContainerIsLockedDown:
    """Exit criterion 3: non-root with a read-only root filesystem, by default."""

    def test_there_is_something_to_check(self, docs):
        names = sorted(name for name, _ in workloads(docs))
        assert names == [
            "Deployment/rel-l1-and-l2-ai-ops-api",
            "Deployment/rel-l1-and-l2-ai-ops-runner",
            "Deployment/rel-l1-and-l2-ai-ops-ui",
            "Deployment/rel-l1-and-l2-ai-ops-worker",
            "Job/rel-l1-and-l2-ai-ops-install-1",
            "Pod/rel-l1-and-l2-ai-ops-test-hardening",
        ]

    def test_every_pod_runs_as_a_named_non_root_user(self, docs):
        for name, spec in workloads(docs):
            context = spec["securityContext"]
            assert context["runAsNonRoot"] is True, name
            assert context["runAsUser"] > 0 and context["runAsGroup"] > 0, name
            assert context["seccompProfile"] == {"type": "RuntimeDefault"}, name
            assert spec["automountServiceAccountToken"] is False, name
            for forbidden in ("hostNetwork", "hostPID", "hostIPC"):
                assert not spec.get(forbidden), f"{name} sets {forbidden}"

    def test_every_container_has_a_read_only_root_filesystem_and_no_privileges(self, docs):
        checked = 0
        for name, spec in workloads(docs):
            for container in containers(spec):
                where = f"{name}/{container['name']}"
                context = container["securityContext"]
                assert context["readOnlyRootFilesystem"] is True, where
                assert context["allowPrivilegeEscalation"] is False, where
                assert context["runAsNonRoot"] is True, where
                assert context["capabilities"] == {"drop": ["ALL"]}, where
                assert not context.get("privileged"), where
                checked += 1
        assert checked >= 8

    def test_the_only_writable_places_are_scratch_volumes(self, docs):
        for name, spec in workloads(docs):
            kinds = {v["name"]: sorted(set(v) - {"name"})[0] for v in spec.get("volumes", [])}
            assert "hostPath" not in kinds.values(), name
            for container in containers(spec):
                for mount in container.get("volumeMounts", []):
                    kind = kinds[mount["name"]]
                    if kind == "emptyDir":
                        assert mount["mountPath"] in ("/tmp", "/etc/nginx/conf.d"), name
                    else:
                        assert kind in ("secret", "configMap"), name
                        assert mount.get("readOnly") is True, f"{name}: {mount['name']}"
            for volume in spec.get("volumes", []):
                if "emptyDir" in volume:
                    assert volume["emptyDir"].get("sizeLimit"), f"{name}: {volume['name']}"

    def test_images_are_named_exactly_and_resources_are_bounded(self, docs):
        for name, spec in workloads(docs):
            for container in containers(spec):
                image = container["image"]
                assert image.startswith("registry.example.com/"), image
                assert not image.endswith(":latest") and (":" in image or "@" in image), image
        for component in ("api", "worker", "runner", "ui"):
            spec = pod_spec(named(docs, "Deployment", f"-{component}"))
            limits = spec["containers"][0]["resources"]["limits"]
            assert limits["memory"] and limits["cpu"], component

    def test_an_image_is_pinned_by_digest_when_one_is_given(self):
        digest = "sha256:" + "ab" * 32
        made = render(f"image.digest={digest}")
        image = pod_spec(named(made, "Deployment", "-api"))["containers"][0]["image"]
        assert image == f"registry.example.com/l1-and-l2-ai-ops@{digest}"

    @pytest.mark.parametrize(
        "change",
        [
            "containerSecurityContext.readOnlyRootFilesystem=false",
            "containerSecurityContext.allowPrivilegeEscalation=true",
            "podSecurityContext.runAsNonRoot=false",
        ],
    )
    def test_the_chart_refuses_values_that_would_undo_it(self, change):
        assert "values don't meet the specifications" in refused(change)


class TestWhoHoldsWhat:
    def test_the_chart_creates_no_secret(self, docs):
        assert [d["kind"] for d in docs if d["kind"] == "Secret"] == []
        rendered = yaml.safe_dump_all(docs)
        assert "PRIVATE KEY" not in rendered and "password" not in rendered.lower()

    def test_the_runner_has_no_database_and_no_private_key_of_the_control_plane(self, docs):
        spec = pod_spec(named(docs, "Deployment", "-runner"))
        runner = spec["containers"][0]
        env = env_of(runner)
        assert runner["args"] == ["runner", "run"]
        assert "envFrom" not in runner, "it is not given the control plane's settings"
        for name in ("DATABASE_URL", "DATABASE_MIGRATION_URL", "AGENT_WEBHOOK_SECRET"):
            assert name not in env
        assert not any(name.endswith("_KEY_REF") and "RUNNER_PRIVATE" not in name for name in env)
        secrets = {v["secret"]["secretName"] for v in spec["volumes"] if "secret" in v}
        assert secrets == {"aiops-runner-key", "aiops-api-tls"}
        assert "ports" not in runner, "it listens on nothing"

    def test_the_runner_is_capped_at_reads_until_someone_raises_it(self, docs):
        env = env_of(pod_spec(named(docs, "Deployment", "-runner"))["containers"][0])
        assert env["RUNNER_MAX_TIER"]["value"] == "T0"
        assert env["RUNNER_CONTROL_PLANE_URL"]["value"].startswith("https://")
        assert env["RUNNER_SECRET_REF_PREFIXES"]["value"] == "file:/run/secrets/connectors/"
        limits = named(docs, "ConfigMap", "-runner")["data"]
        assert limits == {"targets.json": "{}", "connections.json": "{}"}

    def test_the_runners_own_limits_reach_it_as_files(self):
        made = render(
            "runner.maxTier=T1",
            "runner.targets.autosys.force_start_jobs={NIGHTLY_*}",
            "runner.extraSecretRefPrefixes={vault:connectors/}",
        )
        env = env_of(pod_spec(named(made, "Deployment", "-runner"))["containers"][0])
        assert env["RUNNER_MAX_TIER"]["value"] == "T1"
        assert env["RUNNER_SECRET_REF_PREFIXES"]["value"] == (
            "file:/run/secrets/connectors/,vault:connectors/"
        )
        targets = named(made, "ConfigMap", "-runner")["data"]["targets.json"]
        assert yaml.safe_load(targets) == {"autosys": {"force_start_jobs": ["NIGHTLY_*"]}}

    def test_the_grant_key_is_given_to_workers_and_not_to_the_api(self, docs):
        def secrets_of(component: str) -> set:
            spec = pod_spec(named(docs, "Deployment", f"-{component}"))
            return {v["secret"]["secretName"] for v in spec["volumes"] if "secret" in v}

        assert "aiops-grant-key" in secrets_of("worker")
        assert "aiops-grant-key" not in secrets_of("api")
        assert "aiops-dispatch-key" in secrets_of("api") & secrets_of("worker")
        api = env_of(pod_spec(named(docs, "Deployment", "-api"))["containers"][0])
        worker = env_of(pod_spec(named(docs, "Deployment", "-worker"))["containers"][0])
        assert "ACTIONS_GRANT_KEY_REF" in worker and "ACTIONS_GRANT_KEY_REF" not in api

    def test_only_the_install_job_has_the_migration_role(self, docs):
        holders = [
            name
            for name, spec in workloads(docs)
            if any("DATABASE_MIGRATION_URL" in env_of(c) for c in containers(spec))
        ]
        assert holders == ["Job/rel-l1-and-l2-ai-ops-install-1"]

    def test_the_install_job_migrates_then_creates_the_tenant_then_enrols_the_runner(self, docs):
        job = named(docs, "Job", "-install-1")
        assert "pre-install" in job["metadata"]["annotations"]["helm.sh/hook"]
        spec = pod_spec(job)
        steps = [c["args"][:2] for c in containers(spec)]
        assert steps == [["migrate"], ["tenant", "create"], ["runner", "enrol"]]
        assert "--if-absent" in spec["containers"][0]["args"]

    def test_workers_run_apart_from_the_api(self, docs):
        data = named(docs, "ConfigMap", "-control-plane")["data"]
        assert data["WORKER_EMBEDDED"] == "false" and data["RUNNER_MODE"] == "remote"
        assert pod_spec(named(docs, "Deployment", "-worker"))["containers"][0]["args"] == ["worker"]


class TestNetwork:
    def test_nothing_may_connect_to_a_worker_or_a_runner(self, docs):
        policies = [d for d in docs if d["kind"] == "NetworkPolicy"]
        deny = named(docs, "NetworkPolicy", "-default-deny-ingress")
        assert deny["spec"]["policyTypes"] == ["Ingress"] and "ingress" not in deny["spec"]
        assert "app.kubernetes.io/component" not in deny["spec"]["podSelector"]["matchLabels"]
        opened = {
            p["spec"]["podSelector"]["matchLabels"].get("app.kubernetes.io/component")
            for p in policies
            if p["spec"].get("ingress")
        }
        assert opened == {"api"}, "by default only the API is reachable, and only from inside"

    def test_the_api_is_reachable_from_the_dashboard_and_the_runner_only(self, docs):
        rule = named(docs, "NetworkPolicy", "-api")["spec"]["ingress"][0]
        peers = [
            p["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for p in rule["from"]
        ]
        assert peers == ["ui", "runner"] and rule["ports"] == [{"port": 8080}]

    def test_the_runner_connects_over_tls_or_the_chart_does_not_render(self):
        assert "connects over TLS only" in refused("api.tls.secretName=")
        assert "values don't meet the specifications" in refused(
            "runner.controlPlaneUrl=http://api.example.com"
        )
        made = render("api.tls.secretName=", "runner.controlPlaneUrl=https://aiops.example.com")
        env = env_of(pod_spec(named(made, "Deployment", "-runner"))["containers"][0])
        assert env["RUNNER_CONTROL_PLANE_URL"]["value"] == "https://aiops.example.com"

    def test_an_ingress_is_tls_only(self):
        assert "ingress.tls.secretName is required" in refused(
            "ingress.enabled=true", "ingress.host=aiops.example.com"
        )
        made = render(
            "ingress.enabled=true",
            "ingress.host=aiops.example.com",
            "ingress.tls.secretName=aiops-tls",
        )
        ingress = named(made, "Ingress", "l1-and-l2-ai-ops")
        assert ingress["spec"]["tls"][0]["hosts"] == ["aiops.example.com"]


class TestOtherShapes:
    def test_without_the_values_an_install_needs_the_chart_says_what_is_missing(self):
        assert "image.repository is required" in refused(values=False)

    def test_embedded_mode_deploys_no_runner_and_mounts_no_runner_keys(self):
        made = render("controlPlane.runnerMode=embedded")
        kinds = [name for name, _ in workloads(made)]
        assert not any("runner" in name for name in kinds)
        for component in ("api", "worker"):
            spec = pod_spec(named(made, "Deployment", f"-{component}"))
            assert [v["name"] for v in spec["volumes"] if "secret" in v] in ([], ["tls"])
        steps = [c["args"][:2] for c in containers(pod_spec(named(made, "Job", "-install-1")))]
        assert steps == [["migrate"], ["tenant", "create"]]

    def test_without_a_grant_key_nothing_is_told_it_can_authorise_a_write(self):
        made = render("controlPlane.grantKeySecret=", "runner.grantPublicKey=")
        worker = env_of(pod_spec(named(made, "Deployment", "-worker"))["containers"][0])
        runner = env_of(pod_spec(named(made, "Deployment", "-runner"))["containers"][0])
        assert "ACTIONS_GRANT_KEY_REF" not in worker
        assert runner["RUNNER_GRANT_PUBLIC_KEY"]["value"] == ""

    def test_the_dashboard_makes_no_third_party_request_by_default(self, docs):
        env = env_of(pod_spec(named(docs, "Deployment", "-ui"))["containers"][0])
        assert env["UI_STYLE_SRC"]["value"] == "" and env["UI_FONT_SRC"]["value"] == ""
        assert env["API_UPSTREAM"]["value"] == "https://rel-l1-and-l2-ai-ops-api:8080"
