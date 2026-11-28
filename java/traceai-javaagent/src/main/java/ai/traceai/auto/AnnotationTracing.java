package ai.traceai.auto;

import com.google.gson.Gson;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.ContextKey;
import io.opentelemetry.context.Scope;
import java.lang.annotation.Annotation;
import java.lang.reflect.Method;
import java.lang.reflect.Parameter;
import java.util.*;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

/**
 * Spans for application methods annotated with {@code ai.traceai.annotations.*}. The annotations are matched by
 * name and read reflectively, so the agent needs no dependency on the annotation module and the application's
 * class loader keeps its own copy. Spans come from the agent's own provider, with the same hide switches,
 * known-secret replacement and sampling as every other agent span.
 */
final class AnnotationTracing {
    /** Present in the context while an annotated method runs, so nested annotated and wire spans parent under it. */
    static final ContextKey<Boolean> ACTIVE = ContextKey.named("traceai.auto.annotation");
    /** The id of the application's own span that was current when the outermost annotated call began ("" if none): a different current span inside the method is the application's own inner span. */
    static final ContextKey<String> APP_SPAN = ContextKey.named("traceai.auto.annotation.app-span");
    /** At each annotated entry: the id of the agent's span for the Google ADK span that is current there, "" when none is. */
    static final ContextKey<String> ADK_SPAN = ContextKey.named("traceai.auto.annotation.adk-span");
    static final String PACKAGE = "ai.traceai.annotations.";
    static String adkSpanOf(Context context) { String id = context.get(ADK_SPAN); return id == null ? "" : id; }
    /** Annotation simple name -> span kind, in the order used when a method carries more than one. */
    private static final LinkedHashMap<String, String> KINDS = new LinkedHashMap<>();
    static {
        KINDS.put("Agent", "AGENT"); KINDS.put("Chain", "CHAIN"); KINDS.put("LLM", "LLM"); KINDS.put("Tool", "TOOL"); KINDS.put("Span", null);
    }
    private static final int MAX_ITEMS = 100, MAX_NAME = 1000, MAX_CAPTURE = 64 << 10;
    private static final Gson GSON = new com.google.gson.GsonBuilder().disableHtmlEscaping().create();

    /** What the exit advice needs; handed back to it as an opaque object. */
    private static final class Started {
        final Span span; final Scope scope; final AutoConfig config; final boolean returnsVoid; final Class<?> returnType; final int limit;
        final AtomicBoolean ended = new AtomicBoolean();
        /** Characters of text one captured input or output may hold: the configured body limit, and never more than 64 KiB. */
        int chars() { return limit > 0 ? Math.min(limit, MAX_CAPTURE) : MAX_CAPTURE; }
        Started(Span span, Scope scope, AutoConfig config, Class<?> returnType) {
            this.span = span; this.scope = scope; this.config = config; this.returnType = returnType; this.returnsVoid = returnType == void.class; this.limit = config.maxBodyBytes;
        }
    }
    /** The annotated method's span kind and names, from its annotations. */
    private record Meta(String kind, String name, String description) {}

    static boolean annotationName(String type) { return type.startsWith(PACKAGE) && KINDS.containsKey(type.substring(PACKAGE.length())); }

