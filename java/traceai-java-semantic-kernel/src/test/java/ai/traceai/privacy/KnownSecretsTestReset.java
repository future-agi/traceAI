package ai.traceai.privacy;

/** Lets a test in another package, or another module on the same class path, start from an empty process-wide registry. */
public final class KnownSecretsTestReset {
    private KnownSecretsTestReset() {
    }

    public static void reset() {
        KnownSecrets.resetProcessForTest();
    }
}
