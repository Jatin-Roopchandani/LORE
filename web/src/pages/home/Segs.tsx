import { Fragment } from "react";
import type { Seg } from "../../messages";

// A run of copy with its inline marks (see Seg in messages.ts).
export function Segs({ parts }: { parts: Seg[] }) {
  return (
    <>
      {parts.map((s, i) => {
        if (typeof s === "string") return <Fragment key={i}>{s}</Fragment>;
        if ("cite" in s) return <span key={i} className="h-cite">{s.cite}</span>;
        if ("mark" in s) return <mark key={i}>{s.mark}</mark>;
        if ("b" in s) return <b key={i}>{s.b}</b>;
        if ("i" in s) return <i key={i}>{s.i}</i>;
        return <em key={i}>{s.em}</em>;
      })}
    </>
  );
}
