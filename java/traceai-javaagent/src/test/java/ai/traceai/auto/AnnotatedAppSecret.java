package ai.traceai.auto;

/** Values the annotated child application uses, shared with the assertions. */
final class AnnotatedAppSecret {
    static final String VALUE = ai.traceai.apptest.AnnotatedApp.SECRET_ARG;
    static final int LENGTH = VALUE.length();
    static final String KEY = "fi-key-0123456789abcdef";
    private AnnotatedAppSecret() { }
}