    static Object enter(Method method, Object[] arguments) {
        try {
            TraceAIAuto.RuntimeState state = TraceAIAuto.currentRuntime();
            if (state == null || !TraceAIAuto.annotationsEnabled() || TraceAIAuto.besideOtelJavaAgent() || !state.acceptingResults()) return null;
            Context current = Context.current();
            if (Boolean.TRUE.equals(current.get(TraceAIAuto.SUPPRESS)) || TraceAIAuto.IN_FLIGHT.get()) return null;
            Meta meta = meta(method); if (meta == null) return null;
            OtelBridge.Snapshot snapshot = OtelBridge.snapshot();
            // An enclosing annotated span wins, unless a Google ADK span that began after it is current (ADK called this method, as a
            // tool or a model): that span is the parent. Otherwise the application's active span (or none).
            boolean underAdk = !snapshot.adkSpanId().isEmpty() && !snapshot.adkSpanId().equals(adkSpanOf(current));
            Context parent = current.get(ACTIVE) != null && !underAdk ? current : snapshot.parent();
            Span span = state.tracer().spanBuilder(truncate(meta.name(), MAX_NAME)).setSpanKind(SpanKind.INTERNAL).setParent(parent).startSpan();
            Started started;
            // At every annotated entry, nested ones included: the application's own span that is current right here.
            Scope scope = parent.with(span).with(ACTIVE, Boolean.TRUE).with(APP_SPAN, snapshot.appSpanId()).with(ADK_SPAN, snapshot.adkSpanId()).makeCurrent();
            try { started = new Started(span, scope, state.config(), method.getReturnType()); }
            catch (Throwable error) { scope.close(); span.end(); return null; }
            if (span.isRecording()) {
                try {
                    set(started, "fi.span.kind", meta.kind()); set(started, "gen_ai.span.kind", meta.kind().toLowerCase(Locale.ROOT));
                    snapshot.attributes().forEach((key, value) -> set(started, key, value));
                    if (meta.kind().equals("TOOL")) {
                        set(started, "gen_ai.tool.name", meta.name()); set(started, "tool.name", meta.name());
                        if (!meta.description().isEmpty()) set(started, "gen_ai.tool.description", meta.description());
                    }
                    input(started, method, arguments);
                } catch (Throwable ignored) { }
            }
            return started;
        } catch (Throwable ignored) { return null; }
    }

    /** Returns a replacement for the method's return value (a Reactor publisher), or null to keep it. */
    static Object exit(Object token, Object returned, Throwable error) {
        if (!(token instanceof Started started)) return null;
        try { started.scope.close(); } catch (Throwable ignored) { }
        try {
            if (error != null) { finish(started, null, error); return null; }
            if (returned instanceof CompletionStage<?> stage) {
                stage.whenComplete((value, failure) -> {
                    Throwable cause = failure instanceof CompletionException && failure.getCause() != null ? failure.getCause() : failure;
                    if (cause instanceof java.util.concurrent.CancellationException) finish(started, null, null, true, false);
                    else finish(started, value, failure);
                });
                return null;
            }
            // Only a method declared to return exactly Mono or Flux gets the wrapper. Any other declared type (Object, Publisher, a subclass,
            // an erased type variable) keeps the returned instance as it is: the span ends at return, as for any other value.
            String declared = started.returnType.getName();
            if (declared.equals("reactor.core.publisher.Mono") || declared.equals("reactor.core.publisher.Flux")) {
                Object wrapped = Reactor.wrap(started, returned);
                if (wrapped != null) return wrapped;
            }
            finish(started, returned, null);
        } catch (Throwable ignored) { finish(started, null, null); }
        return null;
    }

    private static void finish(Started started, Object value, Throwable error) { finish(started, value, error, false, true); }

    /** Ends the span once. A cancelled span carries the marker, and its output only when something was emitted before the cancel. */
    private static void finish(Started started, Object value, Throwable error, boolean cancelled, boolean hasOutput) {
        if (!started.ended.compareAndSet(false, true)) return;
        try {
            if (started.span.isRecording()) {
                if (cancelled) started.span.setAttribute(AttributeKey.booleanKey("traceai.cancelled"), true);
                Throwable cause = error instanceof CompletionException && error.getCause() != null ? error.getCause() : error;
                if (cause != null) fail(started, cause);
                else {
                    if (!started.returnsVoid && hasOutput) output(started, value);
                    started.span.setStatus(StatusCode.OK);
                }
            }
        } catch (Throwable ignored) { }
        finally { try { started.span.end(); } catch (Throwable ignored) { } }
    }

    private static void input(Started started, Method method, Object[] arguments) {
        if (TraceAIAuto.hidden("input.value", "v", null, started.config)) return;
        Map<String, Object> values = inputValues(method, arguments);
        if (values.isEmpty()) return;
        // One walk over all the arguments, so the depth, item, node and character bounds hold for the whole input.
        set(started, "input.value", render(values, started.chars())); set(started, "input.mime_type", "application/json");
    }

