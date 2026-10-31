//go:build windows

package launcher

import (
	"errors"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
)

var errNotExecutable = errors.New("not executable")
var kernel32 = syscall.NewLazyDLL("kernel32.dll")
var generateConsoleCtrlEvent = kernel32.NewProc("GenerateConsoleCtrlEvent")

func lookProgram(program, dir string, env map[string]string) (string, error) {
	extensions := strings.Split(env["PATHEXT"], ";")
	if len(extensions) == 0 || extensions[0] == "" {
		extensions = []string{".COM", ".EXE", ".BAT", ".CMD"}
	}
	names := []string{program}
	if filepath.Ext(program) == "" {
		names = nil
		for _, ext := range extensions {
			names = append(names, program+strings.ToLower(ext))
		}
	}
	paths := filepath.SplitList(env["PATH"])
	if filepath.IsAbs(program) || strings.ContainsAny(program, "/\\") {
		paths = []string{""}
	}
	for _, path := range paths {
		for _, name := range names {
			candidate := filepath.Join(path, name)
			if !filepath.IsAbs(candidate) {
				candidate = filepath.Join(dir, candidate)
			}
			info, err := os.Stat(candidate)
			if err != nil {
				continue
			}
			if !info.Mode().IsRegular() {
				return "", errNotExecutable
			}
			ext := strings.ToLower(filepath.Ext(candidate))
			if ext != ".exe" && ext != ".com" && ext != ".bat" && ext != ".cmd" {
				return "", errNotExecutable
			}
			return candidate, nil
		}
	}
	return "", os.ErrNotExist
}
func execute(program string, argv, env []string) int {
	cmd := exec.Command(program, argv[1:]...)
	cmd.Args = append([]string(nil), argv...)
	cmd.Env = env
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	// Keep the child in the same console group. Windows broadcasts Ctrl+C and
	// Ctrl+Break to BOTH processes. Signal notification prevents the launcher from
	// exiting before the child; no second event is generated (which would double
	// deliver Ctrl+C). A detached child would require a new process group +
	// GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid), which changes Ctrl+C semantics.
	signals := make(chan os.Signal, 4)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	if err := cmd.Start(); err != nil {
		diagnosticf("traceai: cannot execute %s\n", argv[0])
		return 126
	}
	done := make(chan struct{})
	defer close(done)
	go func() {
		for {
			select {
			case s := <-signals:
				// CTRL_CLOSE/LOGOFF/SHUTDOWN arrive as SIGTERM; forward a bounded console
				// break rather than silently swallowing an OS shutdown notification.
				if s == syscall.SIGTERM {
					_, _, _ = generateConsoleCtrlEvent.Call(1, uintptr(cmd.Process.Pid))
				}
			case <-done:
				return
			}
		}
	}()
	err := cmd.Wait()
	if err == nil {
		return 0
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	return 1
}
