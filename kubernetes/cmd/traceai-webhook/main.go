package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/future-agi/traceAI/kubernetes/internal/injector"
	"github.com/future-agi/traceAI/kubernetes/internal/payload"
	corev1 "k8s.io/api/core/v1"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		// All error strings are fixed diagnostics, never environment or Pod data.
		log.Print(err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) > 0 && args[0] == "stage-payload" {
		f := flag.NewFlagSet("stage-payload", flag.ContinueOnError)
		launcher := f.String("launcher", "/usr/local/bin/traceai", "trusted full launcher binary")
		root := f.String("root", "/opt/traceai", "final application mount path")
		if f.Parse(args[1:]) != nil || f.NArg() != 0 {
			return fmt.Errorf("invalid staging flags")
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		defer cancel()
		if err := payload.Stage(ctx, *launcher, *root); err != nil {
			return fmt.Errorf("payload staging failed; verify the full launcher, empty staging root and complete agent artifacts")
		}
		return nil
	}
	if len(args) > 0 && args[0] == "copy-payload" {
		f := flag.NewFlagSet("copy-payload", flag.ContinueOnError)
		source := f.String("source", "/payload", "image payload")
		target := f.String("target", injector.MountPath, "shared emptyDir mount")
		if f.Parse(args[1:]) != nil || f.NArg() != 0 {
			return fmt.Errorf("invalid copy flags")
		}
		if err := payload.Copy(*source, *target); err != nil {
			return fmt.Errorf("payload copy failed; verify the trusted image inventory and writable emptyDir")
		}
		return nil
	}
	f := flag.NewFlagSet("traceai-webhook", flag.ContinueOnError)
	address := f.String("listen", ":8443", "HTTPS address")
	cert := f.String("tls-cert", "/tls/tls.crt", "serving certificate")
	key := f.String("tls-key", "/tls/tls.key", "serving key")
	clientCA := f.String("tls-client-ca", "", "dedicated API-server client CA PEM; requires verified client certificates")
	admissions := f.Int("max-concurrent-admissions", 32, "nonblocking admission concurrency budget (1-1024)")
	lookups := f.Int("max-concurrent-lookups", 8, "nonblocking namespace lookup concurrency budget")
	initResources := f.String("init-resources", "", "init CPU/memory requests and limits as JSON; original Pod QoS is preserved")
	image := f.String("agent-image", "", "trusted payload image (prefer digest)")
	policy := f.String("image-pull-policy", "IfNotPresent", "init image pull policy")
	secret := f.String("credentials-secret", "traceai-credentials", "Secret in each workload namespace")
	apiKey := f.String("api-key-key", "FI_API_KEY", "Secret key")
	secretKey := f.String("secret-key-key", "FI_SECRET_KEY", "Secret key")
	excluded := f.String("excluded-namespaces", "kube-system,kube-public,kube-node-lease", "never inject these namespaces")
	if f.Parse(args) != nil || f.NArg() != 0 {
		return fmt.Errorf("invalid server flags")
	}
	cfg := injector.Config{AgentImage: *image, ImagePullPolicy: corev1.PullPolicy(*policy), SecretName: *secret, APIKeyKey: *apiKey, SecretKeyKey: *secretKey, ExcludedNamespaces: strings.Split(*excluded, ",")}
	if own := os.Getenv("POD_NAMESPACE"); own != "" {
		cfg.ExcludedNamespaces = append(cfg.ExcludedNamespaces, own)
	}
	if settings := os.Getenv("TRACEAI_SETTINGS_JSON"); settings != "" {
		if json.Unmarshal([]byte(settings), &cfg.Settings) != nil {
			return fmt.Errorf("invalid agent settings")
		}
	}
	if *initResources != "" {
		cfg.InitResources = &corev1.ResourceRequirements{}
		if json.Unmarshal([]byte(*initResources), cfg.InitResources) != nil {
			return fmt.Errorf("invalid init resources")
		}
	}
	if err := cfg.Validate(); err != nil {
		return err
	}
	limits, err := injector.NewAdmissionLimits(*admissions, *lookups)
	if err != nil {
		return err
	}
	tlsConfig, err := injector.TLSConfigWithClientCA(*cert, *key, *clientCA)
	if err != nil {
		return err
	}
	namespaces, err := injector.InClusterNamespaces()
	if err != nil {
		log.Print("namespace lookup unavailable; explicit Pod opt-in remains available")
	}
	handler := injector.Handler{Config: cfg, Limits: limits}
	if namespaces != nil {
		handler.Namespaces = namespaces
	}
	mux := http.NewServeMux()
	mux.Handle("/mutate", handler)
	for _, p := range []string{"/healthz", "/readyz"} {
		mux.HandleFunc(p, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	}
	server := &http.Server{Addr: *address, Handler: mux, TLSConfig: tlsConfig, ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 4 * time.Second, WriteTimeout: 4 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	log.Print("traceAI admission server listening")
	if err := server.ListenAndServeTLS("", ""); err != nil && err != http.ErrServerClosed {
		return fmt.Errorf("HTTPS server stopped")
	}
	return nil
}