    private static final AtomicBoolean NAMES_WARNED = new AtomicBoolean();
    /** Lets a test see the warning again. */
    static void resetNameWarning() { NAMES_WARNED.set(false); }

    /**
     * The arguments of a call by parameter name. A credential name redacts its value. A class compiled without {@code -parameters} has no
     * names ({@code arg0}, {@code arg1}), so no argument can be judged by its name: every argument except null, a Boolean and an enum
     * constant is redacted whatever its type (numbers, arrays, collections, Optionals, Maps, records and objects included), and one
     * warning per process says how to export them.
     */
    static Map<String, Object> inputValues(Method method, Object[] arguments) {
        Parameter[] parameters = method.getParameters();
        Map<String, Object> values = new LinkedHashMap<>();
        for (int i = 0; i < parameters.length && i < arguments.length; i++) {
            if (excluded(parameters[i])) continue;
            boolean named = parameters[i].isNamePresent();
            String name = named ? parameters[i].getName() : "arg" + i;
            Object argument = arguments[i];
            if (named ? CredentialPairs.credentialName(name) : !exportedWithoutName(argument)) {
                if (!named) warnNames();
                argument = "<redacted>";
            }
            values.put(name, argument);
        }
        return values;
    }
    /** What carries no secret whatever the parameter is called: nothing, a flag, or a constant of an enum. */
    private static boolean exportedWithoutName(Object argument) {
        return argument == null || argument instanceof Boolean || argument instanceof Enum<?>;
    }
    private static void warnNames() {
        if (NAMES_WARNED.compareAndSet(false, true))
            System.err.println("[traceai-auto] Arguments of annotated methods are redacted because this class has no parameter names in its class file. Compile with -parameters to export them.");
    }
    private static void output(Started started, Object value) {
        if (TraceAIAuto.hidden("output.value", "v", null, started.config)) return;
        set(started, "output.value", render(value, started.chars())); set(started, "output.mime_type", "application/json");
    }
    private static void fail(Started started, Throwable error) {
        Span span = started.span; AutoConfig config = started.config;
        String type = error.getClass().getName();
        span.setAttribute(AttributeKey.stringKey("error.type"), type);
        if (TraceAIAuto.hidesErrorText(config)) {
            span.setStatus(StatusCode.ERROR);
            span.addEvent("exception", io.opentelemetry.api.common.Attributes.of(AttributeKey.stringKey("exception.type"), type));
        } else {
            // The message of an exception whose getMessage the application overrides is never asked for (the class name stands in), the text
            // goes through the same replacement as any captured string, and the stack trace is not exported: it prints the message.
            String message = guardedMessage(started, error);
            span.setStatus(StatusCode.ERROR, message);
            span.addEvent("exception", io.opentelemetry.api.common.Attributes.of(
                AttributeKey.stringKey("exception.type"), type, AttributeKey.stringKey("exception.message"), message));
            span.setAttribute(AttributeKey.stringKey("error.message"), message);
        }
    }
    private static String guardedMessage(Started started, Throwable error) {
        String message = WireCore.exceptionMessage(error);
        try { return String.valueOf(CapturedValue.of(message, Math.min(started.chars(), 4096))); }
        catch (Throwable unavailable) { return error.getClass().getName(); }
    }

    private static void set(Started started, String key, String value) {
        if (value == null || TraceAIAuto.hidden(key, value, null, started.config)) return;
        started.span.setAttribute(key, value);
    }

