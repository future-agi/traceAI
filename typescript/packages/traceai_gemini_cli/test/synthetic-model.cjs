// Test fixture only: the documented BeforeModel synthetic response. This never ships in the tarball.
let raw = "";
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  JSON.parse(raw);
  process.stdout.write(
    JSON.stringify({
      decision: "deny",
      reason: "Synthetic lifecycle fixture; no network model call",
      hookSpecificOutput: {
        hookEventName: "BeforeModel",
        llm_response: {
          candidates: [
            {
              content: { role: "model", parts: ["SYNTHETIC_LIFECYCLE_OK"] },
              finishReason: "STOP",
            },
          ],
        },
      },
    }) + "\n",
  );
});
