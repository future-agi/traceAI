package injector

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	jsonpatch "github.com/evanphx/json-patch/v5"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func testConfig() Config {
	return Config{AgentImage: "registry.invalid/traceai@sha256:" + strings.Repeat("a", 64), SecretName: "traceai-credentials", Settings: map[string]string{"FI_PROJECT_NAME": "default", "FI_HIDE_INPUTS": "true", "FI_SAMPLE_RATE": "0.25"}, ExcludedNamespaces: []string{"kube-system"}}
}
func testPod(language string) corev1.Pod {
	return corev1.Pod{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Pod"}, ObjectMeta: metav1.ObjectMeta{Name: "test", Namespace: "apps", Annotations: map[string]string{Inject: "true", Language: language}}, Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "app", Image: "application:unchanged", Command: []string{"/app/start"}, Args: []string{"--safe", "secret-argument"}}}}}
}
func rawPod(t *testing.T, p corev1.Pod) []byte {
	t.Helper()
	data, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return data
}
func apply(t *testing.T, raw []byte, patches []Patch) (corev1.Pod, []byte) {
	t.Helper()
	var pod corev1.Pod
	if len(patches) == 0 {
		if err := json.Unmarshal(raw, &pod); err != nil {
			t.Fatal(err)
		}
		return pod, raw
	}
	data, err := json.Marshal(patches)
	if err != nil {
		t.Fatal(err)
	}
	patch, err := jsonpatch.DecodePatch(data)
	if err != nil {
		t.Fatal(err)
	}
	out, err := patch.Apply(raw)
	if err != nil {
		t.Fatalf("invalid JSON patch: %v\n%s", err, data)
	}
	if err := json.Unmarshal(out, &pod); err != nil {
		t.Fatal(err)
	}
	return pod, out
}
func mutate(t *testing.T, p corev1.Pod, cfg Config) (corev1.Pod, []Patch) {
	t.Helper()
	raw := rawPod(t, p)
	patches, err := Mutation(raw, false, cfg)
	if err != nil {
		t.Fatal(err)
	}
	out, _ := apply(t, raw, patches)
	return out, patches
}
func value(t *testing.T, c corev1.Container, key string) corev1.EnvVar {
	t.Helper()
	i := envIndex(c.Env, key)
	if i < 0 {
		t.Fatalf("missing %s", key)
	}
	return c.Env[i]
}

func TestRuntimeJSONPatches(t *testing.T) {
	cases := []struct{ language, key, old, prefix, separator string }{
		{"node", "NODE_OPTIONS", `--max-old-space-size=512 --require "/app/my preload.cjs"`, "--require /opt/traceai/node/preload.cjs", " "},
		{"java", "JAVA_TOOL_OPTIONS", `-Xmx256m -Dconfig="application value" -javaagent:/other/agent.jar`, "-javaagent:/opt/traceai/java/traceai-javaagent.jar", " "},
		{"dotnet", "DOTNET_STARTUP_HOOKS", "/app/Existing.dll", "/opt/traceai/dotnet/TraceAI.AutoInstrumentation.dll", ":"},
		{"python", "PYTHONPATH", "/app/lib:", "/opt/traceai/python", ":"},
		{"ruby", "RUBYOPT", "-w -r/app/boot.rb", "-r/opt/traceai/ruby/preload.rb", " "},
	}
	for _, tc := range cases {
		for _, existing := range []bool{false, true} {
			name := "missing"
			if existing {
				name = "merged"
			}
			t.Run(tc.language+"/"+name, func(t *testing.T) {
				p := testPod(tc.language)
				if existing {
					p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: tc.key, Value: tc.old}, {Name: "APPLICATION", Value: "untouched"}}
				}
				before := p.Spec.Containers[0].DeepCopy()
				out, patches := mutate(t, p, testConfig())
				if len(patches) == 0 {
					t.Fatal("no injection")
				}
				want := tc.prefix
				if existing {
					want += tc.separator + tc.old
				}
				c := out.Spec.Containers[0]
				if got := value(t, c, tc.key).Value; got != want {
					t.Fatalf("merge = %q; want %q", got, want)
				}
				if !reflect.DeepEqual(c.Command, before.Command) || !reflect.DeepEqual(c.Args, before.Args) || c.Image != before.Image {
					t.Fatal("application launch modified")
				}
				if len(c.VolumeMounts) != 1 || !c.VolumeMounts[0].ReadOnly || c.VolumeMounts[0].MountPath != MountPath {
					t.Fatal("application mount is not read-only")
				}
				if len(out.Spec.InitContainers) != 1 || len(out.Spec.Volumes) != 1 || out.Spec.Volumes[0].EmptyDir == nil {
					t.Fatal("shared init volume missing")
				}
				init := out.Spec.InitContainers[0]
				if init.Image != testConfig().AgentImage || *init.SecurityContext.AllowPrivilegeEscalation || !*init.SecurityContext.RunAsNonRoot || !*init.SecurityContext.ReadOnlyRootFilesystem {
					t.Fatal("unsafe init")
				}
				for _, key := range []string{"FI_API_KEY", "FI_SECRET_KEY"} {
					ref := value(t, c, key).ValueFrom.SecretKeyRef
					if ref == nil || ref.Name != "traceai-credentials" || ref.Key != key || ref.Optional == nil || !*ref.Optional {
						t.Fatal("credentials must be optional Secret references")
					}
				}
				if tc.language == "python" && value(t, c, "FI_AUTO_INSTRUMENT").Value != "true" {
					t.Fatal("python bootstrap flag missing")
				}
				if value(t, c, "FI_HIDE_INPUTS").Value != "true" {
					t.Fatal("privacy settings missing")
				}
				second, err := Mutation(rawPod(t, out), false, testConfig())
				if err != nil || len(second) != 0 {
					t.Fatal("not idempotent")
				}
			})
		}
	}
}

