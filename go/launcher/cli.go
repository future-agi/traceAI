package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// Main is the importable command entry point. Unix run successfully execs and
// never returns. All other commands return the documented shell exit code.
func Main(args []string) int {
	dir, err := os.Getwd()
	if err != nil {
		dir = "."
	}
	if len(args) == 0 {
		help(os.Stdout)
		return 2
	}
	switch args[0] {
	case "run":
		return runCLI(args[1:], dir)
	case "env":
		return envCLI(args[1:], dir)
	case "doctor":
		return doctorCLI(args[1:], dir, false)
	case "validate":
		return doctorCLI(args[1:], dir, true)
	case "init":
		return initCLI(args[1:], dir)
	case "install":
		return installCLI(args[1:], dir)
	case "setup":
		return setupCLI(args[1:], dir)
	case "version":
		if len(args) > 1 {
			return usage("version takes no arguments")
		}
		fmt.Printf("traceai %s (%s)\n", Version, Flavour)
		data, _ := json.MarshalIndent(embeddedVersions(), "", "  ")
		fmt.Printf("Embedded agent versions (staging metadata; Go snapshots are unversioned):\n%s\n", data)
		return 0
	case "help", "--help", "-h":
		help(os.Stdout)
		return 0
	default:
		return usage("unknown command")
	}
}
func help(w io.Writer) {
	fmt.Fprintln(w, `traceai — fail-open, universal traceAI launcher

  traceai run [--project NAME] [--project-type observe|experiment]
              [--base-url URL] [--disable NAMES] [--only NAMES]
              [--sample-rate 0..1] [--debug] [--dry-run] [--go-inject] -- PROGRAM [ARGS...]
  traceai env [--format sh|fish|powershell|dockerfile|json] [RUNTIME...]
  traceai doctor [--json] [--offline]
  traceai init [--yes]
  traceai install python|node|java|dotnet|go|ruby|all [--from PATH]
  traceai install java|dotnet --url HTTPS_URL --sha256 DIGEST
  traceai setup go [DIR]
  traceai validate [--json] [--offline]
  traceai version

Set FI_API_KEY and FI_SECRET_KEY in the environment, never on a command line.
Run preserves the program's exit code. Other commands: 0 OK, 1 problem, 2 usage.`)
}
func diagnosticf(format string, args ...any) {
	fmt.Fprint(os.Stderr, redactText(fmt.Sprintf(format, args...), os.Environ()))
}
func usage(message string) int { diagnosticf("traceai: %s\n", message); return 2 }

type diagnosticWriter struct{ io.Writer }

func (w diagnosticWriter) Write(data []byte) (int, error) {
	_, err := io.WriteString(w.Writer, redactText(string(data), os.Environ()))
	if err != nil {
		return 0, err
	}
	return len(data), nil
}

func flagSet(name string) *flag.FlagSet {
	f := flag.NewFlagSet(name, flag.ContinueOnError)
	f.SetOutput(diagnosticWriter{os.Stderr})
	return f
}
func configurationFlags(f *flag.FlagSet, opts *Options) {
	f.StringVar(&opts.Project, "project", "", "FI_PROJECT_NAME")
	f.StringVar(&opts.ProjectType, "project-type", "", "FI_PROJECT_TYPE")
	f.StringVar(&opts.BaseURL, "base-url", "", "FI_BASE_URL")
	f.StringVar(&opts.Disable, "disable", "", "FI_DISABLED_INSTRUMENTATIONS")
	f.StringVar(&opts.Only, "only", "", "FI_INSTRUMENTATIONS")
	f.StringVar(&opts.SampleRate, "sample-rate", "", "FI_SAMPLE_RATE")
	f.BoolVar(&opts.Debug, "debug", false, "FI_AUTO_DEBUG and TRACEAI_DEBUG")
}
func printWarnings(warnings []string) {
	if len(warnings) > 0 {
		diagnosticf("traceai: %s\n", strings.Join(warnings, "; "))
	}
}
func displayed(values map[string]string, original []string) map[string]string {
	return Masked(values, environmentMap(original))
}