    private static Meta meta(Method method) {
        for (Map.Entry<String, String> kind : KINDS.entrySet()) {
            for (Annotation annotation : method.getAnnotations()) {
                if (!annotation.annotationType().getName().equals(PACKAGE + kind.getKey())) continue;
                String name = text(annotation, "name"), description = text(annotation, "description");
                String type = kind.getValue();
                if (type == null) { type = text(annotation, "kind").trim().toUpperCase(Locale.ROOT); if (type.isEmpty()) type = "CHAIN"; }
                return new Meta(type, name.isBlank() ? method.getName() : name, description);
            }
        }
        return null;
    }
    private static String text(Annotation annotation, String member) {
        try {
            Method m = annotation.annotationType().getMethod(member);
            return m.invoke(annotation) instanceof String value ? value : "";
        } catch (ReflectiveOperationException | RuntimeException ignored) { return ""; }
    }
    private static boolean excluded(Parameter parameter) {
        for (Annotation annotation : parameter.getAnnotations()) if (annotation.annotationType().getName().equals(PACKAGE + "ExcludeFromSpan")) return true;
        return false;
    }
    private static String truncate(String value, int max) { return value.length() <= max ? value : value.substring(0, max); }

    /**
     * The JSON of a bounded view of the value, never longer than {@code limit} characters and always valid JSON: a cut JSON text
     * is not JSON, and the export boundary withholds a text it cannot read as such. The view is built with half the limit as its
     * text budget (the JSON syntax and escapes add to it); if the text is still too long the budget is halved until it fits.
     */
    static String render(Object value, int limit) {
        CapturedValue.Memo memo = new CapturedValue.Memo(); // application code runs once, however many times the text is cut
        for (long budget = Math.max(limit / 2, 32); ; budget /= 2) {
            String text;
            try { text = GSON.toJson(CapturedValue.of(value, budget, memo)); } catch (Throwable ignored) { return "\"<unserializable>\""; }
            if (text.length() <= limit) return text;
            if (budget <= 32) return "\"" + CapturedValue.CUT + "\"";
        }
    }
    /** The bounded JSON-safe view of an application value (see CapturedValue); never throws. */
    static Object json(Object value, int depth) { return CapturedValue.of(value, DEFAULT_CHARS); }
    private static final long DEFAULT_CHARS = MAX_CAPTURE;

    /** Reactor support without a compile-time dependency: a Mono or Flux is wrapped so the span ends when it terminates. */
    private static final class Reactor {
        static Object wrap(Started started, Object returned) throws ReflectiveOperationException {
            if (returned == null) return null;
            Class<?> mono = ancestor(returned.getClass(), "reactor.core.publisher.Mono");
            if (mono != null) {
                Object result = call(mono, returned, "doOnSuccess", Consumer.class, (Consumer<Object>) value -> finish(started, value, null));
                result = call(mono, result, "doOnError", Consumer.class, (Consumer<Throwable>) error -> finish(started, null, error));
                return call(mono, result, "doOnCancel", Runnable.class, (Runnable) () -> finish(started, null, null, true, false));
            }
            Class<?> flux = ancestor(returned.getClass(), "reactor.core.publisher.Flux");
            if (flux == null) return null;
            List<Object> items = Collections.synchronizedList(new ArrayList<>());
            Object result = call(flux, returned, "doOnNext", Consumer.class, (Consumer<Object>) item -> {
                try { if (items.size() < MAX_ITEMS && started.span.isRecording() && !TraceAIAuto.hidden("output.value", "v", null, started.config)) items.add(CapturedValue.of(item, Math.max(started.chars() / MAX_ITEMS, 256))); }
                catch (Throwable ignored) { } // capturing an item must never fail the application's stream
            });
            result = call(flux, result, "doOnComplete", Runnable.class, (Runnable) () -> finish(started, new ArrayList<>(items), null));
            result = call(flux, result, "doOnError", Consumer.class, (Consumer<Throwable>) error -> finish(started, null, error));
            return call(flux, result, "doOnCancel", Runnable.class, (Runnable) () -> { List<Object> seen = new ArrayList<>(items); finish(started, seen, null, true, !seen.isEmpty()); });
        }
        private static Class<?> ancestor(Class<?> type, String name) {
            for (Class<?> c = type; c != null; c = c.getSuperclass()) if (c.getName().equals(name)) return c;
            return null;
        }
        private static Object call(Class<?> owner, Object target, String name, Class<?> parameter, Object argument) throws ReflectiveOperationException {
            return owner.getMethod(name, parameter).invoke(target, argument);
        }
    }
    private AnnotationTracing() {}
}
