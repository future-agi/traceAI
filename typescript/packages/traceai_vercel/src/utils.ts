import {
    MimeType,
    FISpanKind,
    SemanticConventions,
  } from "@traceai/fi-semantic-conventions";
  import { Attributes, AttributeValue, diag } from "@opentelemetry/api";
  import {
    VercelSDKFunctionNameToSpanKindMap,
    AISemConvToFISemConvMap,
  } from "./constants";
  import {
    AISemanticConventions,
    AISemanticConventionsList,
  } from "./AISemanticConventions";
  import {
    FIIOConventionKey,
    FISemanticConventionKey,
    SpanFilter,
  } from "./types";
  import {
    assertUnreachable,
    isArrayOfObjects,
    isStringArray,
  } from "./typeUtils";
  import { isAttributeValue } from "@opentelemetry/core";
  import {
    safelyJSONParse,
    safelyJSONStringify,
    withSafety,
  } from "./jsonutils"
  import { ReadableSpan } from "@opentelemetry/sdk-trace-base";
  
  const onErrorCallback = (attributeType: string) => (error: unknown) => {
    diag.warn(
      `Unable to get FI ${attributeType} attributes from AI attributes falling back to null: ${error}`,
    );
  };
  
  /**
   *
   * @param operationName - the operation name of the span
   * Operation names are set on Vercel spans as under the operation.name attribute with the
   * @example ai.generateText.doGenerate <functionId>
   * @returns the Vercel function name from the operation name or undefined if not found
   */
  const getVercelFunctionNameFromOperationName = (
    operationName: string,
  ): string | undefined => {
    return operationName.split(" ")[0];
  };
  
  /**
   * Gets the FI span kind that corresponds to the Vercel operation name
   * @param attributes the attributes of the span
   * @returns the FI span kind associated with the attributes or null if not found
   */
  const getFISpanKindFromAttributes = (
    attributes: Attributes,
  ): FISpanKind | string | undefined => {
    // If the span kind is already set, just use it
    const existingFISpanKind =
      attributes[SemanticConventions.FI_SPAN_KIND];
    if (existingFISpanKind != null && typeof existingFISpanKind === "string") {
      return existingFISpanKind;
    }
    const maybeOperationName = attributes["operation.name"];
    if (maybeOperationName == null || typeof maybeOperationName !== "string") {
      return;
    }
    const maybeFunctionName =
      getVercelFunctionNameFromOperationName(maybeOperationName);
    if (maybeFunctionName == null) {
      return;
    }
    return VercelSDKFunctionNameToSpanKindMap.get(maybeFunctionName);
  };
  
  /**
   * {@link getFISpanKindFromAttributes} wrapped in {@link withSafety} which will return null if any error is thrown
   */
  const safelyGetFISpanKindFromAttributes = withSafety({
    fn: getFISpanKindFromAttributes,
    onError: onErrorCallback("span kind"),
  });
  
  /**
   * Takes the attributes from the span and accumulates the attributes that are prefixed with "ai.settings" to be used as the invocation parameters
   * @param attributes the initial attributes of the span
   * @returns the FI attributes associated with the invocation parameters
   */
  const getInvocationParamAttributes = (attributes: Attributes) => {
    const settingAttributeKeys = Object.keys(attributes).filter((key) =>
      key.startsWith(AISemanticConventions.SETTINGS),
    );
    if (settingAttributeKeys.length === 0) {
      return null;
    }
    const settingAttributes = settingAttributeKeys.reduce((acc, key) => {
      const keyParts = key.split(".");
      const paramKey = keyParts[keyParts.length - 1];
      acc[paramKey] = attributes[key];
      return acc;
    }, {} as Attributes);
  
    return {
      [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
        safelyJSONStringify(settingAttributes) ?? undefined,
    };
  };
  
  /**
   * {@link getInvocationParamAttributes} wrapped in {@link withSafety} which will return null if any error is thrown
   */
  const safelyGetInvocationParamAttributes = withSafety({
    fn: getInvocationParamAttributes,
    onError: onErrorCallback("invocation parameters"),
  });
  
  /**
   * Determines whether the value is a valid JSON string
   * @param value the value to check
   * @returns whether the value is a valid JSON string
   */
  const isValidJsonString = (value?: AttributeValue) => {
    if (typeof value !== "string") {
      return false;
    }
    const parsed = safelyJSONParse(value);
    return typeof parsed === "object" && parsed !== null;
  };
  
  /**
   * Gets the mime type of the attribute value
   * @param value the attribute value to check
   * @returns the mime type of the value
   */
  const getMimeTypeFromValue = (value?: AttributeValue) => {
    if (isValidJsonString(value)) {
      return MimeType.JSON;
    }
    return MimeType.TEXT;
  };
  
  /**
   * Gets FI attributes associated with the IO
   * @param object.attributeValue the IO attribute value set by Vercel
   * @param object.FISemanticConventionKey the corresponding FI semantic convention
   * @returns the FI attributes associated with the IO value
   */
  const getIOValueAttributes = ({
    attributeValue,
    FISemanticConventionKey,
  }: {
    attributeValue?: AttributeValue;
    FISemanticConventionKey: FISemanticConventionKey;
  }) => {
    const mimeTypeSemanticConvention =
      FISemanticConventionKey === SemanticConventions.INPUT_VALUE
        ? SemanticConventions.INPUT_MIME_TYPE
        : SemanticConventions.OUTPUT_MIME_TYPE;
    return {
      [FISemanticConventionKey]: attributeValue,
      [mimeTypeSemanticConvention]: getMimeTypeFromValue(attributeValue),
    };
  };
  
  /**
   * {@link getIOValueAttributes} wrapped in {@link withSafety} which will return null if any error is thrown
   */
  const safelyGetIOValueAttributes = withSafety({
    fn: getIOValueAttributes,
    onError: onErrorCallback("input / output"),
  });
  
  /**
   * Formats an embedding attribute value (i.e., embedding text or vector) into the expected format
   * Vercel embedding vector attributes are stringified arrays, however, the FI spec expects them to be un-stringified arrays
   * @param value the value to format (either embedding text or vector)
   * @returns the formatted value or the original value if it is not a string or cannot be parsed
   */
  const formatEmbeddingValue = (value: AttributeValue) => {
    if (typeof value !== "string") {
      return value;
    }
    const parsedValue = safelyJSONParse(value);
    if (isAttributeValue(parsedValue) && parsedValue !== null) {
      return parsedValue;
    }
    return value;
  };
  
  /**
   * Takes the Vercel embedding attribute value and the corresponding FI attribute key and returns the FI attributes associated with the embedding
   * The Vercel embedding attribute value can be a string or an array of strings
   * @param object the attribute value and the FISemanticConventionKey (either EMBEDDING_TEXT or EMBEDDING_VECTOR)
   * @returns the FI attributes associated with the embedding
   */
  const getEmbeddingAttributes = ({
    attributeValue,
    FISemanticConventionKey,
  }: {
    attributeValue?: AttributeValue;
    FISemanticConventionKey: FISemanticConventionKey;
  }) => {
    const EMBEDDING_PREFIX = SemanticConventions.EMBEDDING_EMBEDDINGS;
  
    if (typeof attributeValue === "string") {
      return {
        [`${EMBEDDING_PREFIX}.0.${FISemanticConventionKey}`]:
          formatEmbeddingValue(attributeValue),
      };
    }
    if (isStringArray(attributeValue)) {
      return attributeValue.reduce((acc: Attributes, embeddingValue, index) => {
        acc[
          `${EMBEDDING_PREFIX}.${index}.${FISemanticConventionKey}`
        ] = formatEmbeddingValue(embeddingValue);
        return acc;
      }, {});
    }
    return null;
  };
  
  /**
   * {@link getEmbeddingAttributes} wrapped in {@link withSafety} which will return null if any error is thrown
   */
  const safelyGetEmbeddingAttributes = withSafety({
    fn: getEmbeddingAttributes,
    onError: onErrorCallback("embedding"),
  });
  
  /**
   * Gets the input_messages FI attributes
   * @param promptMessages the attribute value of the Vercel prompt messages
   * @returns input_messages attributes
   */
  const getInputMessageAttributes = (promptMessages?: AttributeValue) => {
    if (typeof promptMessages !== "string") {
      return null;
    }

    const messages = safelyJSONParse(promptMessages);

    if (!isArrayOfObjects(messages)) {
      return null;
    }

    // Convert Vercel messages to JSON blob format
    const serialized = messages.map((message) => {
      const msg: Record<string, unknown> = {};
      if (typeof message.role === "string") msg.role = message.role;

      if (message.role === "tool") {
        const toolContent = Array.isArray(message.content)
          ? typeof message.content[0]?.result === "string"
            ? message.content[0].result
            : message.content[0]?.result
              ? JSON.stringify(message.content[0].result)
              : undefined
          : typeof message.content === "string"
            ? message.content
            : undefined;
        if (toolContent) msg.content = toolContent;
        const toolCallId = Array.isArray(message.content)
          ? message.content[0]?.toolCallId
          : message.toolCallId;
        if (typeof toolCallId === "string") msg.tool_call_id = toolCallId;
      } else if (isArrayOfObjects(message.content)) {
        // Extract text content and tool calls from multi-part content
        const textParts = message.content
          .filter((c: any) => typeof c.text === "string")
          .map((c: any) => c.text)
          .join("");
        if (textParts) msg.content = textParts;
        const toolCalls = message.content
          .filter((c: any) => c.toolName || c.toolCallId)
          .map((c: any) => ({
            id: c.toolCallId,
            function: {
              name: c.toolName,
              arguments: typeof c.args === "string" ? c.args : typeof c.args === "object" ? JSON.stringify(c.args) : undefined,
            },
          }));
        if (toolCalls.length > 0) msg.tool_calls = toolCalls;
      } else if (typeof message.content === "string") {
        msg.content = message.content;
      }
      return msg;
    });

    return {
      [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(serialized) ?? "[]",
    };
  };
  
  /**
   * {@link getInputMessageAttributes} wrapped in {@link withSafety} which will return null if any error is thrown
   */
  const safelyGetInputMessageAttributes = withSafety({
    fn: getInputMessageAttributes,
    onError: onErrorCallback("input message"),
  });
  
  /**
   * Gets the output_messages tool_call FI attributes
   * @param toolCalls the attribute value of the Vercel result.toolCalls
   * @returns output_messages tool_call attributes
   */
  const getToolCallMessageAttributes = (toolCalls?: AttributeValue) => {
    if (typeof toolCalls !== "string") {
      return null;
    }

    const parsedToolCalls = safelyJSONParse(toolCalls);

    if (!isArrayOfObjects(parsedToolCalls)) {
      return null;
    }

    // Convert to JSON blob format
    const msg: Record<string, unknown> = {
      role: "assistant",
      tool_calls: parsedToolCalls.map((toolCall) => ({
        function: {
          name: isAttributeValue(toolCall.toolName) ? toolCall.toolName : undefined,
          arguments: safelyJSONStringify(toolCall.args) ?? undefined,
        },
      })),
    };

    return {
      [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([msg]) ?? "[]",
    };
  };
  
  /**
   * {@link getToolCallMessageAttributes} wrapped in {@link withSafety} which will return null if any error is thrown
   */
  const safelyGetToolCallMessageAttributes = withSafety({
    fn: getToolCallMessageAttributes,
    onError: onErrorCallback("tool call"),
  });
  
  /**
   * Gets the FI metadata attributes
   * Both vercel and FI attach metadata attributes to spans in a flat structure
   * @example Vercel: ai.telemetry.metadata.<metadataKey>
   * @example FI: metadata.<metadataKey>
   * @param attributes the initial attributes of the span
   * @returns the FI metadata attributes
   */
  const getMetadataAttributes = (attributes: Attributes) => {
    const metadataAttributeKeys = Object.keys(attributes)
      .filter((key) => key.startsWith(AISemanticConventions.METADATA))
      .map((key) => ({ key: key.split(".")[3], value: attributes[key] }));
    if (metadataAttributeKeys.length === 0) {
      return null;
    }
    return metadataAttributeKeys.reduce((acc, { key, value }) => {
      return key != null
        ? {
            ...acc,
            [`${SemanticConventions.METADATA}.${key}`]: value,
          }
        : acc;
    }, {});
  };
  
  /**
   * {@link getMetadataAttributes} wrapped in {@link withSafety} which will return null if any error is thrown
   */
  const safelyGetMetadataAttributes = withSafety({
    fn: getMetadataAttributes,
    onError: onErrorCallback("metadata"),
  });
  
  /**
   * Gets the FI attributes associated with the span from the initial attributes
   * @param attributesWithSpanKind the initial attributes of the span and the FI span kind
   * @param attributesWithSpanKind.attributes the initial attributes of the span
   * @param attributesWithSpanKind.spanKind the FI span kind
   * @returns The FI attributes associated with the span
   */
  const getFIAttributes = (attributes: Attributes): Attributes => {
    const spanKind = safelyGetFISpanKindFromAttributes(attributes);
    const fiAttributes = {
      [SemanticConventions.FI_SPAN_KIND]: spanKind ?? undefined,
    };
    return AISemanticConventionsList.reduce(
      (fiAttributes: Attributes, convention) => {
        /**
         *  Both settings and metadata are not full attribute paths but prefixes
         * @example ai.settings.<paramName> or ai.metadata.<metadataKey>
         */
        if (
          !(convention in attributes) &&
          convention !== AISemanticConventions.SETTINGS &&
          convention !== AISemanticConventions.METADATA
        ) {
          return fiAttributes;
        }
  
        const fiKey = AISemConvToFISemConvMap[convention];
  
        switch (convention) {
          case AISemanticConventions.METADATA:
            return {
              ...fiAttributes,
              ...safelyGetMetadataAttributes(attributes),
            };
          case AISemanticConventions.TOKEN_COUNT_COMPLETION:
          case AISemanticConventions.TOKEN_COUNT_PROMPT:
            // Do not capture token counts for non LLM spans to avoid double token counts
            if (spanKind !== FISpanKind.LLM) {
              return fiAttributes;
            }
            return {
              ...fiAttributes,
              [fiKey]: attributes[convention],
            };
          case AISemanticConventions.TOOL_CALL_ID:
            return {
              ...fiAttributes,
              [fiKey]: attributes[convention],
            };
          case AISemanticConventions.TOOL_CALL_NAME:
            return {
              ...fiAttributes,
              [fiKey]: attributes[convention],
            };
          case AISemanticConventions.TOOL_CALL_ARGS: {
            let argsAttributes = {
              [fiKey]: attributes[convention],
            };
            // For tool spans, capture the arguments as input value
            if (spanKind === FISpanKind.TOOL) {
              argsAttributes = {
                ...argsAttributes,
                [SemanticConventions.INPUT_VALUE]: attributes[convention],
                [SemanticConventions.INPUT_MIME_TYPE]: getMimeTypeFromValue(
                  attributes[convention],
                ),
              };
            }
            return {
              ...fiAttributes,
              ...argsAttributes,
            };
          }
          case AISemanticConventions.TOOL_CALL_RESULT:
            // For tool spans, capture the result as output value, for non tool spans ignore
            if (spanKind !== FISpanKind.TOOL) {
              return fiAttributes;
            }
            return {
              ...fiAttributes,
              [fiKey]: attributes[convention],
              [SemanticConventions.OUTPUT_MIME_TYPE]: getMimeTypeFromValue(
                attributes[convention],
              ),
            };
          case AISemanticConventions.MODEL_ID: {
            const modelSemanticConvention =
              spanKind === FISpanKind.EMBEDDING
                ? SemanticConventions.EMBEDDING_MODEL_NAME
                : SemanticConventions.LLM_MODEL_NAME;
            return {
              ...fiAttributes,
              [modelSemanticConvention]: attributes[convention],
            };
          }
          case AISemanticConventions.SETTINGS:
            return {
              ...fiAttributes,
              ...safelyGetInvocationParamAttributes(attributes),
            };
          case AISemanticConventions.PROMPT:
          case AISemanticConventions.RESPONSE_OBJECT:
          case AISemanticConventions.RESPONSE_TEXT: {
            return {
              ...fiAttributes,
              ...safelyGetIOValueAttributes({
                attributeValue: attributes[convention],
                FISemanticConventionKey:
                  fiKey as FIIOConventionKey,
              }),
            };
          }
          case AISemanticConventions.RESPONSE_TOOL_CALLS:
            return {
              ...fiAttributes,
              ...safelyGetToolCallMessageAttributes(attributes[convention]),
            };
          case AISemanticConventions.PROMPT_MESSAGES:
            return {
              ...fiAttributes,
              ...safelyGetInputMessageAttributes(attributes[convention]),
            };
            break;
          case AISemanticConventions.EMBEDDING_TEXT:
          case AISemanticConventions.EMBEDDING_TEXTS:
          case AISemanticConventions.EMBEDDING_VECTOR:
          case AISemanticConventions.EMBEDDING_VECTORS:
            return {
              ...fiAttributes,
              ...safelyGetEmbeddingAttributes({
                attributeValue: attributes[convention],
                FISemanticConventionKey: fiKey,
              }),
            };
          default:
            return assertUnreachable(convention);
        }
      },
      fiAttributes,
    );
  };
  
  /**
   * {@link getFIAttributes} wrapped in {@link withSafety} which will return null if any error is thrown
   */
  export const safelyGetFIAttributes = withSafety({
    fn: getFIAttributes,
    onError: onErrorCallback(""),
  });
  
  export const isFISpan = (span: ReadableSpan) => {
    const maybeFISpanKind =
      span.attributes[SemanticConventions.FI_SPAN_KIND];
    return typeof maybeFISpanKind === "string";
  };
  
  /**
   * Determines whether a span should be exported based on configuration and the spans attributes.
   * @param span the spn to check for export eligibility.
   * @param spanFilter a filter to apply to a span before exporting. If it returns true for a given span, the span will be exported.
   * @returns true if the span should be exported, false otherwise.
   */
  export const shouldExportSpan = ({
    span,
    spanFilter,
  }: {
    span: ReadableSpan;
    spanFilter?: SpanFilter;
  }): boolean => {
    if (spanFilter == null) {
      return true;
    }
    return spanFilter(span);
  };
  
  /**
   * Adds FI attributes to a span based on the span's existing attributes.
   * @param span - The span to add FI attributes to.
   */
  export const addFIAttributesToSpan = (span: ReadableSpan): void => {
    const newAttributes = {
      ...safelyGetFIAttributes(span.attributes),
    };
  
    // newer versions of opentelemetry will not allow you to reassign
    // the attributes object, so you must edit it by keyname instead
    Object.entries(newAttributes).forEach(([key, value]) => {
      span.attributes[key] = value as AttributeValue;
    });
  };