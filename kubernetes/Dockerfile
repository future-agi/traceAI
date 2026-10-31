# Cloud build only, from the repository root. First stage current agents with
# go/launcher/scripts/stage.py; see README. No downloads at Pod startup.
FROM golang:1.26.0 AS build
ENV GOTOOLCHAIN=auto GOMAXPROCS=4 CGO_ENABLED=0
WORKDIR /src/kubernetes
COPY kubernetes/go.mod kubernetes/go.sum ./
RUN go mod download
COPY kubernetes/cmd ./cmd
COPY kubernetes/internal ./internal
RUN go build -p 2 -trimpath -o /out/traceai-webhook ./cmd/traceai-webhook

WORKDIR /src/launcher
COPY go/launcher/go.mod go/launcher/*.go ./
COPY go/launcher/cmd ./cmd
COPY go/launcher/shims ./shims
# Never embed the unused Go source tree in the deployment image.
COPY go/launcher/embedded/java ./embedded/java
COPY go/launcher/embedded/dotnet ./embedded/dotnet
COPY go/launcher/embedded/node ./embedded/node
COPY go/launcher/embedded/ruby ./embedded/ruby
COPY go/launcher/embedded/versions.json ./embedded/versions.json
RUN go build -p 2 -tags embedagents -trimpath -o /out/traceai ./cmd/traceai
# This validates that the full launcher supplies ALL runtime payloads and complete
# .NET dependencies, generates its guarded shims, and writes a SHA-256 inventory.
RUN /out/traceai-webhook stage-payload --launcher=/out/traceai --root=/opt/traceai

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/traceai-webhook /traceai-webhook
COPY --from=build --chown=65532:65532 /opt/traceai /payload
USER 65532:65532
ENTRYPOINT ["/traceai-webhook"]
