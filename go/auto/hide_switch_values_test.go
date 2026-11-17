package auto

import (
	"os"
	"path/filepath"
	"testing"
)

// A hide switch someone set must never silently do nothing. For every FI_HIDE_*
// variable and its futureagi.toml setting: true, 1, yes and on hide; false, 0,
// no and off show; a blank value is not set; any other value hides. The
// one-time warning for such a value is covered in a real process, by
// TestUnrecognisedHideSwitchE2E. Other switches keep their own parsing.
var hideSwitchNames = []string{"HIDE_INPUTS", "HIDE_OUTPUTS", "HIDE_INPUT_MESSAGES", "HIDE_OUTPUT_MESSAGES", "HIDE_INPUT_IMAGES", "HIDE_INPUT_TEXT", "HIDE_OUTPUT_TEXT", "HIDE_LLM_INVOCATION_PARAMETERS"}

// hideSetting is the TraceConfig field that the FI_<name> variable controls.
func hideSetting(t *testing.T, c TraceConfig, name string) bool {
	t.Helper()
	switch name {
	case "HIDE_INPUTS":
		return c.HideInputs
	case "HIDE_OUTPUTS":
		return c.HideOutputs
	case "HIDE_INPUT_MESSAGES":
		return c.HideInputMessages
	case "HIDE_OUTPUT_MESSAGES":
		return c.HideOutputMessages
	case "HIDE_INPUT_IMAGES":
		return c.HideInputImages
	case "HIDE_INPUT_TEXT":
		return c.HideInputText
	case "HIDE_OUTPUT_TEXT":
		return c.HideOutputText
	case "HIDE_LLM_INVOCATION_PARAMETERS":
		return c.HideLLMInvocationParameters
	}
	t.Fatalf("unknown hide switch %s", name)
	return false
}

