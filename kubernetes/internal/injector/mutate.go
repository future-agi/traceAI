// Package injector implements opt-in, merge-only Pod admission. It never reads
// credentials, modifies commands, or replaces application containers.
package injector

import (
	"encoding/json"
	"fmt"
	"path"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/util/validation"
)

const (
	Prefix            = "traceai.futureagi.com/"
	Inject            = Prefix + "inject"
	Language          = Prefix + "language"
	Languages         = Prefix + "languages"
	SkipContainers    = Prefix + "skip-containers"
	CredentialsSecret = Prefix + "credentials-secret"
	MountPath         = "/opt/traceai"
	VolumeName        = "traceai-agent"
	InitName          = "traceai-agent-init"
	Marker            = "TRACEAI_KUBERNETES_INJECTED"
	DegradedReason    = Prefix + "degraded-reason"
)

type Config struct {
	AgentImage         string
	ImagePullPolicy    corev1.PullPolicy
	SecretName         string
	APIKeyKey          string
	SecretKeyKey       string
	Settings           map[string]string
	ExcludedNamespaces []string
	InitResources      *corev1.ResourceRequirements
}

func (c Config) Validate() error {
	if strings.TrimSpace(c.AgentImage) == "" || strings.ContainsAny(c.AgentImage, " \t\r\n") {
		return fmt.Errorf("agent image is required")
	}
	if c.ImagePullPolicy != "" && c.ImagePullPolicy != corev1.PullAlways && c.ImagePullPolicy != corev1.PullIfNotPresent && c.ImagePullPolicy != corev1.PullNever {
		return fmt.Errorf("invalid image pull policy")
	}
	if c.SecretName != "" && len(validation.IsDNS1123Subdomain(c.SecretName)) != 0 {
		return fmt.Errorf("invalid credentials Secret name")
	}
	for _, key := range []string{c.APIKeyKey, c.SecretKeyKey} {
		if key != "" && len(validation.IsConfigMapKey(key)) != 0 {
			return fmt.Errorf("invalid credentials Secret key")
		}
	}
	if c.InitResources != nil {
		if len(c.InitResources.Claims) != 0 {
			return fmt.Errorf("init resource claims are unsupported")
		}
		for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
			req, limit := c.InitResources.Requests[name], c.InitResources.Limits[name]
			if req.Sign() <= 0 || limit.Sign() <= 0 || req.Cmp(limit) > 0 {
				return fmt.Errorf("init CPU and memory require positive requests not exceeding limits")
			}
		}
		for _, list := range []corev1.ResourceList{c.InitResources.Requests, c.InitResources.Limits} {
			for name := range list {
				if name != corev1.ResourceCPU && name != corev1.ResourceMemory {
					return fmt.Errorf("only init CPU and memory resources are supported")
				}
			}
		}
	}
	for key := range c.Settings {
		switch key {
		case "FI_BASE_URL", "FI_PROJECT_NAME", "FI_PROJECT_TYPE", "FI_INSTRUMENTATIONS", "FI_DISABLED_INSTRUMENTATIONS", "FI_WIRE_CAPTURE", "FI_WIRE_MAX_BODY_BYTES", "FI_AUTO_DEBUG", "FI_SAMPLE_RATE":
		default:
			if !strings.HasPrefix(key, "FI_HIDE_") || len(validation.IsEnvVarName(key)) != 0 {
				return fmt.Errorf("unsupported agent setting (credentials must use Secret references)")
			}
		}
	}
	return nil
}

// Patch changes only fields owned by the injector; unrecognized Pod fields,
// commands, probes, resources, security contexts and existing options survive.
type Patch struct {
	Op    string `json:"op"`
	Path  string `json:"path"`
	Value any    `json:"value"`
}

