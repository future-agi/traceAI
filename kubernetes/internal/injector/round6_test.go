package injector

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	jsonpatch "github.com/evanphx/json-patch/v5"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

// These three tests reproduced on the pre-fix implementation: literal and
// valueFrom credential splicing, Guaranteed -> Burstable, and 40 active lookups.
func TestR6PartialPair(t *testing.T) {
	for _, from := range []bool{false, true} {
		p := testPod("node")
		env := corev1.EnvVar{Name: "FI_API_KEY", Value: "synthetic-application-api"}
		if from {
			env.Value = ""
			env.ValueFrom = &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "application"}, Key: "api"}}
		}
		p.Spec.Containers[0].Env = []corev1.EnvVar{env}
		out, _ := mutate(t, p, testConfig())
		if envIndex(out.Spec.Containers[0].Env, "FI_SECRET_KEY") >= 0 {
			t.Errorf("valueFrom=%t: operator completed a partial application pair", from)
		}
		if out.Annotations[DegradedReason] != "partial-application-credential-pair" {
			t.Fatal("partial pair lacks fixed, value-free degraded reason")
		}
	}
}

func TestR6GuaranteedQoS(t *testing.T) {
	p := testPod("node")
	p.Spec.Containers[0].Resources = guaranteedResources()
	out, _ := mutate(t, p, testConfig())
	init := out.Spec.InitContainers[0]
	for _, r := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
		request := init.Resources.Requests[r]
		if request.Cmp(init.Resources.Limits[r]) != 0 {
			t.Errorf("injected init downgrades Guaranteed QoS: %s", r)
		}
	}
}

func TestR6ConcurrentAdmissions(t *testing.T) {
	var active, maximum atomic.Int32
	gate := make(chan struct{})
	limits, _ := NewAdmissionLimits(32, 8)
	waitWorkers := func() {
		// All handlers have returned: no new lookup permits can be acquired.
		// Fill the semaphore to wait for even not-yet-scheduled workers to exit.
		for i := 0; i < cap(limits.lookups); i++ {
			select {
			case limits.lookups <- struct{}{}:
			case <-time.After(2 * time.Second):
				t.Fatal("lookup worker did not release its permit")
			}
		}
		for i := 0; i < cap(limits.lookups); i++ {
			<-limits.lookups
		}
	}
	h := Handler{Config: testConfig(), Limits: limits, Namespaces: lookupFunc(func(context.Context, string) (bool, error) {
		n := active.Add(1)
		defer active.Add(-1)
		for {
			old := maximum.Load()
			if n <= old || maximum.CompareAndSwap(old, n) {
				break
			}
		}
		<-gate // Deliberately ignores cancellation; still cannot occupy >8 workers.
		return true, nil
	})}
	review := admissionRequest(t)
	p := testPod("node")
	delete(p.Annotations, Inject)
	review.Request.Object.Raw = rawPod(t, p)
	data, _ := json.Marshal(review)
	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/mutate", bytes.NewReader(data)))
		}()
	}
	// Every admission must complete even though the lookup implementation ignores
	// its context. Workers retain their slots until explicitly released below.
	finished := make(chan struct{})
	go func() { wg.Wait(); close(finished) }()
	select {
	case <-finished:
	case <-time.After(2 * time.Second):
		close(gate)
		wg.Wait()
		waitWorkers()
		t.Fatal("admissions blocked on uncancellable namespace lookup")
	}
	close(gate)
	waitWorkers()
	if maximum.Load() < 1 || maximum.Load() > 8 {
		t.Errorf("namespace worker peak=%d; budget=8", maximum.Load())
	}
}

