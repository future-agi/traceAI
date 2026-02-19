import { DefaultTraceConfig, traceConfigMetadata } from "./constants";
import { assertUnreachable, withSafety } from "../../utils";
const safelyParseInt = withSafety({ fn: parseInt });
/**
 * Parses an option based on its type
 * The order of precedence is: optionValue > envValue > defaultValue
 * @param key - The key of the option.
 * @param optionMetadata - The {@link TraceConfigOptionMetadata} for the option which includes its type, default value, and environment variable key.
 *
 */
function parseOption({ optionValue, optionMetadata, }) {
    if (optionValue !== undefined) {
        return optionValue;
    }
    const envValue = process.env[optionMetadata.envKey];
    if (envValue !== undefined) {
        switch (optionMetadata.type) {
            case "number": {
                const maybeEnvNumber = safelyParseInt(envValue);
                return maybeEnvNumber != null && !isNaN(maybeEnvNumber)
                    ? maybeEnvNumber
                    : optionMetadata.default;
            }
            case "boolean":
                return envValue.toLowerCase() === "true";
            default:
                assertUnreachable(optionMetadata);
        }
    }
    return optionMetadata.default;
}
/**
 * Generates a full trace config object based on passed in options, environment variables, and default values.
 * The order of precedence is: optionValue > envValue > defaultValue
 * @param options - The user provided TraceConfigOptions.
 * @returns A full TraceConfig object with all options set to their final values.
 */
