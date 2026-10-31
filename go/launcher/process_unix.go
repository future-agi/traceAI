//go:build !windows

package launcher

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

var errNotExecutable = errors.New("not executable")

func lookProgram(program, dir string, env map[string]string) (string, error) {
	candidate := func(path string) (string, error) {
		if !filepath.IsAbs(path) {
			path = filepath.Join(dir, path)
		}
		info, err := os.Stat(path)
		if err != nil {
			if os.IsPermission(err) {
				return "", errNotExecutable
			}
			return "", err
		}
		// An unrelated user's execute bit does not grant this caller access.
		// access(2) also accounts for supplementary groups and filesystem ACLs.
		if !info.Mode().IsRegular() || syscall.Access(path, 1) != nil {
			return "", errNotExecutable
		}
		return path, nil
	}
	if strings.ContainsRune(program, filepath.Separator) {
		return candidate(program)
	}
	var denied bool
	paths := filepath.SplitList(env["PATH"])
	// An explicitly empty PATH is one empty search component (the working
	// directory), not an empty list. Preserve unset-PATH behavior separately.
	if value, exists := env["PATH"]; exists && value == "" {
		paths = []string{""}
	}
	for _, path := range paths {
		found, err := candidate(filepath.Join(path, program))
		if err == nil {
			return found, nil
		}
		if errors.Is(err, errNotExecutable) {
			denied = true
		}
	}
	if denied {
		return "", errNotExecutable
	}
	return "", os.ErrNotExist
}
func execute(program string, argv, env []string) int {
	err := syscall.Exec(program, argv, env)
	code := 126
	if errors.Is(err, syscall.ENOENT) {
		code = 127
	}
	diagnosticf("traceai: cannot execute %s: %v\n", argv[0], err)
	return code
}
