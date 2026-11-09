/** Agent operations may not wait indefinitely on a broken exporter or stream decoder. */
export async function withDeadline(work: () => Promise<unknown>, milliseconds = 5000): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([Promise.resolve().then(work), new Promise<void>(resolve => {
      timer = setTimeout(resolve, milliseconds); timer.unref?.();
    })]);
  } finally { if (timer) clearTimeout(timer); }
}