// Diagnostics can show launcher-owned injection-only options. Any inherited
// opaque options (and all GOFLAGS, which may come from GOENV) remain entirely
// masked. Executable env scripts use variable references instead of this view.
func displayedInjection(plan Plan, original []string) map[string]string {
	values := displayed(plan.Changes, plan.Environment)
	inherited := environmentMap(original)
	for key, value := range plan.Changes {
		if opaqueOptions(key) && key != "GOFLAGS" && inherited[key] == "" {
			values[key] = Masked(map[string]string{"value": value}, environmentMap(plan.Environment))["value"]
		}
	}
	return values
}

func runCLI(args []string, dir string) int {
	f := flagSet("run")
	var opts Options
	configurationFlags(f, &opts)
	f.BoolVar(&opts.GoInject, "go-inject", false, "experimental Go workspace/overlay injection (also TRACEAI_GO_INJECT=1)")
	dry := f.Bool("dry-run", false, "show masked injection without executing")
	if err := f.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	argv := f.Args()
	if len(argv) == 0 {
		return usage("run requires a program after --")
	}
	original := os.Environ()
	plan := Prepare(argv, original, dir, opts)
	printWarnings(plan.Warnings)
	if *dry {
		config, err := ResolveConfig(dir, plan.Environment)
		warnings := make([]string, len(plan.Warnings))
		for i, warning := range plan.Warnings {
			warnings[i] = redactText(warning, plan.Environment)
		}
		result := map[string]any{"environment": displayedInjection(plan, original), "warnings": warnings}
		if err != nil {
			result["configuration_error"] = redactText(err.Error(), plan.Environment)
		} else {
			result["config"] = displayed(config, plan.Environment)
		}
		// Do not print argv: application arguments can themselves be passwords.
		data, _ := json.MarshalIndent(result, "", "  ")
		fmt.Println(string(data))
		return 0
	}
	program, err := lookProgram(argv[0], dir, environmentMap(plan.Environment))
	if err != nil {
		if errors.Is(err, errNotExecutable) {
			diagnosticf("traceai: %s: permission denied or not executable\n", argv[0])
			return 126
		}
		diagnosticf("traceai: %s: command not found\n", argv[0])
		return 127
	}
	return execute(program, argv, plan.Environment)
}

