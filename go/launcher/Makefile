SHELL := /bin/sh
ROOT := $(abspath ../..)
GOMAXPROCS := 4
GOTOOLCHAIN := auto
export GOMAXPROCS GOTOOLCHAIN

.PHONY: slim full stage test e2e clean

# Only copies prebuilt SDK artifacts. Never builds Java/.NET/Node/Ruby.
slim:
	@mkdir -p bin .scratch
	TMPDIR="$(CURDIR)/.scratch" sh scripts/host-step.sh light go build -o bin/traceai ./cmd/traceai

stage:
	python3 -I scripts/stage.py "$(ROOT)" "$(CURDIR)/embedded"

full: stage
	@mkdir -p bin .scratch
	TMPDIR="$(CURDIR)/.scratch" sh scripts/host-step.sh light go build -tags embedagents -o bin/traceai-full ./cmd/traceai

test:
	@mkdir -p .scratch
	TMPDIR="$(CURDIR)/.scratch" sh scripts/host-step.sh light go vet ./...
	TMPDIR="$(CURDIR)/.scratch" GOOS=windows sh scripts/host-step.sh light go vet ./...
	TMPDIR="$(CURDIR)/.scratch" sh scripts/host-step.sh heavy go test -count=1 ./...

e2e: stage
	@mkdir -p .scratch
	TMPDIR="$(CURDIR)/.scratch" TRACEAI_E2E=1 sh scripts/host-step.sh heavy go test -count=1 -run '^TestE2E$$' -v ./...

clean:
	rm -rf bin .scratch embedded/java embedded/dotnet embedded/go embedded/node embedded/ruby embedded/versions.json
