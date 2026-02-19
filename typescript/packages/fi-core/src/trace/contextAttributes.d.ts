import { Attributes, Context } from "@opentelemetry/api";
import { Metadata, PromptTemplate, Session, Tags, User } from "./types";
export declare const ContextAttributes: {
    readonly "llm.prompt_template.template": symbol;
    readonly "llm.prompt_template.variables": symbol;
    readonly "llm.prompt_template.version": symbol;
    readonly "session.id": symbol;
    readonly metadata: symbol;
    readonly "user.id": symbol;
    readonly "tag.tags": symbol;
    readonly attributes: symbol;
};
export declare function setPromptTemplate(context: Context, attributes: PromptTemplate): Context;
export declare function clearPromptTemplate(context: Context): Context;
export declare function getPromptTemplate(context: Context): Partial<PromptTemplate> | undefined;
export declare function setSession(context: Context, attributes: Session): Context;
export declare function clearSession(context: Context): Context;
/**
 * Retrieves the session ID from the given context.
 * @param context - The context object.
 * @returns {string | undefined} The session ID if it exists, otherwise undefined.
 */
export declare function getSession(context: Context): Session | undefined;
export declare function setMetadata(context: Context, attributes: Metadata): Context;
export declare function clearMetadata(context: Context): Context;
export declare function getMetadata(context: Context): Metadata | undefined;
export declare function setUser(context: Context, attributes: User): Context;
export declare function clearUser(context: Context): Context;
export declare function getUser(context: Context): User | undefined;
export declare function setTags(context: Context, attributes: Tags): Context;
export declare function clearTags(context: Context): Context;
export declare function getTags(context: Context): Tags | undefined;
export declare function setAttributes(context: Context, attributes: Attributes): Context;
export declare function clearAttributes(context: Context): Context;
export declare function getAttributes(context: Context): Attributes | undefined;
/**
 * Gets the FI attributes from the given context
 * @param context
 * @example span.setAttributes(getAttributesFromContext(context.active()));
 * @returns {Attributes} The FI attributes formatted as OpenTelemetry span attributes.
 */
export declare function getAttributesFromContext(context: Context): Attributes;
