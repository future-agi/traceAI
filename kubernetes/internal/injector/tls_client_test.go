package injector

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	admissionv1 "k8s.io/api/admission/v1"
)

type testCA struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func makeCA(t *testing.T, name string) testCA {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: name}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return testCA{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}
func issueCert(t *testing.T, ca testCA, usage x509.ExtKeyUsage) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "synthetic-control-plane"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{usage}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}}
	der, err := x509.CreateCertificate(rand.Reader, template, ca.cert, &key.PublicKey, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}
func writeTestPair(t *testing.T, dir string, cert tls.Certificate) (string, string) {
	t.Helper()
	certFile, keyFile := filepath.Join(dir, "server.crt"), filepath.Join(dir, "server.key")
	key, err := x509.MarshalPKCS8PrivateKey(cert.PrivateKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(certFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Certificate[0]}), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keyFile, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: key}), 0600); err != nil {
		t.Fatal(err)
	}
	return certFile, keyFile
}

func TestTLSVerifiedAPIServerCallerAndCARotation(t *testing.T) {
	serverCA, clientCA, otherCA := makeCA(t, "serving"), makeCA(t, "dedicated-apiserver"), makeCA(t, "untrusted")
	serverCert := issueCert(t, serverCA, x509.ExtKeyUsageServerAuth)
	dir := t.TempDir()
	certFile, keyFile := writeTestPair(t, dir, serverCert)
	caFile := filepath.Join(dir, "client-ca.pem")
	if err := os.WriteFile(caFile, clientCA.pem, 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := TLSConfigWithClientCA(certFile, keyFile, caFile)
	if err != nil || cfg.ClientAuth != tls.RequireAndVerifyClientCert {
		t.Fatalf("mTLS not required: %v", err)
	}
	cfg.Certificates = []tls.Certificate{serverCert}
	server := httptest.NewUnstartedServer(Handler{Config: testConfig()})
	server.Config.ErrorLog = log.New(io.Discard, "", 0)
	server.TLS = cfg
	server.StartTLS()
	defer server.Close()
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM(serverCA.pem)
	request := func(cert *tls.Certificate) bool {
		t.Helper()
		clientTLS := &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}
		if cert != nil {
			clientTLS.Certificates = []tls.Certificate{*cert}
		}
		transport := &http.Transport{TLSClientConfig: clientTLS, DisableKeepAlives: true}
		defer transport.CloseIdleConnections()
		client := &http.Client{Transport: transport, Timeout: time.Second}
		review := admissionRequest(t)
		data, _ := json.Marshal(review)
		response, err := client.Post(server.URL, "application/json", bytes.NewReader(data))
		if err != nil {
			return false
		}
		defer response.Body.Close()
		var out admissionv1.AdmissionReview
		if err := json.NewDecoder(response.Body).Decode(&out); err != nil {
			t.Fatal(err)
		}
		return out.Response != nil && out.Response.Allowed && out.Response.UID == review.Request.UID && len(out.Response.Patch) > 0
	}
	trusted := issueCert(t, clientCA, x509.ExtKeyUsageClientAuth)
	untrusted := issueCert(t, otherCA, x509.ExtKeyUsageClientAuth)
	wrongUsage := issueCert(t, clientCA, x509.ExtKeyUsageServerAuth)
	for _, tc := range []struct {
		name string
		cert *tls.Certificate
		want bool
	}{
		{"anonymous", nil, false}, {"trusted", &trusted, true}, {"untrusted", &untrusted, false}, {"wrong-EKU", &wrongUsage, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if request(tc.cert) != tc.want {
				t.Fatal("incorrect TLS caller authentication")
			}
		})
	}
	if err := os.WriteFile(caFile, otherCA.pem, 0600); err != nil {
		t.Fatal(err)
	}
	if request(&trusted) || !request(&untrusted) {
		t.Fatal("client CA rotation not applied to new handshakes")
	}
	if err := os.WriteFile(caFile, []byte("invalid synthetic CA"), 0600); err != nil {
		t.Fatal(err)
	}
	if request(&untrusted) {
		t.Fatal("invalid rotated CA did not fail closed")
	}
	if _, err := TLSConfigWithClientCA(certFile, keyFile, caFile); err == nil {
		t.Fatal("invalid initial CA accepted")
	}
	if _, err := TLSConfigWithClientCA(certFile, keyFile, filepath.Join(dir, "missing")); err == nil {
		t.Fatal("missing initial CA accepted")
	}
	plain, err := TLSConfig(certFile, keyFile)
	if err != nil || plain.ClientAuth != tls.NoClientCert {
		t.Fatal("managed-cluster server-only TLS mode changed")
	}
}
