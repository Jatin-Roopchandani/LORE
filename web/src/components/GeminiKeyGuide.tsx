import { GEMINI_GUIDE, geminiKeyHint } from "../messages";

// Wherever a Gemini key is asked for: "How do I get a free Gemini key?", collapsed until opened, with the four steps
// and a short reassurance.
export function GeminiKeyGuide() {
  return (
    <details className="key-guide">
      <summary>{GEMINI_GUIDE.toggle}</summary>
      <ol aria-label={GEMINI_GUIDE.stepsLabel}>
        <li>
          <a href={GEMINI_GUIDE.url} target="_blank" rel="noopener noreferrer">{GEMINI_GUIDE.open}</a>
          <span className="visually-hidden"> {GEMINI_GUIDE.newTab}</span>{GEMINI_GUIDE.openRest}
        </li>
        <li>{GEMINI_GUIDE.create}</li>
        <li>{GEMINI_GUIDE.copy}</li>
        <li>{GEMINI_GUIDE.paste}</li>
      </ol>
      <p className="hint">{GEMINI_GUIDE.reassure}</p>
    </details>
  );
}

// Under a Gemini key field: a hint when the value doesn't look like a Gemini key. It never stops the save.
export function GeminiKeyHint({ value, id }: { value: string; id?: string }) {
  const hint = geminiKeyHint(value);
  return hint ? <p className="hint key-hint" id={id} role="status">{hint}</p> : null;
}
