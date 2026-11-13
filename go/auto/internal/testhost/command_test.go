package testhost

import "testing"

const gate = "/srv/ci/gate.sh"

func TestParentGateDetection(t *testing.T) {
	for _, tc := range []struct {
		name, line string
		pid        int
		guarded    bool
	}{
		{"gate", "123 bash " + gate + " go test ./...", 123, true},
		{"absolute-shell", "123 /bin/bash " + gate + " go build ./...", 123, true},
		{"quoted-command-is-not-lease", "123 /bin/zsh -c 'bash " + gate + " go test ./...'", 123, false},
		{"ordinary-parent", "123 go test ./...", 123, false},
		{"unrelated-script", "123 bash /some/other.sh", 123, false},
		{"empty", "", 0, false},
		{"invalid-pid", "invalid bash " + gate, 0, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pid, guarded := parent(tc.line, gate)
			if pid != tc.pid || guarded != tc.guarded {
				t.Fatalf("pid=%d guarded=%v", pid, guarded)
			}
		})
	}
}

func TestCommandRunsDirectlyWithoutGate(t *testing.T) {
	t.Setenv(GateEnv, "")
	cmd := Command("go", "version")
	if len(cmd.Args) != 2 || cmd.Args[0] != "go" || cmd.Args[1] != "version" {
		t.Fatalf("ungated command was wrapped: %q", cmd.Args)
	}
}
