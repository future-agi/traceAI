// Package testhost starts integration-test child processes. It is imported by
// tests only, never by the agent.
package testhost

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
)

// GateEnv optionally names a wrapper script that limits how many heavy child
// builds run at once on a shared machine. When it is unset, as in CI, children
// run directly.
const GateEnv = "TRACEAI_TEST_HOST_GATE"

// Command wraps a standalone child in the optional gate. Inside an already
// gated suite, children inherit its slot: acquiring a second non-reentrant slot
// can deadlock two concurrent suites, each waiting for its own child.
func Command(name string, args ...string) *exec.Cmd {
	if gate := os.Getenv(GateEnv); gate != "" {
		if _, err := os.Stat(gate); err == nil && !guardedAncestor(gate) {
			return exec.Command("bash", append([]string{gate, name}, args...)...)
		}
	}
	return exec.Command(name, args...)
}

func parent(line, gate string) (int, bool) {
	fields := strings.Fields(line)
	if len(fields) < 2 {
		return 0, false
	}
	pid, err := strconv.Atoi(fields[0])
	if err != nil {
		return 0, false
	}
	guarded := len(fields) >= 3 && filepath.Base(fields[1]) == "bash" && fields[2] == gate
	return pid, guarded
}
func guardedAncestor(gate string) bool {
	pid := os.Getppid()
	for depth := 0; pid > 1 && depth < 32; depth++ {
		// Read-only process metadata, not another application/runtime workload.
		out, err := exec.Command("ps", "-p", strconv.Itoa(pid), "-o", "ppid=", "-o", "command=").Output()
		if err != nil {
			return false
		}
		next, guarded := parent(string(out), gate)
		if guarded {
			return true
		}
		if next == pid {
			return false
		}
		pid = next
	}
	return false
}
