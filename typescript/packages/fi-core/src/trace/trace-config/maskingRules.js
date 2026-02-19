import { REDACTED_VALUE } from "./constants";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
/**
 * Masks (redacts) input text in LLM input messages.
 * Will mask information stored under the key `llm.input_messages.[i].message.content`.
 * @example
 * ```typescript
 *  maskInputTextRule.condition({
 *      config: {hideInputText: true},
 *      key: "llm.input_messages.[i].message.content"
 *  }) // returns true so the rule applies and the value will be redacted
 */
const maskInputTextRule = {
    condition: ({ config, key }) => config.hideInputText &&
        key.includes(SemanticConventions.LLM_INPUT_MESSAGES) &&
        key.includes(SemanticConventions.MESSAGE_CONTENT) &&
        !key.includes(SemanticConventions.MESSAGE_CONTENTS),
    action: () => REDACTED_VALUE,
};
/**
 * Masks (redacts) output text in LLM output messages.
 * Will mask information stored under the key `llm.output_messages.[i].message.content`.
 * @example
 * ```typescript
 *  maskOutputTextRule.condition({
 *      config: {hideOutputText: true},
 *      key: "llm.output_messages.[i].message.content"
 *  }) // returns true so the rule applies and the value will be redacted
 * ```
 */
const maskOutputTextRule = {
    condition: ({ config, key }) => config.hideOutputText &&
        key.includes(SemanticConventions.LLM_OUTPUT_MESSAGES) &&
        key.includes(SemanticConventions.MESSAGE_CONTENT) &&
        !key.includes(SemanticConventions.MESSAGE_CONTENTS),
    action: () => REDACTED_VALUE,
};
/**
 * Masks (redacts) input text content in LLM input messages.
 * Will mask information stored under the key `llm.input_messages.[i].message.contents.[j].message_content.text`.
 * @example
 * ```typescript
 *  maskOutputTextRule.condition({
 *      config: {hideInputText: true},
 *      key: "llm.input_messages.[i].message.contents.[j].message_content.text"
 *  }) // returns true so the rule applies and the value will be redacted
 */
const maskInputTextContentRule = {
    condition: ({ config, key }) => config.hideInputText &&
        key.includes(SemanticConventions.LLM_INPUT_MESSAGES) &&
        key.includes(SemanticConventions.MESSAGE_CONTENT_TEXT),
    action: () => REDACTED_VALUE,
};
/**
 * Masks (redacts) output text content in LLM output messages.
 * @example
 * ```typescript
 *  maskOutputTextRule.condition({
 *      config: {hideOutputText: true},
 *      key: "llm.output_messages.[i].message.contents.[j].message_content.text"
 *  }) // returns true so the rule applies and the value will be redacted
 */
const maskOutputTextContentRule = {
    condition: ({ config, key }) => config.hideOutputText &&
        key.includes(SemanticConventions.LLM_OUTPUT_MESSAGES) &&
        key.includes(SemanticConventions.MESSAGE_CONTENT_TEXT),
    action: () => REDACTED_VALUE,
};
/**
 * Masks (removes) input images in LLM input messages.
 * @example
 * ```typescript
 *  maskOutputTextRule.condition({
 *      config: {hideInputImages: true},
 *      key: "llm.input_messages.[i].message.contents.[j].message_content.image"
 *  }) // returns true so the rule applies and the value will be removed
 */
const maskInputImagesRule = {
    condition: ({ config, key }) => config.hideInputImages &&
        key.includes(SemanticConventions.LLM_INPUT_MESSAGES) &&
        key.includes(SemanticConventions.MESSAGE_CONTENT_IMAGE),
    action: () => undefined,
};
function isBase64Url(url) {
    return (typeof url === "string" &&
        url.startsWith("data:image/") &&
        url.includes("base64"));
}
/**
 * Masks (redacts) base64 images that are too long.
 *  * @example
 * ```typescript
 *  maskOutputTextRule.condition({
 *      config: {base64ImageMaxLength: 10},
 *      key: "llm.input_messages.[i].message.contents.[j].message_content.image.url",
 *      value: "data:image/base64,verylongbase64string"
 *  }) // returns true so the rule applies and the value will be redacted
 */
