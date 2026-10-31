{{- define "traceai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "traceai.fullname" -}}
{{- $name := default (printf "%s-%s" .Release.Name (include "traceai.name" .)) .Values.fullnameOverride -}}
{{- /* Service names are DNS-1035 labels: they must start with a letter. */ -}}
{{- if regexMatch "^[^a-z]" $name -}}
{{- $name = printf "traceai-%s" $name -}}
{{- end -}}
{{- $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "traceai.clustername" -}}
{{- /* Hash the namespace and the untruncated name so long overrides cannot collide. */ -}}
{{- $full := default (printf "%s-%s" .Release.Name (include "traceai.name" .)) .Values.fullnameOverride -}}
{{- printf "%s-%s" (include "traceai.fullname" . | trunc 54 | trimSuffix "-") (printf "%s/%s" .Release.Namespace $full | sha256sum | trunc 8) -}}
{{- end -}}
{{- define "traceai.labels" -}}
app.kubernetes.io/name: {{ include "traceai.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}
{{- define "traceai.selectorLabels" -}}
app.kubernetes.io/name: {{ include "traceai.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}
{{- define "traceai.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}
{{- define "traceai.tlsSecret" -}}
{{- if .Values.certManager.enabled -}}
{{- printf "%s-tls" (include "traceai.fullname" . | trunc 59 | trimSuffix "-") -}}
{{- else -}}
{{- required "tls.existingSecret is required without cert-manager" .Values.tls.existingSecret -}}
{{- end -}}
{{- end -}}
