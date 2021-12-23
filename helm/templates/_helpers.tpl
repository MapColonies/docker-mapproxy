{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "map-proxy.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "map-proxy.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "map-proxy.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Returns the tracing url from global if exists or from the chart's values
*/}}
{{- define "map-proxy.tracingUrl" -}}
{{- if .Values.global.tracing.url -}}
    {{- .Values.global.tracing.url -}}
{{- else if .Values.tracing.url -}}
    {{- .Values.tracing.url -}}
{{- end -}}
{{- end -}}

{{/*
Returns the tracing enabled state from global if exists or from the chart's values
*/}}
{{- define "map-proxy.tracingEnabled" -}}
{{- if .Values.global.tracing.enabled -}}
    {{- .Values.global.tracing.enabled -}}
{{- else if .Values.tracing.enabled -}}
    {{- .Values.tracing.enabled -}}
{{- end -}}
{{- end -}}
