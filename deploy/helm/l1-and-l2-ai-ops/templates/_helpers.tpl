{{- define "aiops.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aiops.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "aiops.labels" -}}
app.kubernetes.io/name: {{ include "aiops.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "aiops.selector" -}}
app.kubernetes.io/name: {{ include "aiops.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* An image reference: by digest when one is given, by tag otherwise. */}}
{{- define "aiops.imageRef" -}}
{{- $image := .image -}}
{{- $repository := required (printf "%s.repository is required" .what) $image.repository -}}
{{- if $image.digest -}}
{{- printf "%s@%s" $repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (default .appVersion $image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "aiops.image" -}}
{{- include "aiops.imageRef" (dict "image" .Values.image "what" "image" "appVersion" .Chart.AppVersion) -}}
{{- end -}}

{{- define "aiops.uiImage" -}}
{{- include "aiops.imageRef" (dict "image" .Values.ui.image "what" "ui.image" "appVersion" .Chart.AppVersion) -}}
{{- end -}}

{{- define "aiops.remote" -}}
{{- if eq .Values.controlPlane.runnerMode "remote" -}}true{{- end -}}
{{- end -}}

{{- define "aiops.apiScheme" -}}
{{- if .Values.api.tls.secretName -}}https{{- else -}}http{{- end -}}
{{- end -}}

{{/* Where the runner reaches the control plane. Always https. */}}
{{- define "aiops.controlPlaneUrl" -}}
{{- if .Values.runner.controlPlaneUrl -}}
{{- if not (hasPrefix "https://" .Values.runner.controlPlaneUrl) -}}
{{- fail "runner.controlPlaneUrl must start with https://" -}}
{{- end -}}
{{- .Values.runner.controlPlaneUrl -}}
{{- else if .Values.api.tls.secretName -}}
{{- printf "https://%s-api.%s.svc:%v" (include "aiops.fullname" .) .Release.Namespace .Values.api.port -}}
{{- else -}}
{{- fail "The runner connects over TLS only: set api.tls.secretName, or set runner.controlPlaneUrl to an https URL" -}}
{{- end -}}
{{- end -}}

{{/* Scratch space, since the root filesystem is read only. */}}
{{- define "aiops.tmpVolume" -}}
- name: tmp
  emptyDir:
    sizeLimit: 256Mi
{{- end -}}

{{- define "aiops.scheduling" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Environment the API, the workers and the install job share: database and secrets. */}}
{{- define "aiops.controlPlaneEnv" -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ required "database.existingSecret is required" .Values.database.existingSecret }}
      key: {{ .Values.database.urlKey }}
- name: DATABASE_APP_ROLE
  value: {{ .Values.database.appRole | quote }}
- name: TENANT_SLUG
  value: {{ .Values.tenant.slug | quote }}
- name: AGENT_WEBHOOK_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "secrets.existingSecret is required" .Values.secrets.existingSecret }}
      key: {{ .Values.secrets.keys.webhookSecret }}
{{- if .Values.secrets.present.auditCheckpointKey }}
- name: AUDIT_CHECKPOINT_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret }}
      key: {{ .Values.secrets.keys.auditCheckpointKey }}
{{- end }}
{{- if .Values.secrets.present.oidcClientSecret }}
- name: OIDC_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret }}
      key: {{ .Values.secrets.keys.oidcClientSecret }}
{{- end }}
{{- if .Values.secrets.present.breakGlassPasswordHash }}
- name: BREAK_GLASS_PASSWORD_HASH
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret }}
      key: {{ .Values.secrets.keys.breakGlassPasswordHash }}
{{- end }}
{{- range .Values.secretEnv }}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ .secretName }}
      key: {{ .key }}
{{- end }}
{{- end -}}
