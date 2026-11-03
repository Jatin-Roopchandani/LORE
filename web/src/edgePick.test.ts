import { describe, expect, it } from "vitest";
import { nearestEdge, pointSegmentDistance } from "./edgePick";

describe("near-miss edge picking", () => {
  it("measures distance to a segment, clamped to its ends", () => {
    expect(pointSegmentDistance(5, 3, 0, 0, 10, 0)).toBeCloseTo(3);
    expect(pointSegmentDistance(-4, 3, 0, 0, 10, 0)).toBeCloseTo(5);
  });
  it("picks the nearest edge within tolerance, or none", () => {
    const edges = [
      { id: "far", a: { x: 0, y: 20 }, b: { x: 100, y: 20 } },
      { id: "near", a: { x: 0, y: 0 }, b: { x: 100, y: 0 } },
    ];
    expect(nearestEdge(50, 6, edges)).toBe("near");
    expect(nearestEdge(50, 14, edges)).toBe("far");
    expect(nearestEdge(50, 40, edges)).toBeNull();
  });
});