// Mutation is deterministic and has no network/filesystem side effects. Namespace
// authorization is supplied by the admission handler. On an ambiguous environment
// or a naming/path collision it declines injection rather than guessing.
func Mutation(raw []byte, namespaceEnabled bool, cfg Config) ([]Patch, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	var pod corev1.Pod
	if err := json.Unmarshal(raw, &pod); err != nil {
		return nil, fmt.Errorf("invalid Pod")
	}
	if pod.Spec.OS != nil && pod.Spec.OS.Name == corev1.Windows || pod.Spec.NodeSelector["kubernetes.io/os"] == "windows" {
		return nil, nil
	}
	if contains(cfg.ExcludedNamespaces, pod.Namespace) {
		return nil, nil
	}
	if value, exists := pod.Annotations[Inject]; exists {
		if value != "true" {
			return nil, nil
		}
	} else if !namespaceEnabled {
		return nil, nil
	}
	for _, v := range pod.Spec.Volumes {
		if v.Name == VolumeName {
			return nil, nil
		}
	}
	// Kubernetes requires names to be unique across application and init containers,
	// including application containers that are otherwise ineligible for injection.
	for _, c := range pod.Spec.Containers {
		if c.Name == InitName {
			return nil, nil
		}
	}
	for _, c := range pod.Spec.InitContainers {
		if c.Name == InitName {
			return nil, nil
		}
	}

	hints := map[string]string{}
	if value, exists := pod.Annotations[Languages]; exists {
		if err := json.Unmarshal([]byte(value), &hints); err != nil || hints == nil {
			return nil, fmt.Errorf("invalid language hints")
		}
		containerNames := make(map[string]bool, len(pod.Spec.Containers))
		for _, c := range pod.Spec.Containers {
			containerNames[c.Name] = true
		}
		for name := range hints {
			if !containerNames[name] {
				return nil, fmt.Errorf("language hint names an unknown container")
			}
		}
	}
	secret := cfg.SecretName
	if override, exists := pod.Annotations[CredentialsSecret]; exists {
		if len(validation.IsDNS1123Subdomain(override)) != 0 {
			return nil, fmt.Errorf("invalid credentials Secret annotation")
		}
		secret = override
	}
	skip := strings.Split(pod.Annotations[SkipContainers], ",")
	var patches []Patch
	partialPair, optionalPair := credentialDiagnostics(&pod)
	for i, container := range pod.Spec.Containers {
		if contains(skip, container.Name) || !safeContainer(container) {
			continue
		}
		hint, exists := hints[container.Name]
		if !exists {
			hint = pod.Annotations[Language]
		}
		language := normalizeLanguage(hint)
		if hint == "" && len(container.Command) > 0 {
			language = commandLanguage(container.Command[0])
		}
		if language == "" || language == "go" {
			continue
		}
		key, injected, separator := runtimeEnv(language)
		index := envIndex(container.Env, key)
		if index >= 0 && container.Env[index].ValueFrom != nil {
			continue
		}
		if language == "python" {
			if n := envIndex(container.Env, "FI_AUTO_INSTRUMENT"); n >= 0 {
				// An explicit disable or unresolved reference is the application's choice.
				continue
			}
		}
		base := fmt.Sprintf("/spec/containers/%d", i)
		if len(container.Env) == 0 {
			patches = append(patches, Patch{"add", base + "/env", []corev1.EnvVar{}})
		}
		if index >= 0 {
			old := container.Env[index].Value
			if old != "" {
				injected += separator + old
			}
			patches = append(patches, Patch{"add", fmt.Sprintf("%s/env/%d/value", base, index), injected})
		} else {
			patches = append(patches, Patch{"add", base + "/env/-", corev1.EnvVar{Name: key, Value: injected}})
		}
		add := func(env corev1.EnvVar) {
			if envIndex(container.Env, env.Name) < 0 {
				patches = append(patches, Patch{"add", base + "/env/-", env})
			}
		}
		if language == "python" {
			add(corev1.EnvVar{Name: "FI_AUTO_INSTRUMENT", Value: "true"})
		}
		keys := make([]string, 0, len(cfg.Settings))
		for k := range cfg.Settings {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			add(corev1.EnvVar{Name: k, Value: literalEnv(cfg.Settings[k])})
		}
		// Declaration, not resolved value, establishes application ownership. Never
		// splice a partial pair, including empty literals and unresolved valueFrom.
		if secret != "" && envIndex(container.Env, "FI_API_KEY") < 0 && envIndex(container.Env, "FI_SECRET_KEY") < 0 {
			optionalPair = true
			apiKey, secretKey := cfg.APIKeyKey, cfg.SecretKeyKey
			if apiKey == "" {
				apiKey = "FI_API_KEY"
			}
			if secretKey == "" {
				secretKey = "FI_SECRET_KEY"
			}
			for _, pair := range [][2]string{{"FI_API_KEY", apiKey}, {"FI_SECRET_KEY", secretKey}} {
				optional := true
				add(corev1.EnvVar{Name: pair[0], ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: secret}, Key: pair[1], Optional: &optional}}})
			}
		}
		add(corev1.EnvVar{Name: Marker, Value: "v1"})
		mount := corev1.VolumeMount{Name: VolumeName, MountPath: MountPath, ReadOnly: true}
		if len(container.VolumeMounts) == 0 {
			patches = append(patches, Patch{"add", base + "/volumeMounts", []corev1.VolumeMount{mount}})
		} else {
			patches = append(patches, Patch{"add", base + "/volumeMounts/-", mount})
		}
	}
	if len(patches) == 0 {
		return nil, nil
	}
	volume := corev1.Volume{Name: VolumeName, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: quantity("512Mi")}}}
	if len(pod.Spec.Volumes) == 0 {
		patches = append(patches, Patch{"add", "/spec/volumes", []corev1.Volume{volume}})
	} else {
		patches = append(patches, Patch{"add", "/spec/volumes/-", volume})
	}
	var reasons []string
	if partialPair {
		reasons = append(reasons, "partial-application-credential-pair")
	}
	if optionalPair {
		// No Secret-read privilege: admission cannot know whether optional keys exist.
		reasons = append(reasons, "optional-credential-secret-unverified")
	}
	if len(reasons) > 0 {
		if pod.Annotations == nil {
			patches = append(patches, Patch{"add", "/metadata/annotations", map[string]string{DegradedReason: strings.Join(reasons, ",")}})
		} else {
			patches = append(patches, Patch{"add", "/metadata/annotations/" + strings.ReplaceAll(DegradedReason, "/", "~1"), strings.Join(reasons, ",")})
		}
	}
	init := initContainer(cfg)
	preserveQoS(&pod, &init)
	if len(pod.Spec.InitContainers) == 0 {
		patches = append(patches, Patch{"add", "/spec/initContainers", []corev1.Container{init}})
	} else {
		patches = append(patches, Patch{"add", "/spec/initContainers/-", init})
	}
	return patches, nil
}

