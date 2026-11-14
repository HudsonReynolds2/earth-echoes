/**
 * E6.3: the leaf fan's geometry (spec 9.2). Leaves never overlap at the pin
 * size, rings grow outward only when the inner one is full, the first leaf
 * sits at 12 o'clock, and the same count always gives the same fan, so a
 * refetch never reshuffles leaves under the cursor.
 */
import { describe, expect, it } from "vitest";

import { FIRST_RING, LEAF_SPACING, leafOffsets, ringCapacity } from "../src/components/map/leafFan";
import { LISTENER_SIZE } from "../src/components/map/markers";

function minimumGap(count: number): number {
  const points = leafOffsets(count);
  let min = Infinity;
  for (let a = 0; a < points.length; a += 1) {
    for (let b = a + 1; b < points.length; b += 1) {
      min = Math.min(min, Math.hypot(points[a].dx - points[b].dx, points[a].dy - points[b].dy));
    }
  }
  return min;
}

describe("leafOffsets", () => {
  it.each([1, 8, 50, 51])("places %i leaves with none overlapping a pin", (count) => {
    const points = leafOffsets(count);
    expect(points).toHaveLength(count);
    if (count > 1) expect(minimumGap(count)).toBeGreaterThanOrEqual(LISTENER_SIZE);
    // Every leaf clears the largest Pod square (44px) and its badge.
    for (const point of points)
      expect(Math.hypot(point.dx, point.dy)).toBeGreaterThanOrEqual(FIRST_RING - 0.01);
  });

  it("starts at 12 o'clock on the first ring", () => {
    expect(leafOffsets(1)[0]).toEqual({ dx: 0, dy: -FIRST_RING, ring: 0 });
  });

  it("fills the inner ring before starting the next, and rings grow outward", () => {
    const first = ringCapacity(0);
    const points = leafOffsets(first + 1);
    expect(points.slice(0, first).every((point) => point.ring === 0)).toBe(true);
    expect(points[first].ring).toBe(1);
    expect(Math.hypot(points[first].dx, points[first].dy)).toBeCloseTo(
      FIRST_RING + LEAF_SPACING,
      1,
    );
    expect(ringCapacity(1)).toBeGreaterThan(ringCapacity(0));
  });

  it("keeps every leaf on the upper half, clear of the Pod's label below it", () => {
    for (const point of leafOffsets(51)) expect(point.dy).toBeLessThanOrEqual(0);
    const full = leafOffsets(ringCapacity(0));
    expect(full[0].dx).toBeCloseTo(-FIRST_RING, 5); // 9 o'clock
    expect(full[full.length - 1].dx).toBeCloseTo(FIRST_RING, 5); // 3 o'clock
  });

  it("is deterministic", () => {
    expect(leafOffsets(37)).toEqual(leafOffsets(37));
  });
});