func envCLI(args []string, dir string) int {
	f := flagSet("env")
	format := f.String("format", "sh", "sh, fish, powershell, dockerfile, json")
	if err := f.Parse(args); err != nil {
		return 2
	}
	switch *format {
	case "sh", "fish", "powershell", "dockerfile", "json":
	default:
		return usage("unsupported env format")
	}
	for _, runtime := range f.Args() {
		switch runtime {
		case "python", "node", "java", "jvm", "dotnet", "go", "ruby":
		default:
			return usage("unsupported runtime")
		}
	}
	original := os.Environ()
	plan := Prepare(nil, original, dir, Options{Runtimes: f.Args()})
	printWarnings(plan.Warnings)
	if *format == "json" {
		data, _ := json.MarshalIndent(displayedInjection(plan, original), "", "  ")
		fmt.Println(string(data))
		return 0
	}
	keys := make([]string, 0, len(plan.Changes))
	for k := range plan.Changes {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	inherited := environmentMap(original)
	for _, key := range keys {
		value, old := plan.Changes[key], inherited[key]
		if value == old {
			continue // Already injected; never print inherited options, even masked.
		}
		separator, prepend := "", true
		switch key {
		case "PYTHONPATH", "RUBYLIB", "DOTNET_STARTUP_HOOKS":
			separator = string(os.PathListSeparator)
		case "NODE_OPTIONS", "JAVA_TOOL_OPTIONS", "RUBYOPT":
			separator = " "
		}
		// A startup script may be generated before application settings exist
		// (e.g. at image build time). Always merge these variables at evaluation.
		if old != "" && separator != "" {
			switch {
			case strings.HasSuffix(value, separator+old):
				value = strings.TrimSuffix(value, separator+old)
			case strings.HasPrefix(value, old+separator):
				value, prepend = strings.TrimPrefix(value, old+separator), false
			default:
				// Only launcher-owned literals may appear in executable output.
				diagnosticf("traceai: %s cannot be emitted safely; inherited value preserved\n", key)
				continue
			}
		}
		if containsCredential(value, environmentMap(plan.Environment)) {
			diagnosticf("traceai: %s injection cannot be displayed safely; inherited value preserved\n", key)
			continue
		}
		fmt.Print(envAssignment(*format, key, value, separator, prepend))
	}
	return 0
}

// Executable env output references the inherited variable at evaluation time,
// never serializes it or executes a diagnostic redaction placeholder. Values
// inserted here are launcher-owned, privacy-checked literals, not shell code.
func envAssignment(format, key, value, separator string, prepend bool) string {
	switch format {
	case "sh":
		literal := shellQuote(value)
		if separator != "" {
			if prepend {
				literal += "\"${" + key + ":+" + separator + "$" + key + "}\""
			} else {
				literal = "\"${" + key + ":+$" + key + separator + "}\"" + literal
			}
		}
		return "export " + key + "=" + literal + "\n"
	case "fish":
		quote := func(s string) string { return "'" + strings.NewReplacer("\\", "\\\\", "'", "\\'").Replace(s) + "'" }
		plain := "set -gx " + key + " " + quote(value) + ";\n"
		if separator == "" {
			return plain
		}
		merged := quote(value+separator) + "\"$" + key + "\""
		if !prepend {
			merged = "\"$" + key + "\"" + quote(separator+value)
		}
		return "if test -n \"$" + key + "\";\nset -gx " + key + " " + merged + ";\nelse;\n" + plain + "end;\n"
	case "powershell":
		quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }
		plain := "$env:" + key + " = " + quote(value)
		if separator == "" {
			return plain + "\n"
		}
		merged := quote(value+separator) + " + $env:" + key
		if !prepend {
			merged = "$env:" + key + " + " + quote(separator+value)
		}
		return "if ($env:" + key + ") { $env:" + key + " = " + merged + " } else { " + plain + " }\n"
	case "dockerfile":
		literal := strings.TrimSuffix(strings.TrimPrefix(strconv.Quote(value), "\""), "\"")
		literal = strings.ReplaceAll(literal, "$", "\\$")
		if separator != "" {
			if prepend {
				literal += "${" + key + ":+" + separator + "}${" + key + "}"
			} else {
				literal = "${" + key + "}${" + key + ":+" + separator + "}" + literal
			}
		}
		return "ENV " + key + "=\"" + literal + "\"\n"
	}
	return ""
}
func shellQuote(value string) string { return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'" }

func initCLI(args []string, dir string) int {
	f := flagSet("init")
	yes := f.Bool("yes", false, "write non-interactive config; print install advice")
	if err := f.Parse(args); err != nil {
		return 2
	}
	if f.NArg() != 0 {
		return usage("init takes no positional arguments")
	}
	project := filepath.Base(dir)
	runtimes := DetectProject(dir)
	fmt.Printf("Detected: %s\n", strings.Join(runtimes, ", "))
	if !*yes {
		info, err := os.Stdin.Stat()
		if err != nil || info.Mode()&os.ModeCharDevice == 0 {
			return usage("non-interactive init requires --yes")
		}
		fmt.Printf("Project name [%s]: ", redactText(project, os.Environ()))
		var line string
		// Read a single line without buffering past later prompts.
		for {
			var b [1]byte
			n, e := os.Stdin.Read(b[:])
			if n > 0 && b[0] != '\n' {
				line += string(b[:])
				if len(line) > 1024 {
					return usage("project name too long")
				}
			}
			if e != nil || n > 0 && b[0] == '\n' {
				break
			}
		}
		if strings.TrimSpace(line) != "" {
			project = strings.TrimSpace(line)
		}
	}
	path := filepath.Join(dir, "futureagi.toml")
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
	if err != nil {
		diagnosticf("traceai: futureagi.toml already exists or cannot be created; not overwritten\n")
		return 1
	}
	_, err = fmt.Fprintf(file, "# Credentials belong in FI_API_KEY / FI_SECRET_KEY, not this file.\nproject_name = %s\nproject_type = \"observe\"\nsample_rate = 1.0\nwire_capture = true\n", strconv.Quote(project))
	closeErr := file.Close()
	if err != nil || closeErr != nil {
		diagnosticf("traceai: cannot write futureagi.toml\n")
		return 1
	}
	fmt.Println("Wrote futureagi.toml. Set FI_API_KEY and FI_SECRET_KEY in your environment.")
	for _, runtime := range runtimes {
		switch runtime {
		case "python":
			// Redact before shell quoting: quoting may split a credential that
			// contains an apostrophe, defeating replacement in the rendered text.
			interpreter := redactText(projectPython(dir, environmentMap(os.Environ())), os.Environ())
			fmt.Printf("Install: traceai install python\nRun: traceai run -- %s app.py\n", shellQuote(interpreter))
		case "node":
			fmt.Println("Install: traceai install node\nRun: traceai run -- npm start")
		case "java":
			fmt.Println("Install: traceai install java --from /path/traceai-javaagent.jar\nRun: traceai run -- java -jar app.jar")
		case "dotnet":
			fmt.Println("Install: traceai install dotnet --from /path/startup-hook-directory\nRun: traceai run -- dotnet MyApp.dll")
		case "go":
			fmt.Println("Setup: traceai setup go\nRun: traceai run -- go run .\nExperimental (full build): traceai run --go-inject -- go run .")
		case "ruby":
			fmt.Println("Install: traceai install ruby\nRun: traceai run -- bundle exec rails server")
		}
	}
	if len(runtimes) == 0 {
		fmt.Println("Run: traceai run -- <your normal start command>")
	}
	return 0
}

func doctorCLI(args []string, dir string, validate bool) int {
	f := flagSet("doctor")
	jsonOut := f.Bool("json", false, "JSON report")
	offline := f.Bool("offline", false, "do not contact collector")
	if err := f.Parse(args); err != nil {
		return 2
	}
	if f.NArg() != 0 {
		return usage("unexpected arguments")
	}
	config, err := ResolveConfig(dir, os.Environ())
	if err != nil {
		if *jsonOut {
			data, _ := json.Marshal(map[string]string{"error": redactText(err.Error(), os.Environ())})
			fmt.Println(string(data))
		} else {
			diagnosticf("traceai: %s\n", err)
		}
		return 1
	}
	result := CollectorResult{true, "offline; collector not checked"}
	if *offline {
		if config["FI_API_KEY"] == "" || config["FI_SECRET_KEY"] == "" {
			result = CollectorResult{false, "FI_API_KEY and FI_SECRET_KEY are not set"}
		}
	} else {
		result = CheckCollector(context.Background(), config)
	}
	report := map[string]any{"config": displayed(config, os.Environ()), "collector": result}
	if !validate {
		report["runtimes"] = RuntimeAvailability(dir, append(os.Environ(), environmentList(config)...))
		report["project"] = DetectProject(dir)
	}
	if *jsonOut {
		data, _ := json.MarshalIndent(report, "", "  ")
		fmt.Println(string(data))
	} else {
		data, _ := json.MarshalIndent(report, "", "  ")
		fmt.Println(string(data))
	}
	if result.OK {
		return 0
	}
	return 1
}