func initContainer(cfg Config) corev1.Container {
	no, yes, uid := false, true, int64(65532)
	policy := cfg.ImagePullPolicy
	if policy == "" {
		policy = corev1.PullIfNotPresent
	}
	resources := corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("10m"), corev1.ResourceMemory: resource.MustParse("16Mi")}, Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("128Mi")}}
	if cfg.InitResources != nil {
		resources = *cfg.InitResources.DeepCopy()
	}
	return corev1.Container{
		Name: InitName, Image: cfg.AgentImage, ImagePullPolicy: policy,
		Command: []string{"/traceai-webhook"}, Args: []string{"copy-payload", "--source=/payload", "--target=" + MountPath},
		SecurityContext: &corev1.SecurityContext{RunAsNonRoot: &yes, RunAsUser: &uid, RunAsGroup: &uid, AllowPrivilegeEscalation: &no, ReadOnlyRootFilesystem: &yes, Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}, SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}},
		Resources:       resources,
		VolumeMounts:    []corev1.VolumeMount{{Name: VolumeName, MountPath: MountPath}},
	}
}

// Application init containers are not instrumented, and their credential
// declarations are never completed or rewritten. Still surface safe diagnostics.
func credentialDiagnostics(pod *corev1.Pod) (partial, optional bool) {
	inspect := func(c corev1.Container) {
		api, secret := envIndex(c.Env, "FI_API_KEY"), envIndex(c.Env, "FI_SECRET_KEY")
		partial = partial || (api >= 0) != (secret >= 0)
		for _, i := range []int{api, secret} {
			if i >= 0 && c.Env[i].ValueFrom != nil {
				ref := c.Env[i].ValueFrom.SecretKeyRef
				optional = optional || ref != nil && ref.Optional != nil && *ref.Optional
			}
		}
	}
	for _, c := range pod.Spec.Containers {
		inspect(c)
	}
	for _, c := range pod.Spec.InitContainers {
		inspect(c)
	}
	return
}

