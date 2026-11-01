import { useId } from "react";
import { citationSource, type AskState } from "../askReducer";
import { DEEP } from "../messages";
import { Prose } from "./Prose";

// An interpretive answer opens with this: one sentence per interpretive part (its reading, or the line saying the
// evidence found no clear position). It is shown as soon as it is sent, above the argument that follows.
export function ShortAnswer({ state, onHoverChunks }: { state: AskState; onHoverChunks: (chunks: string[]) => void }) {
  const id = useId();
  const prose = state.shortAnswer ?? state.result?.short_answer ?? [];
  if (!prose.length) return null;
  return (
    <section className="short-answer" aria-labelledby={id}>
      <h3 id={id}>{DEEP.shortAnswer}</h3>
      <Prose result={citationSource(state)} prose={prose} showMissing={false} labels={false} onHoverChunks={onHoverChunks} />
    </section>
  );
}