func TestOptInAndNamespacePrecedence(t *testing.T) {
	for _, tc := range []struct {
		name, opt            string
		present, ns, mutated bool
	}{
		{"unselected", "", false, false, false}, {"annotation", "true", true, false, true}, {"namespace", "", false, true, true}, {"explicit-opt-out", "false", true, true, false}, {"invalid", "TRUE", true, true, false}, {"empty", "", true, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := testPod("node")
			delete(p.Annotations, Inject)
			if tc.present {
				p.Annotations[Inject] = tc.opt
			}
			patch, err := Mutation(rawPod(t, p), tc.ns, testConfig())
			if err != nil || (len(patch) > 0) != tc.mutated {
				t.Fatalf("patch=%v err=%v", patch, err)
			}
		})
	}
}

func TestSkipUnsafeAndAlreadyInjectedContainers(t *testing.T) {
	cases := []struct {
		name   string
		change func(*corev1.Pod)
	}{
		{"envFrom", func(p *corev1.Pod) {
			p.Spec.Containers[0].EnvFrom = []corev1.EnvFromSource{{SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "options"}}}}
		}},
		{"runtime-valueFrom", func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "NODE_OPTIONS", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.name"}}}}
		}},
		{"duplicate-env", func(p *corev1.Pod) { p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "A"}, {Name: "A"}} }},
		{"marker", func(p *corev1.Pod) { p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: Marker}} }},
		{"node-existing", func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "NODE_OPTIONS", Value: "--import @traceai/auto/register"}}
		}},
		{"java-existing", func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "JAVA_TOOL_OPTIONS", Value: "-javaagent:/agent/traceai-javaagent.jar"}}
		}},
		{"dotnet-existing", func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "DOTNET_STARTUP_HOOKS", Value: "/agent/TraceAI.AutoInstrumentation.dll"}}
		}},
		{"python-existing", func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "FI_AUTO_INSTRUMENT", Value: "true"}}
		}},
		{"ruby-existing", func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "RUBYOPT", Value: "-rtraceai/auto"}}
		}},
		{"disabled", func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "OTEL_SDK_DISABLED", Value: "true"}}
		}},
		{"disabled-unresolved", func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "OTEL_SDK_DISABLED", ValueFrom: &corev1.EnvVarSource{}}}
		}},
		{"path-collision", func(p *corev1.Pod) {
			p.Spec.Containers[0].VolumeMounts = []corev1.VolumeMount{{Name: "other", MountPath: MountPath}}
		}},
		{"ancestor-mount", func(p *corev1.Pod) {
			p.Spec.Containers[0].VolumeMounts = []corev1.VolumeMount{{Name: "other", MountPath: "/opt"}}
		}},
		{"descendant-mount", func(p *corev1.Pod) {
			p.Spec.Containers[0].VolumeMounts = []corev1.VolumeMount{{Name: "other", MountPath: MountPath + "/cache"}}
		}},
		{"volume-name-collision", func(p *corev1.Pod) { p.Spec.Volumes = []corev1.Volume{{Name: VolumeName}} }},
		{"init-name-collision", func(p *corev1.Pod) { p.Spec.InitContainers = []corev1.Container{{Name: InitName}} }},
		{"per-container-opt-out", func(p *corev1.Pod) { p.Annotations[SkipContainers] = " other, app " }},
		{"unknown-language", func(p *corev1.Pod) { p.Annotations[Language] = "lua" }},
		{"go-is-build-time", func(p *corev1.Pod) { p.Annotations[Language] = "go" }},
		{"no-guess-from-image", func(p *corev1.Pod) { delete(p.Annotations, Language) }},
		{"windows", func(p *corev1.Pod) { p.Spec.OS = &corev1.PodOS{Name: corev1.Windows} }},
		{"windows-selector", func(p *corev1.Pod) { p.Spec.NodeSelector = map[string]string{"kubernetes.io/os": "windows"} }},
		{"excluded-namespace", func(p *corev1.Pod) { p.Namespace = "kube-system" }},
		{"python-disabled", func(p *corev1.Pod) {
			p.Annotations[Language] = "python"
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "FI_AUTO_INSTRUMENT", Value: "false"}}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := testPod("node")
			tc.change(&p)
			_, patches := mutate(t, p, testConfig())
			if len(patches) != 0 {
				t.Fatalf("unsafe mutation: %v", patches)
			}
		})
	}
}