// Preserve all three container-level QoS classes without changing application
// resources. Pod-level resource requirements, when present, remain untouched.
func preserveQoS(pod *corev1.Pod, init *corev1.Container) {
	guaranteed, bestEffort := true, true
	inspect := func(c corev1.Container) {
		for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
			req, limit := c.Resources.Requests[name], c.Resources.Limits[name]
			if req.Sign() > 0 || limit.Sign() > 0 {
				bestEffort = false
			}
			if req.Sign() <= 0 || limit.Sign() <= 0 || req.Cmp(limit) != 0 {
				guaranteed = false
			}
		}
	}
	for _, c := range pod.Spec.Containers {
		inspect(c)
	}
	for _, c := range pod.Spec.InitContainers {
		inspect(c)
	}
	if bestEffort {
		init.Resources = corev1.ResourceRequirements{}
	} else if guaranteed {
		init.Resources.Requests = init.Resources.Limits.DeepCopy()
	}
}

func quantity(s string) *resource.Quantity { q := resource.MustParse(s); return &q }
func contains(values []string, value string) bool {
	for _, v := range values {
		if strings.TrimSpace(v) == value {
			return true
		}
	}
	return false
}
func envIndex(env []corev1.EnvVar, key string) int {
	for i, v := range env {
		if v.Name == key {
			return i
		}
	}
	return -1
}
func truthy(s string) bool {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// Literal settings must not accidentally expand another container environment variable.
func literalEnv(s string) string { return strings.ReplaceAll(s, "$", "$$") }

func safeContainer(c corev1.Container) bool {
	// envFrom and valueFrom may conceal options that cannot be safely merged by
	// admission (and the webhook deliberately cannot read Secrets/ConfigMaps).
	if len(c.EnvFrom) > 0 {
		return false
	}
	seen := map[string]bool{}
	for _, env := range c.Env {
		if seen[env.Name] {
			return false
		}
		seen[env.Name] = true
		if env.Name == Marker || env.Name == "OTEL_SDK_DISABLED" && (env.ValueFrom != nil || truthy(env.Value)) || env.Name == "FI_AUTO_INSTRUMENT" && truthy(env.Value) {
			return false
		}
		switch env.Name {
		case "NODE_OPTIONS", "JAVA_TOOL_OPTIONS", "DOTNET_STARTUP_HOOKS", "PYTHONPATH", "RUBYOPT":
			value := strings.ToLower(env.Value)
			if strings.Contains(value, "traceai") || strings.Contains(value, "fi_instrumentation") {
				return false
			}
		}
	}
	for _, mount := range c.VolumeMounts {
		p := path.Clean(mount.MountPath)
		if p == MountPath || strings.HasPrefix(p, MountPath+"/") || strings.HasPrefix(MountPath+"/", strings.TrimSuffix(p, "/")+"/") {
			return false
		}
	}
	return true
}

func normalizeLanguage(s string) string {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "node", "nodejs", "javascript", "typescript", "js", "ts":
		return "node"
	case "java", "jvm":
		return "java"
	case "dotnet", "csharp", "c#":
		return "dotnet"
	case "python", "py":
		return "python"
	case "ruby", "rb":
		return "ruby"
	case "go":
		return "go"
	}
	return ""
}
func commandLanguage(s string) string {
	switch path.Base(s) {
	case "node", "nodejs":
		return "node"
	case "java":
		return "java"
	case "dotnet":
		return "dotnet"
	case "python", "python3", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14":
		return "python"
	case "ruby":
		return "ruby"
	}
	return ""
}
func runtimeEnv(language string) (key, value, separator string) {
	switch language {
	case "node":
		return "NODE_OPTIONS", "--require " + MountPath + "/node/preload.cjs", " "
	case "java":
		return "JAVA_TOOL_OPTIONS", "-javaagent:" + MountPath + "/java/traceai-javaagent.jar", " "
	case "dotnet":
		return "DOTNET_STARTUP_HOOKS", MountPath + "/dotnet/TraceAI.AutoInstrumentation.dll", ":"
	case "python":
		return "PYTHONPATH", MountPath + "/python", ":"
	case "ruby":
		return "RUBYOPT", "-r" + MountPath + "/ruby/preload.rb", " "
	}
	return "", "", ""
}
