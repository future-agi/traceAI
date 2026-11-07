import { describe, expect, it } from "@jest/globals";
import { capBase64Images } from "../trace/trace-config/imageCap";
import { REDACTED_VALUE as MARK } from "../trace/trace-config/constants";

// Replacement ranges are merged through a per-block summary (1024 start positions
// per block). These controls pin the observable result at block edges: expected
// values are built literally from the layout, never by calling the implementation.
const CAP = 64;
const BLOCK = 1024;
const HEADER = "data:image/png;base64,";

/** An image URL of exactly `length` characters, header included. */
function url(length: number): string {
  return HEADER + "A".repeat(length - HEADER.length);
}

type Piece = { gap: number; length: number };

/** Ordinary text of `gap` characters, then an image; ! ends every payload chain. */
function layout(pieces: Piece[], tail: number): [string, string] {
  let raw = "", expected = "";
  for (const { gap, length } of pieces) {
    const filler = "x".repeat(Math.max(0, gap - 1)) + (gap > 0 ? " " : "");
    raw += filler + url(length) + "!";
    expected += filler + (length > CAP ? MARK : url(length)) + "!";
  }
  raw += "y".repeat(tail);
  expected += "y".repeat(tail);
  return [raw, expected];
}

/** Pad so the next image header begins exactly at `position` in the raw text. */
function startAt(position: number, before: string): number {
  return position - before.length;
}

describe("image replacement ranges at summary-block boundaries", () => {
  it.each([BLOCK - 1, BLOCK, BLOCK + 1, 2 * BLOCK - 1, 2 * BLOCK, 3 * BLOCK + 7])(
    "an over-cap image starting at %i is replaced whole and only once",
    (position) => {
      const before = "lead ";
      const pad = "z".repeat(startAt(position, before));
      const image = url(CAP + 300);
      const raw = before + pad + image + " after";
      expect(raw.indexOf(HEADER)).toBe(position);
      expect(capBase64Images(raw, CAP)).toBe(before + pad + MARK + " after");
    },
  );

  it("an at-cap image on a block edge survives (no universal drop)", () => {
    const pad = "z".repeat(BLOCK - 3);
    const raw = pad + url(CAP) + " end";
    expect(capBase64Images(raw, CAP)).toBe("[image-redacted]");
  });

  it("one image spanning many blocks and a separate later image keep separate markers", () => {
    const big = url(CAP + 9 * BLOCK + 13);
    const small = url(CAP + 1);
    const raw = "a " + big + " " + small + " z";
    expect(capBase64Images(raw, CAP)).toBe("a " + MARK + " " + MARK + " z");
  });

  it("adjacent images whose separator falls on a block boundary stay separate", () => {
    const first = url(2 * BLOCK - 3);
    const raw = "ab" + first + " " + url(CAP + 5) + " tail";
    // first occupies [2, 2*BLOCK - 1); the separator is at 2*BLOCK - 1 and the
    // second header begins on the aligned position 2*BLOCK.
    expect(raw.indexOf(HEADER, 3)).toBe(2 * BLOCK);
    expect(capBase64Images(raw, CAP)).toBe("ab" + MARK + " " + MARK + " tail");
  });

  it("many small over-cap images, one per block and across block edges, are each replaced", () => {
    const pieces: Piece[] = [];
    for (let n = 0; n < 40; n++) pieces.push({ gap: BLOCK - (CAP + 10) - 1 + (n % 3), length: CAP + 10 });
    const [raw, expected] = layout(pieces, 5);
    expect(capBase64Images(raw, CAP)).toBe(expected);
  });

  it("matches a literal model on seeded random layouts", () => {
    let seed = 0x5eed1234;
    const next = (limit: number): number => {
      seed = (Math.imul(seed, 1103515245) + 12345) >>> 0;
      return seed % limit;
    };
    for (let round = 0; round < 120; round++) {
      const pieces: Piece[] = [];
      const count = 1 + next(12);
      for (let n = 0; n < count; n++) {
        const length = HEADER.length + 1 + next(next(4) === 0 ? 4 * BLOCK : 2 * CAP);
        pieces.push({ gap: BLOCK + (next(3) === 0 ? 1 : next(2 * BLOCK)), length });
      }
      const [raw, expected] = layout(pieces, next(BLOCK));
      expect(capBase64Images(raw, CAP)).toBe(expected);
    }
  });
});