// hideEnvironment leaves only the given variables set among the switches these
// tests read, and makes file the config file ("" for none).
func hideEnvironment(t *testing.T, file string, env map[string]string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "futureagi.toml")
	if file != "" {
		if err := os.WriteFile(path, []byte(file+"\n"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("FI_CONFIG_FILE", path)
	names := []string{"OTEL_SDK_DISABLED", "FI_WIRE_CAPTURE", "FI_AUTO_DEBUG"}
	for _, name := range hideSwitchNames {
		names = append(names, "FI_"+name)
	}
	for _, name := range names {
		// Setenv restores the variable after the test; Unsetenv clears it now.
		t.Setenv(name, "")
		if err := os.Unsetenv(name); err != nil {
			t.Fatal(err)
		}
	}
	for name, value := range env {
		t.Setenv(name, value)
	}
}

func hideConfig(t *testing.T, file string, env map[string]string) Config {
	t.Helper()
	hideEnvironment(t, file, env)
	c, err := readConfig()
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestHideSwitchTrueValuesHide(t *testing.T) {
	for _, value := range []string{"true", "on", "yes", "1", " TRUE ", "On"} {
		t.Run(value, func(t *testing.T) {
			c := hideConfig(t, "", map[string]string{"FI_HIDE_INPUTS": value})
			if !c.TraceConfig.HideInputs {
				t.Fatalf("FI_HIDE_INPUTS=%q must hide inputs", value)
			}
		})
	}
}

func TestHideSwitchFalseValuesShow(t *testing.T) {
	for _, value := range []string{"false", "off", "no", "0", " OFF "} {
		t.Run(value, func(t *testing.T) {
			c := hideConfig(t, "", map[string]string{"FI_HIDE_INPUTS": value})
			if c.TraceConfig.HideInputs {
				t.Fatalf("FI_HIDE_INPUTS=%q must show inputs", value)
			}
		})
	}
}

func TestBlankHideSwitchIsNotSet(t *testing.T) {
	for _, value := range []string{"", "   "} {
		if c := hideConfig(t, "", map[string]string{"FI_HIDE_INPUTS": value}); c.TraceConfig.HideInputs {
			t.Fatalf("a blank FI_HIDE_INPUTS (%q) must count as not set", value)
		}
		// A blank environment value does not switch off what the config file hides.
		if c := hideConfig(t, "hide_inputs = true", map[string]string{"FI_HIDE_INPUTS": value}); !c.TraceConfig.HideInputs {
			t.Fatalf("a blank FI_HIDE_INPUTS (%q) must not replace hide_inputs = true in the config file", value)
		}
	}
}

func TestEveryHideSwitchReadsOnAndOff(t *testing.T) {
	for _, name := range hideSwitchNames {
		t.Run(name, func(t *testing.T) {
			on := hideConfig(t, "", map[string]string{"FI_" + name: "on"})
			if !hideSetting(t, on.TraceConfig, name) {
				t.Fatalf("FI_%s=on must hide", name)
			}
			off := hideConfig(t, "", map[string]string{"FI_" + name: "off"})
			if hideSetting(t, off.TraceConfig, name) {
				t.Fatalf("FI_%s=off must show", name)
			}
		})
	}
}

func TestUnrecognisedHideSwitchValueHides(t *testing.T) {
	for _, name := range hideSwitchNames {
		for _, value := range []string{"maybe", "enabled"} {
			t.Run(name+"/"+value, func(t *testing.T) {
				c := hideConfig(t, "", map[string]string{"FI_" + name: value})
				if !hideSetting(t, c.TraceConfig, name) {
					t.Fatalf("FI_%s=%s is not a recognised value, so it must hide", name, value)
				}
			})
		}
	}
}

func TestHideSwitchConfigFileValues(t *testing.T) {
	for _, tc := range []struct {
		line   string
		hidden bool
	}{
		{`hide_inputs = true`, true},
		{`hide_inputs = "on"`, true},
		{`hide_inputs = on`, true},
		{`hide_inputs = ' Yes '`, true},
		{`hide_inputs = 1`, true},
		{`hide_inputs = false`, false},
		{`hide_inputs = "off"`, false},
		{`hide_inputs = off`, false},
		{`hide_inputs = 0`, false},
		{`hide_inputs = ""`, false},
		{`hide_inputs = "maybe"`, true},
		{`hide_inputs = maybe`, true},
	} {
		t.Run(tc.line, func(t *testing.T) {
			c := hideConfig(t, tc.line, nil)
			if c.TraceConfig.HideInputs != tc.hidden {
				t.Fatalf("%s: hidden=%v, want %v", tc.line, c.TraceConfig.HideInputs, tc.hidden)
			}
		})
	}
}

func TestOtherSwitchesKeepTheirParsing(t *testing.T) {
	for _, tc := range []struct {
		value string
		on    bool
	}{
		{"true", true},
		{" TRUE ", true},
		{"on", false},
		{"yes", false},
		{"1", false},
		{"maybe", false},
	} {
		t.Run(tc.value, func(t *testing.T) {
			c := hideConfig(t, "", map[string]string{"FI_WIRE_CAPTURE": tc.value, "FI_AUTO_DEBUG": tc.value, "OTEL_SDK_DISABLED": tc.value})
			if c.WireCapture != tc.on || c.Debug != tc.on || c.Disabled != tc.on {
				t.Fatalf("%q: wire capture %v, debug %v, disabled %v, want %v for each", tc.value, c.WireCapture, c.Debug, c.Disabled, tc.on)
			}
			for _, name := range hideSwitchNames {
				if hideSetting(t, c.TraceConfig, name) {
					t.Fatalf("%q in another switch turned on FI_%s", tc.value, name)
				}
			}
		})
	}
}

// A TraceConfig given in code replaces the hide switches, whatever they say.
func TestTraceConfigOptionReplacesTheHideSwitches(t *testing.T) {
	hideEnvironment(t, `hide_input_text = "maybe"`, map[string]string{"FI_HIDE_INPUTS": "maybe"})
	s := settings{}
	WithTraceConfig(TraceConfig{HideOutputs: true, Base64ImageMaxLength: 32000})(&s)
	c, err := readConfigWithOverrides(s.ignoreInherited, s.config...)
	if err != nil {
		t.Fatal(err)
	}
	if c.TraceConfig != (TraceConfig{HideOutputs: true, Base64ImageMaxLength: 32000}) {
		t.Fatalf("the TraceConfig given in code was not used as given: %+v", c.TraceConfig)
	}
}