func TestAdmissionCredentialPairs(t *testing.T) {
	ref := func(name, key string, optional bool) corev1.EnvVar {
		return corev1.EnvVar{Name: key, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: name}, Key: key, Optional: &optional}}}
	}
	cases := []struct {
		name    string
		env     []corev1.EnvVar
		partial bool
	}{
		{"neither", nil, false},
		{"api-literal", []corev1.EnvVar{{Name: "FI_API_KEY", Value: "synthetic-api"}}, true},
		{"secret-literal", []corev1.EnvVar{{Name: "FI_SECRET_KEY", Value: "synthetic-secret"}}, true},
		{"api-empty", []corev1.EnvVar{{Name: "FI_API_KEY"}}, true},
		{"secret-empty", []corev1.EnvVar{{Name: "FI_SECRET_KEY"}}, true},
		{"api-valueFrom", []corev1.EnvVar{ref("app", "FI_API_KEY", false)}, true},
		{"secret-valueFrom", []corev1.EnvVar{ref("app", "FI_SECRET_KEY", false)}, true},
		{"api-optional-valueFrom", []corev1.EnvVar{ref("app", "FI_API_KEY", true)}, true},
		{"complete-literal", []corev1.EnvVar{{Name: "FI_API_KEY", Value: "synthetic-api"}, {Name: "FI_SECRET_KEY", Value: "synthetic-secret"}}, false},
		{"complete-valueFrom", []corev1.EnvVar{ref("app", "FI_API_KEY", false), ref("app", "FI_SECRET_KEY", false)}, false},
		{"complete-optional", []corev1.EnvVar{ref("app", "FI_API_KEY", true), ref("app", "FI_SECRET_KEY", true)}, false},
		{"complete-mixed", []corev1.EnvVar{{Name: "FI_API_KEY", Value: "synthetic-api"}, ref("app", "FI_SECRET_KEY", false)}, false},
	}
	for _, tc := range cases {
		for _, isInit := range []bool{false, true} {
			kind := "application"
			if isInit {
				kind = "init"
			}
			t.Run(kind+"/"+tc.name, func(t *testing.T) {
				p := testPod("node")
				if isInit {
					p.Spec.InitContainers = []corev1.Container{{Name: "application-init", Image: "unchanged", Env: tc.env}}
				} else {
					p.Spec.Containers[0].Env = tc.env
				}
				review := admissionRequest(t)
				review.Request.Object.Raw = rawPod(t, p)
				response := serve(t, Handler{Config: testConfig()}, review)
				patch, err := jsonpatch.DecodePatch(response.Patch)
				if err != nil {
					t.Fatal(err)
				}
				data, err := patch.Apply(review.Request.Object.Raw)
				if err != nil {
					t.Fatal(err)
				}
				var out corev1.Pod
				if err := json.Unmarshal(data, &out); err != nil {
					t.Fatal(err)
				}
				if isInit {
					if !reflect.DeepEqual(p.Spec.InitContainers[0], out.Spec.InitContainers[0]) {
						t.Fatal("application init container modified")
					}
				} else {
					for _, key := range []string{"FI_API_KEY", "FI_SECRET_KEY"} {
						before, after := envIndex(tc.env, key), envIndex(out.Spec.Containers[0].Env, key)
						if before >= 0 && (after < 0 || !reflect.DeepEqual(tc.env[before], out.Spec.Containers[0].Env[after])) {
							t.Fatal("application credential declaration modified")
						}
						if before < 0 && len(tc.env) > 0 && after >= 0 {
							t.Fatal("operator spliced credential source")
						}
					}
				}
				if strings.Contains(out.Annotations[DegradedReason], "partial-application-credential-pair") != tc.partial {
					t.Fatal("incorrect partial pair diagnostic")
				}
				copier := out.Spec.InitContainers[len(out.Spec.InitContainers)-1]
				if len(copier.Env) != 0 {
					t.Fatal("payload copier received credentials")
				}
				if bytes.Contains(response.Patch, []byte("synthetic-api")) || bytes.Contains(response.Patch, []byte("synthetic-secret")) {
					t.Fatal("credential values copied into response/diagnostics")
				}
			})
		}
	}
}

func TestAdmissionMissingOptionalSecretKeys(t *testing.T) {
	for _, present := range []string{"", "FI_API_KEY", "FI_SECRET_KEY", "both"} {
		t.Run(present, func(t *testing.T) {
			review := admissionRequest(t)
			response := serve(t, Handler{Config: testConfig()}, review)
			var patches []Patch
			if err := json.Unmarshal(response.Patch, &patches); err != nil {
				t.Fatal(err)
			}
			out, _ := apply(t, review.Request.Object.Raw, patches)
			// Simulate kubelet's optional projection with an in-memory Secret. No
			// cluster or privileged Secret-reading API client is involved.
			secret := map[string]string{}
			for _, key := range []string{"FI_API_KEY", "FI_SECRET_KEY"} {
				if present == key || present == "both" {
					secret[key] = "synthetic-" + key
				}
				v := value(t, out.Spec.Containers[0], key)
				r := v.ValueFrom.SecretKeyRef
				if r.Name != "traceai-credentials" || r.Key != key || r.Optional == nil || !*r.Optional || v.Value != "" {
					t.Fatal("optional pair lost common operator source")
				}
			}
			resolved := map[string]string{}
			for _, v := range out.Spec.Containers[0].Env {
				if v.ValueFrom != nil && v.ValueFrom.SecretKeyRef != nil {
					if v, ok := secret[v.ValueFrom.SecretKeyRef.Key]; ok {
						resolved[v] = v
					}
				}
			}
			if len(resolved) != len(secret) {
				t.Fatal("missing optional key acquired a fallback value")
			}
			if out.Annotations[DegradedReason] != "optional-credential-secret-unverified" {
				t.Fatal("optional key verification limitation not surfaced")
			}
		})
	}
}

