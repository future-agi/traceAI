import { withDeadline } from "../src/deadline";
afterEach(() => jest.useRealTimers());
test("a stalled flush/shutdown cannot outlive its deadline", async () => {
  jest.useFakeTimers(); const operation = withDeadline(() => new Promise(() => {}), 5000);
  await jest.advanceTimersByTimeAsync(5000); await expect(operation).resolves.toBeUndefined(); expect(jest.getTimerCount()).toBe(0);
});
test("completed operations clear their deadline; errors remain catchable", async () => {
  jest.useFakeTimers(); await withDeadline(async () => {}); expect(jest.getTimerCount()).toBe(0);
  const error = new Error("exporter failed"); await expect(withDeadline(async () => { throw error; })).rejects.toBe(error); expect(jest.getTimerCount()).toBe(0);
});
