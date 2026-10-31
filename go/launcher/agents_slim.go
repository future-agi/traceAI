//go:build !embedagents

package launcher

const Flavour = "slim"

func embeddedAgent(runtime, cache string) (string, error) { return "", nil }

func embeddedVersions() map[string]string { return map[string]string{} }
