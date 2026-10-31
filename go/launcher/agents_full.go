//go:build embedagents

package launcher

import (
	"embed"
	"encoding/json"
	"strings"
)

const Flavour = "full"

//go:embed all:embedded
var payloads embed.FS

func embeddedAgent(runtime, cache string) (string, error) {
	files, err := filesFromFS(payloads, "embedded/"+runtime)
	if err != nil {
		return "", err
	}
	if runtime == "go" {
		restored := make(map[string][]byte, len(files))
		for name, data := range files {
			restored[strings.TrimSuffix(name, ".source")] = data
		}
		files = restored
	}
	return installFiles(cache, runtime, files)
}

func embeddedVersions() map[string]string {
	versions := map[string]string{}
	if data, err := payloads.ReadFile("embedded/versions.json"); err == nil {
		_ = json.Unmarshal(data, &versions)
	}
	return versions
}