func TestReservedApplicationContainerNameSkipsPod(t *testing.T) {
	for _, language := range []string{"node", "java", "dotnet", "python", "ruby"} {
		for _, tc := range []struct {
			name   string
			change func(*corev1.Pod)
		}{
			{"selected-container", func(p *corev1.Pod) { p.Spec.Containers[0].Name = InitName }},
			{"skipped-first-sibling", func(p *corev1.Pod) {
				p.Spec.Containers = append([]corev1.Container{{Name: InitName, Image: "sidecar"}}, p.Spec.Containers...)
				p.Annotations[SkipContainers] = InitName
			}},
			{"unknown-language-last-sibling", func(p *corev1.Pod) {
				p.Spec.Containers = append(p.Spec.Containers, corev1.Container{Name: InitName, Image: "sidecar"})
				p.Annotations[Languages] = `{"traceai-agent-init":"none"}`
			}},
		} {
			t.Run(language+"/"+tc.name, func(t *testing.T) {
				p := testPod(language)
				tc.change(&p)
				raw := rawPod(t, p)
				patch, err := Mutation(raw, false, testConfig())
				if err != nil || len(patch) != 0 {
					t.Fatalf("reserved application name must skip the entire Pod: patch=%v err=%v", patch, err)
				}
				out, _ := apply(t, raw, patch)
				if !reflect.DeepEqual(out, p) {
					t.Fatal("reserved-name Pod was changed")
				}
			})
		}
	}
}

