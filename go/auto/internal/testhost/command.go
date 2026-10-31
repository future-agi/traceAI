// Package testhost keeps integration-test child workloads inside the shared
// laptop budget. It is imported by tests only, never by the agent.
package testhost

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
)

const gate = "/Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh"

// Command wraps a standalone child in the host gate. Inside an already gated
// suite, children inherit its slot: acquiring a second non-reentrant slot can
// deadlock two concurrent suites, each waiting for its own child. This does not
// bypass the host budget; the ancestor wrapper holds it until all children exit.
func Command(name string, args ...string) *exec.Cmd {
	if _, err := os.Stat(gate); err == nil && !guardedAncestor() {
		return exec.Command("bash", append([]string{gate, name}, args...)...)
	}
	return exec.Command(name, args...)
}

func parent(line string) (int, bool) {
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
func guardedAncestor() bool {
	pid := os.Getppid()
	for depth := 0; pid > 1 && depth < 32; depth++ {
		// Read-only process metadata, not another application/runtime workload.
		out, err := exec.Command("ps", "-p", strconv.Itoa(pid), "-o", "ppid=", "-o", "command=").Output()
		if err != nil {
			return false
		}
		next, guarded := parent(string(out))
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
