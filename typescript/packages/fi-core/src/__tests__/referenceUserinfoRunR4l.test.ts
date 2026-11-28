import { describe, expect, test } from "@jest/globals";
import { scrubText, scrubValue } from "../privacy-runtime";

// A registered value that starts in a URL's userinfo, runs over the host and a query value the URL catalog replaced, and on past
// it: the userinfo replacement alone cuts no occurrence, but the replaced query value does, so nothing of the value is left after
// that placeholder (the rest of the URL token goes with it).
const SECRET = "PLNTpw12@e.invalid/?token=PLNTval9&PLNTtail7";

describe("a registered value from the userinfo over a replaced query value", () => {
  test("a whole URL value", () => {
    const out = scrubValue("https://u:PLNTpw12@e.invalid/?token=PLNTval9&PLNTtail7&safe=1", [SECRET], "");
    for (const marker of ["PLNTpw12", "PLNTval9", "PLNTtail7"]) expect(out).not.toContain(marker);
    expect(out).toBe("https://<redacted>");
  });
  test("a URL in prose", () => {
    for (const out of [scrubValue("log https://u:PLNTpw12@e.invalid/?token=PLNTval9&PLNTtail7&safe=1 next", [SECRET], ""),
      scrubText("log https://u:PLNTpw12@e.invalid/?token=PLNTval9&PLNTtail7&safe=1 next", [SECRET])]) {
      for (const marker of ["PLNTpw12", "PLNTval9", "PLNTtail7"]) expect(out).not.toContain(marker);
    }
  });
});