func TestRuntimeValueFromIsNotOverwritten(t *testing.T) {
	for _, language := range []string{"node", "java", "dotnet", "python", "ruby"} {
		t.Run(language, func(t *testing.T) {
			p := testPod(language)
			key, _, _ := runtimeEnv(language)
			p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: key, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "existing-options"}, Key: key}}}}
			_, patch := mutate(t, p, testConfig())
			if len(patch) != 0 {
				t.Fatal("unresolved options were overwritten")
			}
		})
	}
}

func TestSkippedContainerDoesNotPreventEligibleSibling(t *testing.T) {
	p := testPod("node")
	p.Spec.Containers[0].EnvFrom = []corev1.EnvFromSource{{ConfigMapRef: &corev1.ConfigMapEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "opaque-options"}}}}
	p.Spec.Containers = append(p.Spec.Containers, corev1.Container{Name: "eligible", Image: "node-app"})
	out, patch := mutate(t, p, testConfig())
	if len(patch) == 0 || !reflect.DeepEqual(out.Spec.Containers[0], p.Spec.Containers[0]) {
		t.Fatal("unsafe sibling was changed or blocked eligible injection")
	}
	value(t, out.Spec.Containers[1], "NODE_OPTIONS")
}

func TestPerContainerLanguagesAndExistingPodFields(t *testing.T) {
	p := testPod("")
	p.Annotations[Languages] = `{"app":"python","worker":"jvm","sidecar":"node"}`
	p.Annotations[SkipContainers] = "sidecar"
	p.Spec.Containers = append(p.Spec.Containers, corev1.Container{Name: "worker", Image: "java-app"}, corev1.Container{Name: "sidecar", Image: "proxy"})
	p.Spec.InitContainers = []corev1.Container{{Name: "application-init", Image: "application-init"}}
	p.Spec.Volumes = []corev1.Volume{{Name: "data", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}}
	p.Spec.Containers[0].VolumeMounts = []corev1.VolumeMount{{Name: "data", MountPath: "/data"}}
	p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "FI_API_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "own-secret"}, Key: "own-key"}}}, {Name: "FI_PROJECT_NAME", Value: "existing"}, {Name: "FI_HIDE_INPUTS", Value: "false"}}
	out, patch := mutate(t, p, testConfig())
	if len(patch) == 0 {
		t.Fatal("not injected")
	}
	if value(t, out.Spec.Containers[0], "FI_PROJECT_NAME").Value != "existing" || value(t, out.Spec.Containers[0], "FI_API_KEY").ValueFrom.SecretKeyRef.Name != "own-secret" || value(t, out.Spec.Containers[0], "FI_HIDE_INPUTS").Value != "false" {
		t.Fatal("overwrote application configuration")
	}
	value(t, out.Spec.Containers[0], "PYTHONPATH")
	value(t, out.Spec.Containers[1], "JAVA_TOOL_OPTIONS")
	if !reflect.DeepEqual(out.Spec.Containers[2], p.Spec.Containers[2]) || !reflect.DeepEqual(out.Spec.InitContainers[0], p.Spec.InitContainers[0]) || !reflect.DeepEqual(out.Spec.Volumes[0], p.Spec.Volumes[0]) || len(out.Spec.Containers[0].VolumeMounts) != 2 {
		t.Fatal("unowned Pod fields changed")
	}
	if len(out.Spec.InitContainers) != 2 || len(out.Spec.Volumes) != 2 {
		t.Fatal("init/volume duplicated per container")
	}
}