const maskLongBase64ImageRule = {
    condition: ({ config, key, value }) => typeof value === "string" &&
        isBase64Url(value) &&
        value.length > config.base64ImageMaxLength &&
        key.includes(SemanticConventions.LLM_INPUT_MESSAGES) &&
        key.includes(SemanticConventions.MESSAGE_CONTENT_IMAGE) &&
        key.endsWith(SemanticConventions.IMAGE_URL),
    action: () => REDACTED_VALUE,
};
/**
 * Masks (removes) embedding vectors.
 *  * @example
 * ```typescript
 *  maskOutputTextRule.condition({
 *      config: {hideEmbeddingVectors: true},
 *      key: "embedding.embeddings.[i].embedding.vector"
 *  }) // returns true so the rule applies and the value will be redacted
 */
const maskEmbeddingVectorsRule = {
    condition: ({ config, key }) => config.hideEmbeddingVectors &&
        key.includes(SemanticConventions.EMBEDDING_EMBEDDINGS) &&
        key.includes(SemanticConventions.EMBEDDING_VECTOR),
    action: () => undefined,
};
/**
 * A list of {@link MaskingRule}s that are applied to span attributes to either redact or remove sensitive information.
 * The order of these rules is important as it can ensure appropriate masking of information
 * Rules should go from more specific to more general so that things like `llm.input_messages.[i].message.content` are masked with {@link REDACTED_VALUE} before the more generic masking of `llm.input_messages` might happen with `undefined` might happen.
 */
const maskingRules = [
    {
        condition: ({ config, key }) => config.hideInputs && key === SemanticConventions.INPUT_VALUE,
        action: () => REDACTED_VALUE,
    },
    {
        condition: ({ config, key }) => config.hideInputs && key === SemanticConventions.INPUT_MIME_TYPE,
        action: () => undefined,
    },
    {
        condition: ({ config, key }) => config.hideOutputs && key === SemanticConventions.OUTPUT_VALUE,
        action: () => REDACTED_VALUE,
    },
    {
        condition: ({ config, key }) => config.hideOutputs && key === SemanticConventions.OUTPUT_MIME_TYPE,
        action: () => undefined,
    },
    {
        condition: ({ config, key }) => (config.hideInputs || config.hideInputMessages) &&
            key.includes(SemanticConventions.LLM_INPUT_MESSAGES),
        action: () => undefined,
    },
    {
        condition: ({ config, key }) => (config.hideOutputs || config.hideOutputMessages) &&
            key.includes(SemanticConventions.LLM_OUTPUT_MESSAGES),
        action: () => undefined,
    },
    maskInputTextRule,
    maskOutputTextRule,
    maskInputTextContentRule,
    maskOutputTextContentRule,
    maskInputImagesRule,
    maskLongBase64ImageRule,
    maskEmbeddingVectorsRule,
];
/**
 * A function that masks (redacts or removes) sensitive information from span attributes based on the trace config.
 * @param config The {@link TraceConfig} to use to determine if the value should be masked
 * @param key The key of the attribute to mask
 * @param value The value of the attribute to mask
 * @returns The redacted value or undefined if the value should be masked, otherwise the original value
 */