export function generateTraceConfig(options) {
    if (options == null) {
        return DefaultTraceConfig;
    }
    return Object.entries(traceConfigMetadata).reduce((config, [key, optionMetadata]) => {
        const TraceConfigKey = key;
        return {
            ...config,
            [TraceConfigKey]: parseOption({
                optionValue: options[TraceConfigKey],
                optionMetadata,
            }),
        };
    }, {});
}
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoidHJhY2VDb25maWcuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyJ0cmFjZUNvbmZpZy50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiQUFBQSxPQUFPLEVBQUUsa0JBQWtCLEVBQUUsbUJBQW1CLEVBQUUsTUFBTSxhQUFhLENBQUM7QUFFdEUsT0FBTyxFQUFFLGlCQUFpQixFQUFFLFVBQVUsRUFBRSxNQUFNLGFBQWEsQ0FBQztBQUU1RCxNQUFNLGNBQWMsR0FBRyxVQUFVLENBQUMsRUFBRSxFQUFFLEVBQUUsUUFBUSxFQUFFLENBQUMsQ0FBQztBQUlwRDs7Ozs7O0dBTUc7QUFDSCxTQUFTLFdBQVcsQ0FBQyxFQUNuQixXQUFXLEVBQ1gsY0FBYyxHQUlmO0lBQ0MsSUFBSSxXQUFXLEtBQUssU0FBUyxFQUFFLENBQUM7UUFDOUIsT0FBTyxXQUFXLENBQUM7SUFDckIsQ0FBQztJQUNELE1BQU0sUUFBUSxHQUFHLE9BQU8sQ0FBQyxHQUFHLENBQUMsY0FBYyxDQUFDLE1BQU0sQ0FBQyxDQUFDO0lBQ3BELElBQUksUUFBUSxLQUFLLFNBQVMsRUFBRSxDQUFDO1FBQzNCLFFBQVEsY0FBYyxDQUFDLElBQUksRUFBRSxDQUFDO1lBQzVCLEtBQUssUUFBUSxDQUFDLENBQUMsQ0FBQztnQkFDZCxNQUFNLGNBQWMsR0FBRyxjQUFjLENBQUMsUUFBUSxDQUFDLENBQUM7Z0JBQ2hELE9BQU8sY0FBYyxJQUFJLElBQUksSUFBSSxDQUFDLEtBQUssQ0FBQyxjQUFjLENBQUM7b0JBQ3JELENBQUMsQ0FBQyxjQUFjO29CQUNoQixDQUFDLENBQUMsY0FBYyxDQUFDLE9BQU8sQ0FBQztZQUM3QixDQUFDO1lBQ0QsS0FBSyxTQUFTO2dCQUNaLE9BQU8sUUFBUSxDQUFDLFdBQVcsRUFBRSxLQUFLLE1BQU0sQ0FBQztZQUMzQztnQkFDRSxpQkFBaUIsQ0FBQyxjQUFjLENBQUMsQ0FBQztRQUN0QyxDQUFDO0lBQ0gsQ0FBQztJQUVELE9BQU8sY0FBYyxDQUFDLE9BQU8sQ0FBQztBQUNoQyxDQUFDO0FBRUQ7Ozs7O0dBS0c7QUFDSCxNQUFNLFVBQVUsbUJBQW1CLENBQUMsT0FBNEI7SUFDOUQsSUFBSSxPQUFPLElBQUksSUFBSSxFQUFFLENBQUM7UUFDcEIsT0FBTyxrQkFBa0IsQ0FBQztJQUM1QixDQUFDO0lBQ0QsT0FBTyxNQUFNLENBQUMsT0FBTyxDQUFDLG1CQUFtQixDQUFDLENBQUMsTUFBTSxDQUMvQyxDQUFDLE1BQU0sRUFBRSxDQUFDLEdBQUcsRUFBRSxjQUFjLENBQUMsRUFBRSxFQUFFO1FBQ2hDLE1BQU0sY0FBYyxHQUFHLEdBQXFCLENBQUM7UUFDN0MsT0FBTztZQUNMLEdBQUcsTUFBTTtZQUNULENBQUMsY0FBYyxDQUFDLEVBQUUsV0FBVyxDQUFDO2dCQUM1QixXQUFXLEVBQUUsT0FBTyxDQUFDLGNBQWMsQ0FBQztnQkFDcEMsY0FBYzthQUNmLENBQUM7U0FDSCxDQUFDO0lBQ0osQ0FBQyxFQUNELEVBQWlCLENBQ2xCLENBQUM7QUFDSixDQUFDIiwic291cmNlc0NvbnRlbnQiOlsiaW1wb3J0IHsgRGVmYXVsdFRyYWNlQ29uZmlnLCB0cmFjZUNvbmZpZ01ldGFkYXRhIH0gZnJvbSBcIi4vY29uc3RhbnRzXCI7XG5pbXBvcnQgeyBUcmFjZUNvbmZpZ0tleSwgVHJhY2VDb25maWcsIFRyYWNlQ29uZmlnT3B0aW9ucyB9IGZyb20gXCIuL3R5cGVzXCI7XG5pbXBvcnQgeyBhc3NlcnRVbnJlYWNoYWJsZSwgd2l0aFNhZmV0eSB9IGZyb20gXCIuLi8uLi91dGlsc1wiO1xuXG5jb25zdCBzYWZlbHlQYXJzZUludCA9IHdpdGhTYWZldHkoeyBmbjogcGFyc2VJbnQgfSk7XG5cbnR5cGUgVHJhY2VDb25maWdPcHRpb25NZXRhZGF0YSA9ICh0eXBlb2YgdHJhY2VDb25maWdNZXRhZGF0YSlbVHJhY2VDb25maWdLZXldO1xuXG4vKipcbiAqIFBhcnNlcyBhbiBvcHRpb24gYmFzZWQgb24gaXRzIHR5cGVcbiAqIFRoZSBvcmRlciBvZiBwcmVjZWRlbmNlIGlzOiBvcHRpb25WYWx1ZSA+IGVudlZhbHVlID4gZGVmYXVsdFZhbHVlXG4gKiBAcGFyYW0ga2V5IC0gVGhlIGtleSBvZiB0aGUgb3B0aW9uLlxuICogQHBhcmFtIG9wdGlvbk1ldGFkYXRhIC0gVGhlIHtAbGluayBUcmFjZUNvbmZpZ09wdGlvbk1ldGFkYXRhfSBmb3IgdGhlIG9wdGlvbiB3aGljaCBpbmNsdWRlcyBpdHMgdHlwZSwgZGVmYXVsdCB2YWx1ZSwgYW5kIGVudmlyb25tZW50IHZhcmlhYmxlIGtleS5cbiAqXG4gKi9cbmZ1bmN0aW9uIHBhcnNlT3B0aW9uKHtcbiAgb3B0aW9uVmFsdWUsXG4gIG9wdGlvbk1ldGFkYXRhLFxufToge1xuICBvcHRpb25WYWx1ZT86IG51bWJlciB8IGJvb2xlYW47XG4gIG9wdGlvbk1ldGFkYXRhOiBUcmFjZUNvbmZpZ09wdGlvbk1ldGFkYXRhO1xufSkge1xuICBpZiAob3B0aW9uVmFsdWUgIT09IHVuZGVmaW5lZCkge1xuICAgIHJldHVybiBvcHRpb25WYWx1ZTtcbiAgfVxuICBjb25zdCBlbnZWYWx1ZSA9IHByb2Nlc3MuZW52W29wdGlvbk1ldGFkYXRhLmVudktleV07XG4gIGlmIChlbnZWYWx1ZSAhPT0gdW5kZWZpbmVkKSB7XG4gICAgc3dpdGNoIChvcHRpb25NZXRhZGF0YS50eXBlKSB7XG4gICAgICBjYXNlIFwibnVtYmVyXCI6IHtcbiAgICAgICAgY29uc3QgbWF5YmVFbnZOdW1iZXIgPSBzYWZlbHlQYXJzZUludChlbnZWYWx1ZSk7XG4gICAgICAgIHJldHVybiBtYXliZUVudk51bWJlciAhPSBudWxsICYmICFpc05hTihtYXliZUVudk51bWJlcilcbiAgICAgICAgICA/IG1heWJlRW52TnVtYmVyXG4gICAgICAgICAgOiBvcHRpb25NZXRhZGF0YS5kZWZhdWx0O1xuICAgICAgfVxuICAgICAgY2FzZSBcImJvb2xlYW5cIjpcbiAgICAgICAgcmV0dXJuIGVudlZhbHVlLnRvTG93ZXJDYXNlKCkgPT09IFwidHJ1ZVwiO1xuICAgICAgZGVmYXVsdDpcbiAgICAgICAgYXNzZXJ0VW5yZWFjaGFibGUob3B0aW9uTWV0YWRhdGEpO1xuICAgIH1cbiAgfVxuXG4gIHJldHVybiBvcHRpb25NZXRhZGF0YS5kZWZhdWx0O1xufVxuXG4vKipcbiAqIEdlbmVyYXRlcyBhIGZ1bGwgdHJhY2UgY29uZmlnIG9iamVjdCBiYXNlZCBvbiBwYXNzZWQgaW4gb3B0aW9ucywgZW52aXJvbm1lbnQgdmFyaWFibGVzLCBhbmQgZGVmYXVsdCB2YWx1ZXMuXG4gKiBUaGUgb3JkZXIgb2YgcHJlY2VkZW5jZSBpczogb3B0aW9uVmFsdWUgPiBlbnZWYWx1ZSA+IGRlZmF1bHRWYWx1ZVxuICogQHBhcmFtIG9wdGlvbnMgLSBUaGUgdXNlciBwcm92aWRlZCBUcmFjZUNvbmZpZ09wdGlvbnMuXG4gKiBAcmV0dXJucyBBIGZ1bGwgVHJhY2VDb25maWcgb2JqZWN0IHdpdGggYWxsIG9wdGlvbnMgc2V0IHRvIHRoZWlyIGZpbmFsIHZhbHVlcy5cbiAqL1xuZXhwb3J0IGZ1bmN0aW9uIGdlbmVyYXRlVHJhY2VDb25maWcob3B0aW9ucz86IFRyYWNlQ29uZmlnT3B0aW9ucyk6IFRyYWNlQ29uZmlnIHtcbiAgaWYgKG9wdGlvbnMgPT0gbnVsbCkge1xuICAgIHJldHVybiBEZWZhdWx0VHJhY2VDb25maWc7XG4gIH1cbiAgcmV0dXJuIE9iamVjdC5lbnRyaWVzKHRyYWNlQ29uZmlnTWV0YWRhdGEpLnJlZHVjZShcbiAgICAoY29uZmlnLCBba2V5LCBvcHRpb25NZXRhZGF0YV0pID0+IHtcbiAgICAgIGNvbnN0IFRyYWNlQ29uZmlnS2V5ID0ga2V5IGFzIFRyYWNlQ29uZmlnS2V5O1xuICAgICAgcmV0dXJuIHtcbiAgICAgICAgLi4uY29uZmlnLFxuICAgICAgICBbVHJhY2VDb25maWdLZXldOiBwYXJzZU9wdGlvbih7XG4gICAgICAgICAgb3B0aW9uVmFsdWU6IG9wdGlvbnNbVHJhY2VDb25maWdLZXldLFxuICAgICAgICAgIG9wdGlvbk1ldGFkYXRhLFxuICAgICAgICB9KSxcbiAgICAgIH07XG4gICAgfSxcbiAgICB7fSBhcyBUcmFjZUNvbmZpZyxcbiAgKTtcbn0iXX0=