func TestConfigAndHintValidation(t *testing.T) {
	for _, hint := range []string{`null`, `[]`, `{"unknown":"node"}`, `{"app":2}`, `not json`} {
		p := testPod("node")
		p.Annotations[Languages] = hint
		if patch, err := Mutation(rawPod(t, p), false, testConfig()); err == nil || len(patch) != 0 {
			t.Fatalf("accepted invalid hint %s", hint)
		}
	}
	for _, key := range []string{"FI_API_KEY", "FI_SECRET_KEY", "FI_CONFIG_FILE", "NODE_OPTIONS", "TRACEAI_NODE_AGENT", "FI_HIDE_INPUTS=unsafe"} {
		cfg := testConfig()
		cfg.Settings[key] = "not-allowed"
		if err := cfg.Validate(); err == nil {
			t.Fatalf("allowed %s", key)
		}
	}
	for _, change := range []func(*Config){func(c *Config) { c.AgentImage = "" }, func(c *Config) { c.SecretName = "../secret" }, func(c *Config) { c.APIKeyKey = "/bad" }, func(c *Config) { c.ImagePullPolicy = "sometimes" }} {
		cfg := testConfig()
		change(&cfg)
		if err := cfg.Validate(); err == nil {
			t.Fatal("accepted invalid configuration")
		}
	}
	if _, err := Mutation([]byte(`{"spec":`), false, testConfig()); err == nil {
		t.Fatal("accepted malformed Pod")
	}
}

func TestSecretOverrideSettingsAndCommandHints(t *testing.T) {
	for _, tc := range []struct{ command, language string }{{"/usr/bin/python3", "python"}, {"node", "node"}, {"/usr/bin/java", "java"}, {"dotnet", "dotnet"}, {"ruby", "ruby"}} {
		t.Run(tc.language, func(t *testing.T) {
			p := testPod("")
			p.Spec.Containers[0].Command = []string{tc.command}
			p.Annotations[CredentialsSecret] = "workload-credentials"
			cfg := testConfig()
			cfg.APIKeyKey = "api-key"
			cfg.SecretKeyKey = "secret-key"
			cfg.Settings["FI_PROJECT_NAME"] = "literal-$(PASSWORD)"
			out, patch := mutate(t, p, cfg)
			if len(patch) == 0 {
				t.Fatal("no command detection")
			}
			ref := value(t, out.Spec.Containers[0], "FI_API_KEY").ValueFrom.SecretKeyRef
			if ref.Name != "workload-credentials" || ref.Key != "api-key" {
				t.Fatal("wrong Secret reference")
			}
			if value(t, out.Spec.Containers[0], "FI_PROJECT_NAME").Value != "literal-$$(PASSWORD)" {
				t.Fatal("settings permit unintended env expansion")
			}
		})
	}
	p := testPod("node")
	p.Annotations[CredentialsSecret] = "../outside"
	if _, err := Mutation(rawPod(t, p), false, testConfig()); err == nil {
		t.Fatal("accepted invalid Secret")
	}
	cfg := testConfig()
	cfg.SecretName = ""
	delete(p.Annotations, CredentialsSecret)
	out, _ := mutate(t, p, cfg)
	if envIndex(out.Spec.Containers[0].Env, "FI_API_KEY") >= 0 {
		t.Fatal("empty secret should not inject a reference")
	}
}

func TestPatchPreservesUnknownFieldsAndNullLists(t *testing.T) {
	raw := []byte(`{"apiVersion":"v1","kind":"Pod","metadata":{"annotations":{"traceai.futureagi.com/inject":"true","traceai.futureagi.com/language":"node"}},"spec":{"futurePodField":{"keep":true},"containers":[{"name":"app","image":"app","env":null,"volumeMounts":null,"futureContainerField":"untouched"}],"volumes":null,"initContainers":null}}`)
	patch, err := Mutation(raw, false, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	_, out := apply(t, raw, patch)
	var generic map[string]any
	if err := json.Unmarshal(out, &generic); err != nil {
		t.Fatal(err)
	}
	spec := generic["spec"].(map[string]any)
	if spec["futurePodField"].(map[string]any)["keep"] != true || spec["containers"].([]any)[0].(map[string]any)["futureContainerField"] != "untouched" {
		t.Fatal("unknown schema fields dropped")
	}
	p := testPod("node")
	p.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "NODE_OPTIONS"}}
	outPod, _ := mutate(t, p, testConfig())
	if value(t, outPod.Spec.Containers[0], "NODE_OPTIONS").Value == "" {
		t.Fatal("missing env value property not added")
	}
}
