package launcher

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"testing/fstest"
)

// This filesystem fails deterministically if ingestion attempts to read a
// special entry. A real FIFO would block in Open before a post-open stat.
type specialFileFS struct {
	fs.FS
	reads int
}

func (f *specialFileFS) ReadFile(name string) ([]byte, error) {
	f.reads++
	return nil, fmt.Errorf("unexpected payload read")
}

func TestAgentIngestionRejectsSpecialFilesBeforeRead(t *testing.T) {
	for _, mode := range []fs.FileMode{fs.ModeNamedPipe, fs.ModeSocket, fs.ModeDevice, fs.ModeCharDevice | fs.ModeDevice, fs.ModeSymlink, fs.ModeIrregular} {
		t.Run(mode.String(), func(t *testing.T) {
			source := &specialFileFS{FS: fstest.MapFS{"special": &fstest.MapFile{Mode: mode | 0600, Data: []byte("not an agent")}}}
			if _, err := filesFromFS(source, "."); err == nil || source.reads != 0 {
				t.Fatal("special payload was accepted or read", source.reads)
			}
		})
	}
}

func TestJVMBackslashesAreLiteralAndIdempotent(t *testing.T) {
	for _, name := range []string{`agent\path.jar`, `agent with space\path.jar`, `agent\\double.jar`} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			jar := filepath.Join(dir, name)
			put(t, jar, "agent")
			original := append(baseEnv(t), "TRACEAI_JAVA_AGENT="+jar, `JAVA_TOOL_OPTIONS=-Dapp.path=C:\literal\path\`)
			first := Prepare(nil, original, dir, Options{Runtimes: []string{"java"}})
			second := Prepare(nil, first.Environment, dir, Options{Runtimes: []string{"java"}})
			if !reflect.DeepEqual(first.Environment, second.Environment) || strings.Count(environmentMap(second.Environment)["JAVA_TOOL_OPTIONS"], "-javaagent:") != 1 {
				t.Fatal("nested JVM injection duplicated the agent")
			}
			words, err := optionWords(environmentMap(first.Environment)["JAVA_TOOL_OPTIONS"])
			if err != nil || len(words) != 2 || words[0] != "-javaagent:"+filepath.ToSlash(jar) || words[1] != `-Dapp.path=C:\literal\path\` {
				t.Fatal("JVM literal backslash parsing changed")
			}
		})
	}
}

func TestGoOutputdirFlagIsNotAPackage(t *testing.T) {
	for _, args := range [][]string{
		{"-outputdir", "results"}, {"-outputdir=results"},
		{"-count=1", ".", "-outputdir", "results", "-args", "application-argument"},
	} {
		targets, err := goTargets(args, "test")
		if err != nil || !reflect.DeepEqual(targets, []string{"."}) {
			t.Fatal("test output directory became a package", targets, err)
		}
	}
}

func TestExecutableEnvOutputReferencesInheritedOptions(t *testing.T) {
	dir := isolatedCLI(t)
	jar := filepath.Join(dir, "agent.jar")
	put(t, jar, "agent")
	t.Setenv("TRACEAI_JAVA_AGENT", jar)
	secret := "VERIFY_OPAQUE_OPTION_829130"
	t.Setenv("JAVA_TOOL_OPTIONS", "-Dpassword="+secret)
	t.Setenv("NODE_OPTIONS", "--title="+secret)
	t.Setenv("RUBYOPT", "-I"+secret)
	t.Setenv("TRACEAI_LAUNCHER", "previous-launcher-version")
	for _, format := range []string{"sh", "fish", "powershell", "dockerfile"} {
		t.Run(format, func(t *testing.T) {
			code, output, stderr := captureCLI(t, func() int { return Main([]string{"env", "--format", format, "java", "node", "ruby"}) })
			if code != 0 || stderr != "" || strings.Contains(output, "***") || strings.Contains(output, secret) || strings.Contains(output, "previous-launcher-version") || !strings.Contains(output, Version) {
				t.Fatal("executable environment leaked options or emitted a diagnostic placeholder")
			}
			for _, key := range []string{"JAVA_TOOL_OPTIONS", "NODE_OPTIONS", "RUBYOPT"} {
				reference := "$" + key
				if format == "powershell" {
					reference = "$env:" + key
				}
				if format == "sh" || format == "dockerfile" {
					reference = "${" + key
				}
				if !strings.Contains(output, reference) {
					t.Fatal("missing inherited-variable reference", key)
				}
			}
		})
	}
	// Never turn a masked injection path into executable output either.
	t.Setenv("FI_API_KEY", "agent.jar")
	_, output, stderr := captureCLI(t, func() int { return Main([]string{"env", "java"}) })
	if strings.Contains(output, "JAVA_TOOL_OPTIONS") || strings.Contains(output, "agent.jar") || !strings.Contains(stderr, "Java agent path contains a known credential; injection skipped") {
		t.Fatal("redacted path was emitted as executable code")
	}
}

func TestEnvAssignmentFormats(t *testing.T) {
	for _, tc := range []struct{ format, want string }{
		{"sh", "export NODE_OPTIONS='new'\"${NODE_OPTIONS:+ $NODE_OPTIONS}\"\n"},
		{"fish", "if test -n \"$NODE_OPTIONS\";\nset -gx NODE_OPTIONS 'new '\"$NODE_OPTIONS\";\nelse;\nset -gx NODE_OPTIONS 'new';\nend;\n"},
		{"powershell", "if ($env:NODE_OPTIONS) { $env:NODE_OPTIONS = 'new ' + $env:NODE_OPTIONS } else { $env:NODE_OPTIONS = 'new' }\n"},
		{"dockerfile", "ENV NODE_OPTIONS=\"new${NODE_OPTIONS:+ }${NODE_OPTIONS}\"\n"},
	} {
		if got := envAssignment(tc.format, "NODE_OPTIONS", "new", " ", true); got != tc.want {
			t.Fatal("invalid environment expression", tc.format)
		}
	}
	// Diagnostics remain masked and do not mutate the inherited environment.
	before := os.Getenv("NODE_OPTIONS")
	_ = envAssignment("sh", "NODE_OPTIONS", "new", " ", true)
	if os.Getenv("NODE_OPTIONS") != before {
		t.Fatal("emission mutated the process")
	}
}
