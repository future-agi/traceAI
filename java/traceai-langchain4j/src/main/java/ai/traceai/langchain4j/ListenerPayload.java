package ai.traceai.langchain4j;

import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.model.chat.listener.ChatModelRequestContext;
import dev.langchain4j.model.chat.listener.ChatModelResponseContext;
import dev.langchain4j.model.output.FinishReason;
import dev.langchain4j.model.output.TokenUsage;

import java.lang.reflect.Method;
import java.util.List;

/**
 * The listener SPI is binary-compatible between 0.35 and 1.x, but its payload
 * accessors and types were renamed. Resolve only those public accessors once.
 * No dependency on removed ChatLanguageModel/ChatModelRequest types escapes here,
 * so the same published jar can serve both generations without loading the legacy wrapper.
 */
final class ListenerPayload {
    private static final Method REQUEST = accessor(ChatModelRequestContext.class, "chatRequest", "request");
    private static final Method RESPONSE = accessor(ChatModelResponseContext.class, "chatResponse", "response");
    private static final Method REQUEST_MODEL = accessor(REQUEST.getReturnType(), "modelName", "model");
    private static final Method MESSAGES = accessor(REQUEST.getReturnType(), "messages", "messages");
    private static final Method RESPONSE_MODEL = accessor(RESPONSE.getReturnType(), "modelName", "model");
    private static final Method RESPONSE_ID = accessor(RESPONSE.getReturnType(), "id", "id");
    private static final Method AI_MESSAGE = accessor(RESPONSE.getReturnType(), "aiMessage", "aiMessage");
    private static final Method USAGE = accessor(RESPONSE.getReturnType(), "tokenUsage", "tokenUsage");
    private static final Method FINISH_REASON = accessor(RESPONSE.getReturnType(), "finishReason", "finishReason");

    private ListenerPayload() {}

    static Object request(ChatModelRequestContext context) { return read(REQUEST, context); }
    static Object response(ChatModelResponseContext context) { return read(RESPONSE, context); }
    static String requestModel(Object request) { return (String) read(REQUEST_MODEL, request); }
    static String responseModel(Object response) { return (String) read(RESPONSE_MODEL, response); }
    static String responseId(Object response) { return (String) read(RESPONSE_ID, response); }
    @SuppressWarnings("unchecked")
    static List<ChatMessage> messages(Object request) { return (List<ChatMessage>) read(MESSAGES, request); }
    static AiMessage message(Object response) { return (AiMessage) read(AI_MESSAGE, response); }
    static TokenUsage usage(Object response) { return (TokenUsage) read(USAGE, response); }
    static FinishReason finishReason(Object response) { return (FinishReason) read(FINISH_REASON, response); }

    private static Method accessor(Class<?> type, String currentName, String legacyName) {
        try {
            try {
                return type.getMethod(currentName);
            } catch (NoSuchMethodException legacy) {
                return type.getMethod(legacyName);
            }
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException("Unsupported LangChain4j listener API", e);
        }
    }

    private static Object read(Method method, Object target) {
        try {
            return method.invoke(target);
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("Cannot read LangChain4j listener payload", e);
        }
    }
}
