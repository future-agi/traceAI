export * from "./typeUtils";
/**
 * Wraps a function with a try-catch block to catch and log any errors.
 * @param fn - A function to wrap with a try-catch block.
 * @returns A function that returns null if an error is thrown.
 */
export function withSafety({ fn, onError, }) {
    return (...args) => {
        try {
            return fn(...args);
        }
        catch (error) {
            if (onError) {
                onError(error);
            }
            return null;
        }
    };
}
export const safelyJSONStringify = withSafety({ fn: JSON.stringify });
export const safelyJSONParse = withSafety({ fn: JSON.parse });
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiaW5kZXguanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyJpbmRleC50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiQUFDQSxjQUFjLGFBQWEsQ0FBQztBQUU1Qjs7OztHQUlHO0FBQ0gsTUFBTSxVQUFVLFVBQVUsQ0FBNEIsRUFDcEQsRUFBRSxFQUNGLE9BQU8sR0FJUjtJQUNDLE9BQU8sQ0FBQyxHQUFHLElBQUksRUFBRSxFQUFFO1FBQ2pCLElBQUksQ0FBQztZQUNILE9BQU8sRUFBRSxDQUFDLEdBQUcsSUFBSSxDQUFDLENBQUM7UUFDckIsQ0FBQztRQUFDLE9BQU8sS0FBSyxFQUFFLENBQUM7WUFDZixJQUFJLE9BQU8sRUFBRSxDQUFDO2dCQUNaLE9BQU8sQ0FBQyxLQUFLLENBQUMsQ0FBQztZQUNqQixDQUFDO1lBQ0QsT0FBTyxJQUFJLENBQUM7UUFDZCxDQUFDO0lBQ0gsQ0FBQyxDQUFDO0FBQ0osQ0FBQztBQUVELE1BQU0sQ0FBQyxNQUFNLG1CQUFtQixHQUFHLFVBQVUsQ0FBQyxFQUFFLEVBQUUsRUFBRSxJQUFJLENBQUMsU0FBUyxFQUFFLENBQUMsQ0FBQztBQUV0RSxNQUFNLENBQUMsTUFBTSxlQUFlLEdBQUcsVUFBVSxDQUFDLEVBQUUsRUFBRSxFQUFFLElBQUksQ0FBQyxLQUFLLEVBQUUsQ0FBQyxDQUFDIiwic291cmNlc0NvbnRlbnQiOlsiaW1wb3J0IHsgR2VuZXJpY0Z1bmN0aW9uLCBTYWZlRnVuY3Rpb24gfSBmcm9tIFwiLi90eXBlc1wiO1xuZXhwb3J0ICogZnJvbSBcIi4vdHlwZVV0aWxzXCI7XG5cbi8qKlxuICogV3JhcHMgYSBmdW5jdGlvbiB3aXRoIGEgdHJ5LWNhdGNoIGJsb2NrIHRvIGNhdGNoIGFuZCBsb2cgYW55IGVycm9ycy5cbiAqIEBwYXJhbSBmbiAtIEEgZnVuY3Rpb24gdG8gd3JhcCB3aXRoIGEgdHJ5LWNhdGNoIGJsb2NrLlxuICogQHJldHVybnMgQSBmdW5jdGlvbiB0aGF0IHJldHVybnMgbnVsbCBpZiBhbiBlcnJvciBpcyB0aHJvd24uXG4gKi9cbmV4cG9ydCBmdW5jdGlvbiB3aXRoU2FmZXR5PFQgZXh0ZW5kcyBHZW5lcmljRnVuY3Rpb24+KHtcbiAgZm4sXG4gIG9uRXJyb3IsXG59OiB7XG4gIGZuOiBUO1xuICBvbkVycm9yPzogKGVycm9yOiB1bmtub3duKSA9PiB2b2lkO1xufSk6IFNhZmVGdW5jdGlvbjxUPiB7XG4gIHJldHVybiAoLi4uYXJncykgPT4ge1xuICAgIHRyeSB7XG4gICAgICByZXR1cm4gZm4oLi4uYXJncyk7XG4gICAgfSBjYXRjaCAoZXJyb3IpIHtcbiAgICAgIGlmIChvbkVycm9yKSB7XG4gICAgICAgIG9uRXJyb3IoZXJyb3IpO1xuICAgICAgfVxuICAgICAgcmV0dXJuIG51bGw7XG4gICAgfVxuICB9O1xufVxuXG5leHBvcnQgY29uc3Qgc2FmZWx5SlNPTlN0cmluZ2lmeSA9IHdpdGhTYWZldHkoeyBmbjogSlNPTi5zdHJpbmdpZnkgfSk7XG5cbmV4cG9ydCBjb25zdCBzYWZlbHlKU09OUGFyc2UgPSB3aXRoU2FmZXR5KHsgZm46IEpTT04ucGFyc2UgfSk7Il19