func guaranteedResources() corev1.ResourceRequirements {
	list := corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("128Mi")}
	return corev1.ResourceRequirements{Requests: list.DeepCopy(), Limits: list.DeepCopy()}
}

func TestInitResourceConfigurationAndQoS(t *testing.T) {
	for _, custom := range []bool{false, true} {
		for _, class := range []string{"Guaranteed", "Burstable", "BestEffort"} {
			t.Run(class+"/custom="+map[bool]string{true: "true", false: "false"}[custom], func(t *testing.T) {
				p := testPod("node")
				if class != "BestEffort" {
					p.Spec.Containers[0].Resources = guaranteedResources()
				}
				if class == "Burstable" {
					p.Spec.Containers[0].Resources.Requests[corev1.ResourceCPU] = resource.MustParse("50m")
				}
				p.Spec.InitContainers = []corev1.Container{{Name: "existing-init", Image: "unchanged", Resources: *p.Spec.Containers[0].Resources.DeepCopy()}}
				before := p.DeepCopy()
				cfg := testConfig()
				if custom {
					cfg.InitResources = &corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("20m"), corev1.ResourceMemory: resource.MustParse("32Mi")}, Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("200m"), corev1.ResourceMemory: resource.MustParse("256Mi")}}
				}
				out, _ := mutate(t, p, cfg)
				init := out.Spec.InitContainers[1]
				if !reflect.DeepEqual(before.Spec.Containers[0].Resources, out.Spec.Containers[0].Resources) || !reflect.DeepEqual(before.Spec.InitContainers[0], out.Spec.InitContainers[0]) {
					t.Fatal("application resource requirements changed")
				}
				if class == "BestEffort" {
					if len(init.Resources.Requests)+len(init.Resources.Limits) != 0 {
						t.Fatal("BestEffort QoS changed")
					}
					return
				}
				want := initContainer(cfg).Resources
				if class == "Guaranteed" {
					want.Requests = want.Limits.DeepCopy()
				}
				if !reflect.DeepEqual(want, init.Resources) {
					t.Fatal("wrong init resource/QoS policy")
				}
				if cfg.InitResources != nil && !reflect.DeepEqual(cfg.InitResources.Requests, initContainer(cfg).Resources.Requests) {
					t.Fatal("config mutated")
				}
			})
		}
	}
}

func TestAdmissionOverloadAndLookupTimeout(t *testing.T) {
	limits, _ := NewAdmissionLimits(1, 1)
	h := Handler{Config: testConfig(), Limits: limits}
	limits.admissions <- struct{}{}
	response := serve(t, h, admissionRequest(t))
	<-limits.admissions
	if len(response.Patch) != 0 || response.PatchType != nil || len(response.Warnings) == 0 || !strings.Contains(response.Warnings[0], "overloaded") {
		t.Fatal("overload must allow without mutation")
	}
	// The permit must be reusable immediately after overload.
	if len(serve(t, h, admissionRequest(t)).Patch) == 0 {
		t.Fatal("admission permit leaked")
	}

	gate := make(chan struct{})
	finished := make(chan struct{})
	h.Namespaces = lookupFunc(func(context.Context, string) (bool, error) { defer close(finished); <-gate; return true, nil })
	defer func() { close(gate); <-finished }()
	review := admissionRequest(t)
	p := testPod("node")
	delete(p.Annotations, Inject)
	review.Request.Object.Raw = rawPod(t, p)
	start := time.Now()
	response = serve(t, h, review)
	if len(response.Patch) != 0 || time.Since(start) > time.Second {
		t.Fatal("lookup timeout blocked admission")
	}
	// The timed-out worker is still running and must retain its lookup slot.
	start = time.Now()
	response = serve(t, h, review)
	if len(response.Patch) != 0 || time.Since(start) > 200*time.Millisecond {
		t.Fatal("lookup overload did not fail open immediately")
	}
	if len(serve(t, h, admissionRequest(t)).Patch) == 0 {
		t.Fatal("lookup overload affected explicit Pod opt-in")
	}
}

func TestInvalidAdmissionAndResourceLimits(t *testing.T) {
	for _, counts := range [][2]int{{0, 1}, {1, 0}, {1, 2}, {1025, 1}} {
		if _, err := NewAdmissionLimits(counts[0], counts[1]); err == nil {
			t.Fatal("invalid concurrency accepted")
		}
	}
	for _, resources := range []corev1.ResourceRequirements{
		{},
		{Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("-1")}},
		{Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("200m"), corev1.ResourceMemory: resource.MustParse("1Mi")}, Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("1Mi")}},
	} {
		cfg := testConfig()
		cfg.InitResources = &resources
		if cfg.Validate() == nil {
			t.Fatal("invalid init resources accepted")
		}
	}
}
