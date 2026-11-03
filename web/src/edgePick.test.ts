import { describe, expect, it } from "vitest";
import { nearestEdge, nodeAt, pointSegmentDistance } from "./edgePick";

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

describe("node picking", () => {
  const nodes = [{ id: "a", x: 100, y: 100, r: 7, labelW: 80, labelH: 12 }, { id: "b", x: 100, y: 140, r: 7, labelW: 0, labelH: 12 }];
  it("hits the dot with a margin", () => {
    expect(nodeAt(110, 104, nodes)).toBe("a");
    expect(nodeAt(100, 160, nodes)).toBeNull();
  });
  it("hits the drawn label to the right of the dot", () => {
    expect(nodeAt(160, 101, nodes)).toBe("a");
    expect(nodeAt(160, 120, nodes)).toBeNull();
  });
  it("does not hit a label that is not drawn", () => {
    expect(nodeAt(160, 140, nodes)).toBeNull();
  });
});