export function mask({ config, key, value, }) {
    for (const rule of maskingRules) {
        if (rule.condition({ config, key, value })) {
            return rule.action();
        }
    }
    return value;
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoibWFza2luZ1J1bGVzLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsibWFza2luZ1J1bGVzLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiJBQUNBLE9BQU8sRUFBRSxjQUFjLEVBQUUsTUFBTSxhQUFhLENBQUM7QUFDN0MsT0FBTyxFQUFFLG1CQUFtQixFQUFFLE1BQU0sa0NBQWtDLENBQUM7QUFHdkU7Ozs7Ozs7OztHQVNHO0FBQ0gsTUFBTSxpQkFBaUIsR0FBZ0I7SUFDckMsU0FBUyxFQUFFLENBQUMsRUFBRSxNQUFNLEVBQUUsR0FBRyxFQUFFLEVBQUUsRUFBRSxDQUM3QixNQUFNLENBQUMsYUFBYTtRQUNwQixHQUFHLENBQUMsUUFBUSxDQUFDLG1CQUFtQixDQUFDLGtCQUFrQixDQUFDO1FBQ3BELEdBQUcsQ0FBQyxRQUFRLENBQUMsbUJBQW1CLENBQUMsZUFBZSxDQUFDO1FBQ2pELENBQUMsR0FBRyxDQUFDLFFBQVEsQ0FBQyxtQkFBbUIsQ0FBQyxnQkFBZ0IsQ0FBQztJQUNyRCxNQUFNLEVBQUUsR0FBRyxFQUFFLENBQUMsY0FBYztDQUM3QixDQUFDO0FBRUY7Ozs7Ozs7Ozs7R0FVRztBQUNILE1BQU0sa0JBQWtCLEdBQWdCO0lBQ3RDLFNBQVMsRUFBRSxDQUFDLEVBQUUsTUFBTSxFQUFFLEdBQUcsRUFBRSxFQUFFLEVBQUUsQ0FDN0IsTUFBTSxDQUFDLGNBQWM7UUFDckIsR0FBRyxDQUFDLFFBQVEsQ0FBQyxtQkFBbUIsQ0FBQyxtQkFBbUIsQ0FBQztRQUNyRCxHQUFHLENBQUMsUUFBUSxDQUFDLG1CQUFtQixDQUFDLGVBQWUsQ0FBQztRQUNqRCxDQUFDLEdBQUcsQ0FBQyxRQUFRLENBQUMsbUJBQW1CLENBQUMsZ0JBQWdCLENBQUM7SUFDckQsTUFBTSxFQUFFLEdBQUcsRUFBRSxDQUFDLGNBQWM7Q0FDN0IsQ0FBQztBQUVGOzs7Ozs7Ozs7R0FTRztBQUNILE1BQU0sd0JBQXdCLEdBQWdCO0lBQzVDLFNBQVMsRUFBRSxDQUFDLEVBQUUsTUFB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Q3pCLENBQUM7QUFFRjs7Ozs7O0dBTUc7QUFDSCxNQUFNLFVBQVUsSUFBSSxDQUFDLEVBQ25CLE1BQU0sRUFDTixHQUFHLEVBQ0gsS0FBSyxHQUNXO0lBQ2hCLEtBQUssTUFBTSxJQUFJLElBQUksWUFBWSxFQUFFLENBQUM7UUFDaEMsSUFBSSxJQUFJLENBQUMsU0FBUyxDQUFDLEVBQUUsTUFBTSxFQUFFLEdBQUcsRUFBRSxLQUFLLEVBQUUsQ0FBQyxFQUFFLENBQUM7WUFDM0MsT0FBTyxJQUFJLENBQUMsTUFBTSxFQUFFLENBQUM7UUFDdkIsQ0FBQztJQUNILENBQUM7SUFDRCxPQUFPLEtBQUssQ0FBQztBQUNmLENBQUMiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgeyBBdHRyaWJ1dGVWYWx1ZSB9IGZyb20gXCJAb3BlbnRlbGVtZXRyeS9hcGlcIjtcbmltcG9ydCB7IFJFREFDVEVEX1ZBTFVFIH0gZnJvbSBcIi4vY29uc3RhbnRzXCI7XG5pbXBvcnQgeyBTZW1hbnRpY0NvbnZlbnRpb25zIH0gZnJvbSBcIkB0cmFjZWFpL2ZpLXNlbWFudGljLWNvbnZlbnRpb25zXCI7XG5pbXBvcnQgeyBNYXNraW5nUnVsZSwgTWFza2luZ1J1bGVBcmdzIH0gZnJvbSBcIi4vdHlwZXNcIjtcblxuLyoqXG4gKiBNYXNrcyAocmVkYWN0cykgaW5wdXQgdGV4dCBpbiBMTE0gaW5wdXQgbWVzc2FnZXMuXG4gKiBXaWxsIG1hc2sgaW5mb3JtYXRpb24gc3RvcmVkIHVuZGVyIHRoZSBrZXkgYGxsbS5pbnB1dF9tZXNzYWdlcy5baV0ubWVzc2FnZS5jb250ZW50YC5cbiAqIEBleGFtcGxlXG4gKiBgYGB0eXBlc2NyaXB0XG4gKiAgbWFza0lucHV0VGV4dFJ1bGUuY29uZGl0aW9uKHtcbiAqICAgICAgY29uZmlnOiB7aGlkZUlucHV0VGV4dDogdHJ1ZX0sXG4gKiAgICAgIGtleTogXCJsbG0uaW5wdXRfbWVzc2FnZXMuW2ldLm1lc3NhZ2UuY29udGVudFwiXG4gKiAgfSkgLy8gcmV0dXJucyB0cnVlIHNvIHRoZSBydWxlIGFwcGxpZXMgYW5kIHRoZSB2YWx1ZSB3aWxsIGJlIHJlZGFjdGVkXG4gKi9cbmNvbnN0IG1hc2tJbnB1dFRleHRSdWxlOiBNYXNraW5nUnVsZSA9IHtcbiAgY29uZGl0aW9uOiAoeyBjb25maWcsIGtleSB9KSA9PlxuICAgIGNvbmZpZy5oaWRlSW5wdXRUZXh0ICYmXG4gICAga2V5LmluY2x1ZGVzKFNlbWFudGljQ29udmVudGlvbnMuTExNX0lOUFVUX01FU1NBR0VTKSAmJlxuICAgIGtleS5pbmNsdWRlcyhTZW1hbnRpY0NvbnZlbnRpb25zLk1FU1NBR0VfQ09OVEVOVCkgJiZcbiAgICAha2V5LmluY2x1ZGVzKFNlbWFudGljQ29udmVudGlvbnMuTUVTU0FHRV9DT05URU5UUyksXG4gIGFjdGlvbjogKCkgPT4gUkVEQUNURURfVkFMVUUsXG59O1xuXG4vKipcbiAqIE1hc2tzIChyZWRhY3RzKSBvdXRwdXQgdGV4dCBpbiBMTE0gb3V0cHV0IG1lc3NhZ2VzLlxuICogV2lsbCBtYXNrIGluZm9ybWF0aW9uIHN0b3JlZCB1bmRlciB0aGUga2V5IGBsbG0ub3V0cHV0X21lc3NhZ2VzLltpXS5tZXNzYWdlLmNvbnRlbnRgLlxuICogQGV4YW1wbGVcbiAqIGBgYHR5cGVzY3JpcHRcbiAqICBtYXNrT3V0cHV0VGV4dFJ1bGUuY29uZGl0aW9uKHtcbiAqICAgICAgY29uZmlnOiB7aGlkZU91dHB1dFRleHQ6IHRydWV9LFxuICogICAgICBrZXk6IFwibGxtLm91dHB1dF9tZXNzYWdlcy5baV0ubWVzc2FnZS5jb250ZW50XCJcbiAqICB9KSAvLyByZXR1cm5zIHRydWUgc28gdGhlIHJ1bGUgYXBwbGllcyBhbmQgdGhlIHZhbHVlIHdpbGwgYmUgcmVkYWN0ZWRcbiAqIGBgYFxuICovXG5jb25zdCBtYXNrT3V0cHV0VGV4dFJ1bGU6IE1hc2tpbmdSdWxlID0ge1xuICBjb25kaXRpb246ICh7IGNvbmZpZywga2V5IH0pID0+XG4gICAgY29uZmlnLmhpZGVPdXRwdXRUZXh0ICYmXG4gICAga2V5LmluY2x1ZGVzKFNlbWFudGljQ29udmVudGlvbnMuTExNX09VVFBVVF9NRVNTQUdFUykgJiZcbiAgICBrZXkuaW5jbHVkZXMoU2VtYW50aWNDb252ZW50aW9ucy5NRVNTQUdFX0NPTlRFTlQpICYmXG4gICAgIWtleS5pbmNsdWRlcyhTZW1hbnRpY0NvbnZlbnRpb25zLk1FU1NBR0VfQ09OVEVOVFMpLFxuICBhY3Rpb246ICgpID0+IFJFREFDVEVEX1ZBTFVFLFxufTtcblxuLyoqXG4gKiBNYXNrcyAocmVkYWN0cykgaW5wdXQgdGV4dCBjb250ZW50IGluIExMTSBpbnB1dCBtZXNzYWdlcy5cbiAqIFdpbGwgbWFzayBpbmZvcm1hdGlvbiBzdG9yZWQgdW5kZXIgdGhlIGtleSBgbGxtLmlucHV0X21lc3NhZ2VzLltpXS5tZXNzYWdlLmNvbnRlbnRzLltqXS5tZXNzYWdlX2NvbnRlbnQudGV4dGAuXG4gKiBAZXhhbXBsZVxuICogYGBgdHlwZXNjcmlwdFxuICogIG1hc2tPdXRwdXRUZXh0UnVsZS5jb25kaXRpb24oe1xuICogICAgICBjb25maWc6IHtoaWRlSW5wdXRUZXh0OiB0cnVlfSxcbiAqICAgICAga2V5OiBcImxsbS5pbnB1dF9tZXNzYWdlcy5baV0ubWVzc2FnZS5jb250ZW50cy5bal0ubWVzc2FnZV9jb250ZW50LnRleHRcIlxuICogIH0pIC8vIHJldHVybnMgdHJ1ZSBzbyB0aGUgcnVsZSBhcHBsaWVzIGFuZCB0aGUgdmFsdWUgd2lsbCBiZSByZWRhY3RlZFxuICovXG5jb25zdCBtYXNrSW5wdXRUZXh0Q29udGVudFJ1bGU6IE1hc2tpbmdSdWxlID0ge1xuICBjb25kaXRpb246ICh7IGNvbmZpZywga2V5IH0pID0+XG4gICAgY29uZmlnLmhpZGVJbnB1dFRleHQgJiZcbiAgICBrZXkuaW5jbHVkZXMoU2VtYW50aWNDb252ZW50aW9ucy5MTE1fSU5QVVRfTUVTU0FHRVMpICYmXG4gICAga2V5LmluY2x1ZGVzKFNlbWFudGljQ29udmVudGlvbnMuTUVTU0FHRV9DT05URU5UX1RFWFQpLFxuICBhY3Rpb246ICgpID0+IFJFREFDVEVEX1ZBTFVFLFxufTtcblxuLyoqXG4gKiBNYXNrcyAocmVkYWN0cykgb3V0cHV0IHRleHQgY29udGVudCBpbiBMTE0gb3V0cHV0IG1lc3NhZ2VzLlxuICogQGV4YW1wbGVcbiAqIGBgYHR5cGVzY3JpcHRcbiAqICBtYXNrT3V0cHV0VGV4dFJ1bGUuY29uZGl0aW9uKHtcbiAqICAgICAgY29uZmlnOiB7aGlkZU91dHB1dFRleHQ6IHRydWV9LFxuICogICAgICBrZXk6IFwibGxtLm91dHB1dF9tZXNzYWdlcy5baV0ubWVzc2FnZS5jb250ZW50cy5bal0ubWVzc2FnZV9jb250ZW50LnRleHRcIlxuICogIH0pIC8vIHJldHVybnMgdHJ1ZSBzbyB0aGUgcnVsZSBhcHBsaWVzIGFuZCB0aGUgdmFsdWUgd2lsbCBiZSByZWRhY3RlZFxuICovXG5jb25zdCBtYXNrT3V0cHV0VGV4dENvbnRlbnRSdWxlOiBNYXNraW5nUnVsZSA9IHtcbiAgY29uZGl0aW9uOiAoeyBjb25maWcsIGtleSB9KSA9PlxuICAgIGNvbmZpZy5oaWRlT3V0cHV0VGV4dCAmJlxuICAgIGtleS5pbmNsdWRlcyhTZW1hbnRpY0NvbnZlbnRpb25zLkxMTV9PVVRQVVRfTUVTU0FHRVMpICYmXG4gICAga2V5LmluY2x1ZGVzKFNlbWFudGljQ29udmVudGlvbnMuTUVTU0FHRV9DT05URU5UX1RFWFQpLFxuICBhY3Rpb246ICgpID0+IFJFREFDVEVEX1ZBTFVFLFxufTtcblxuLyoqXG4gKiBNYXNrcyAocmVtb3ZlcykgaW5wdXQgaW1hZ2VzIGluIExMTSBpbnB1dCBtZXNzYWdlcy5cbiAqIEBleGFtcGxlXG4gKiBgYGB0eXBlc2NyaXB0XG4gKiAgbWFza091dHB1dFRleHRSdWxlLmNvbmRpdGlvbih7XG4gKiAgICAgIGNvbmZpZzoge2hpZGVJbnB1dEltYWdlczogdHJ1ZX0sXG4gKiAgICAgIGtleTogXCJsbG0uaW5wdXRfbWVzc2FnZXMuW2ldLm1lc3NhZ2UuY29udGVudHMuW2pdLm1lc3NhZ2VfY29udGVudC5pbWFnZVwiXG4gKiAgfSkgLy8gcmV0dXJucyB0cnVlIHNvIHRoZSBydWxlIGFwcGxpZXMgYW5kIHRoZSB2YWx1ZSB3aWxsIGJlIHJlbW92ZWRcbiAqL1xuY29uc3QgbWFza0lucHV0SW1hZ2VzUnVsZTogTWFza2luZ1J1bGUgPSB7XG4gIGNvbmRpdGlvbjogKHsgY29uZmlnLCBrZXkgfSkgPT5cbiAgICBjb25maWcuaGlkZUlucHV0SW1hZ2VzICYmXG4gICAga2V5LmluY2x1ZGVzKFNlbWFudGljQ29udmVudGlvbnMuTExNX0lOUFVUX01FU1NBR0VTKSAmJlxuICAgIGtleS5pbmNsdWRlcyhTZW1hbnRpY0NvbnZlbnRpb25zLk1FU1NBR0VfQ09OVEVOVF9JTUFHRSksXG4gIGFjdGlvbjogKCkgPT4gdW5kZWZpbmVkLFxufTtcblxuZnVuY3Rpb24gaXNCYXNlNjRVcmwodXJsPzogQXR0cmlidXRlVmFsdWUpOiBib29sZWFuIHtcbiAgcmV0dXJuIChcbiAgICB0eXBlb2YgdXJsID09PSBcInN0cmluZ1wiICYmXG4gICAgdXJsLnN0YXJ0c1dpdGgoXCJkYXRhOmltYWdlL1wiKSAmJlxuICAgIHVybC5pbmNsdWRlcyhcImJhc2U2NFwiKVxuICApO1xufVxuXG4vKipcbiAqIE1hc2tzIChyZWRhY3RzKSBiYXNlNjQgaW1hZ2VzIHRoYXQgYXJlIHRvbyBsb25nLlxuICogICogQGV4YW1wbGVcbiAqIGBgYHR5cGVzY3JpcHRcbiAqICBtYXNrT3V0cHV0VGV4dFJ1bGUuY29uZGl0aW9uKHtcbiAqICAgICAgY29uZmlnOiB7YmFzZTY0SW1hZ2VNYXhMZW5ndGg6IDEwfSxcbiAqICAgICAga2V5OiBcImxsbS5pbnB1dF9tZXNzYWdlcy5baV0ubWVzc2FnZS5jb250ZW50cy5bal0ubWVzc2FnZV9jb250ZW50LmltYWdlLnVybFwiLFxuICogICAgICB2YWx1ZTogXCJkYXRhOmltYWdlL2Jhc2U2NCx2ZXJ5bG9uZ2Jhc2U2NHN0cmluZ1wiXG4gKiAgfSkgLy8gcmV0dXJucyB0cnVlIHNvIHRoZSBydWxlIGFwcGxpZXMgYW5kIHRoZSB2YWx1ZSB3aWxsIGJlIHJlZGFjdGVkXG4gKi9cbmNvbnN0IG1hc2tMb25nQmFzZTY0SW1hZ2VSdWxlOiBNYXNraW5nUnVsZSA9IHtcbiAgY29uZGl0aW9uOiAoeyBjb25maWcsIGtleSwgdmFsdWUgfSkgPT5cbiAgICB0eXBlb2YgdmFsdWUgPT09IFwic3RyaW5nXCIgJiZcbiAgICBpc0Jhc2U2NFVybCh2YWx1ZSkgJiZcbiAgICB2YWx1ZS5sZW5ndGggPiBjb25maWcuYmFzZTY0SW1hZ2VNYXhMZW5ndGggJiZcbiAgICBrZXkuaW5jbHVkZXMoU2VtYW50aWNDb252ZW50aW9ucy5MTE1fSU5QVVRfTUVTU0FHRVMpICYmXG4gICAga2V5LmluY2x1ZGVzKFNlbWFudGljQ29udmVudGlvbnMuTUVTU0FHRV9DT05URU5UX0lNQUdFKSAmJlxuICAgIGtleS5lbmRzV2l0aChTZW1hbnRpY0NvbnZlbnRpb25zLklNQUdFX1VSTCksXG4gIGFjdGlvbjogKCkgPT4gUkVEQUNURURfVkFMVUUsXG59O1xuXG4vKipcbiAqIE1hc2tzIChyZW1vdmVzKSBlbWJlZGRpbmcgdmVjdG9ycy5cbiAqICAqIEBleGFtcGxlXG4gKiBgYGB0eXBlc2NyaXB0XG4gKiAgbWFza091dHB1dFRleHRSdWxlLmNvbmRpdGlvbih7XG4gKiAgICAgIGNvbmZpZzoge2hpZGVFbWJlZGRpbmdWZWN0b3JzOiB0cnVlfSxcbiAqICAgICAga2V5OiBcImVtYmVkZGluZy5lbWJlZGRpbmdzLltpXS5lbWJlZGRpbmcudmVjdG9yXCJcbiAqICB9KSAvLyByZXR1cm5zIHRydWUgc28gdGhlIHJ1bGUgYXBwbGllcyBhbmQgdGhlIHZhbHVlIHdpbGwgYmUgcmVkYWN0ZWRcbiAqL1xuY29uc3QgbWFza0VtYmVkZGluZ1ZlY3RvcnNSdWxlOiBNYXNraW5nUnVsZSA9IHtcbiAgY29uZGl0aW9uOiAoeyBjb25maWcsIGtleSB9KSA9PlxuICAgIGNvbmZpZy5oaWRlRW1iZWRkaW5nVmVjdG9ycyAmJlxuICAgIGtleS5pbmNsdWRlcyhTZW1hbnRpY0NvbnZlbnRpb25zLkVNQkVERElOR19FTUJFRERJTkdTKSAmJlxuICAgIGtleS5pbmNsdWRlcyhTZW1hbnRpY0NvbnZlbnRpb25zLkVNQkVERElOR19WRUNUT1IpLFxuICBhY3Rpb246ICgpID0+IHVuZGVmaW5lZCxcbn07XG5cbi8qKlxuICogQSBsaXN0IG9mIHtAbGluayBNYXNraW5nUnVsZX1zIHRoYXQgYXJlIGFwcGxpZWQgdG8gc3BhbiBhdHRyaWJ1dGVzIHRvIGVpdGhlciByZWRhY3Qgb3IgcmVtb3ZlIHNlbnNpdGl2ZSBpbmZvcm1hdGlvbi5cbiAqIFRoZSBvcmRlciBvZiB0aGVzZSBydWxlcyBpcyBpbXBvcnRhbnQgYXMgaXQgY2FuIGVuc3VyZSBhcHByb3ByaWF0ZSBtYXNraW5nIG9mIGluZm9ybWF0aW9uXG4gKiBSdWxlcyBzaG91bGQgZ28gZnJvbSBtb3JlIHNwZWNpZmljIHRvIG1vcmUgZ2VuZXJhbCBzbyB0aGF0IHRoaW5ncyBsaWtlIGBsbG0uaW5wdXRfbWVzc2FnZXMuW2ldLm1lc3NhZ2UuY29udGVudGAgYXJlIG1hc2tlZCB3aXRoIHtAbGluayBSRURBQ1RFRF9WQUxVRX0gYmVmb3JlIHRoZSBtb3JlIGdlbmVyaWMgbWFza2luZyBvZiBgbGxtLmlucHV0X21lc3NhZ2VzYCBtaWdodCBoYXBwZW4gd2l0aCBgdW5kZWZpbmVkYCBtaWdodCBoYXBwZW4uXG4gKi9cbmNvbnN0IG1hc2tpbmdSdWxlczogTWFza2luZ1J1bGVbXSA9IFtcbiAge1xuICAgIGNvbmRpdGlvbjogKHsgY29uZmlnLCBrZXkgfSkgPT5cbiAgICAgIGNvbmZpZy5oaWRlSW5wdXRzICYmIGtleSA9PT0gU2VtYW50aWNDb252ZW50aW9ucy5JTlBVVF9WQUxVRSxcbiAgICBhY3Rpb246ICgpID0+IFJFREFDVEVEX1ZBTFVFLFxuICB9LFxuICB7XG4gICAgY29uZGl0aW9uOiAoeyBjb25maWcsIGtleSB9KSA9PlxuICAgICAgY29uZmlnLmhpZGVJbnB1dHMgJiYga2V5ID09PSBTZW1hbnRpY0NvbnZlbnRpb25zLklOUFVUX01JTUVfVFlQRSxcbiAgICBhY3Rpb246ICgpID0+IHVuZGVmaW5lZCxcbiAgfSxcbiAge1xuICAgIGNvbmRpdGlvbjogKHsgY29uZmlnLCBrZXkgfSkgPT5cbiAgICAgIGNvbmZpZy5oaWRlT3V0cHV0cyAmJiBrZXkgPT09IFNlbWFudGljQ29udmVudGlvbnMuT1VUUFVUX1ZBTFVFLFxuICAgIGFjdGlvbjogKCkgPT4gUkVEQUNURURfVkFMVUUsXG4gIH0sXG4gIHtcbiAgICBjb25kaXRpb246ICh7IGNvbmZpZywga2V5IH0pID0+XG4gICAgICBjb25maWcuaGlkZU91dHB1dHMgJiYga2V5ID09PSBTZW1hbnRpY0NvbnZlbnRpb25zLk9VVFBVVF9NSU1FX1RZUEUsXG4gICAgYWN0aW9uOiAoKSA9PiB1bmRlZmluZWQsXG4gIH0sXG4gIHtcbiAgICBjb25kaXRpb246ICh7IGNvbmZpZywga2V5IH0pID0+XG4gICAgICAoY29uZmlnLmhpZGVJbnB1dHMgfHwgY29uZmlnLmhpZGVJbnB1dE1lc3NhZ2VzKSAmJlxuICAgICAga2V5LmluY2x1ZGVzKFNlbWFudGljQ29udmVudGlvbnMuTExNX0lOUFVUX01FU1NBR0VTKSxcbiAgICBhY3Rpb246ICgpID0+IHVuZGVmaW5lZCxcbiAgfSxcbiAge1xuICAgIGNvbmRpdGlvbjogKHsgY29uZmlnLCBrZXkgfSkgPT5cbiAgICAgIChjb25maWcuaGlkZU91dHB1dHMgfHwgY29uZmlnLmhpZGVPdXRwdXRNZXNzYWdlcykgJiZcbiAgICAgIGtleS5pbmNsdWRlcyhTZW1hbnRpY0NvbnZlbnRpb25zLkxMTV9PVVRQVVRfTUVTU0FHRVMpLFxuICAgIGFjdGlvbjogKCkgPT4gdW5kZWZpbmVkLFxuICB9LFxuICBtYXNrSW5wdXRUZXh0UnVsZSxcbiAgbWFza091dHB1dFRleHRSdWxlLFxuICBtYXNrSW5wdXRUZXh0Q29udGVudFJ1bGUsXG4gIG1hc2tPdXRwdXRUZXh0Q29udGVudFJ1bGUsXG4gIG1hc2tJbnB1dEltYWdlc1J1bGUsXG4gIG1hc2tMb25nQmFzZTY0SW1hZ2VSdWxlLFxuICBtYXNrRW1iZWRkaW5nVmVjdG9yc1J1bGUsXG5dO1xuXG4vKipcbiAqIEEgZnVuY3Rpb24gdGhhdCBtYXNrcyAocmVkYWN0cyBvciByZW1vdmVzKSBzZW5zaXRpdmUgaW5mb3JtYXRpb24gZnJvbSBzcGFuIGF0dHJpYnV0ZXMgYmFzZWQgb24gdGhlIHRyYWNlIGNvbmZpZy5cbiAqIEBwYXJhbSBjb25maWcgVGhlIHtAbGluayBUcmFjZUNvbmZpZ30gdG8gdXNlIHRvIGRldGVybWluZSBpZiB0aGUgdmFsdWUgc2hvdWxkIGJlIG1hc2tlZFxuICogQHBhcmFtIGtleSBUaGUga2V5IG9mIHRoZSBhdHRyaWJ1dGUgdG8gbWFza1xuICogQHBhcmFtIHZhbHVlIFRoZSB2YWx1ZSBvZiB0aGUgYXR0cmlidXRlIHRvIG1hc2tcbiAqIEByZXR1cm5zIFRoZSByZWRhY3RlZCB2YWx1ZSBvciB1bmRlZmluZWQgaWYgdGhlIHZhbHVlIHNob3VsZCBiZSBtYXNrZWQsIG90aGVyd2lzZSB0aGUgb3JpZ2luYWwgdmFsdWVcbiAqL1xuZXhwb3J0IGZ1bmN0aW9uIG1hc2soe1xuICBjb25maWcsXG4gIGtleSxcbiAgdmFsdWUsXG59OiBNYXNraW5nUnVsZUFyZ3MpOiBBdHRyaWJ1dGVWYWx1ZSB8IHVuZGVmaW5lZCB7XG4gIGZvciAoY29uc3QgcnVsZSBvZiBtYXNraW5nUnVsZXMpIHtcbiAgICBpZiAocnVsZS5jb25kaXRpb24oeyBjb25maWcsIGtleSwgdmFsdWUgfSkpIHtcbiAgICAgIHJldHVybiBydWxlLmFjdGlvbigpO1xuICAgIH1cbiAgfVxuICByZXR1cm4gdmFsdWU7XG59Il19