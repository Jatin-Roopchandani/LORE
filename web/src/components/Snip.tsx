import { useRef, useState } from "react";
import { SETTINGS } from "../messages";

// A command or config snippet with a Copy button. Where the clipboard is refused, the text is selected instead, so
// Ctrl+C still works. `label` names the snippet for screen readers ("Copy Claude Desktop").
export function Snip({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const copy = () => {
    const select = () => { const r = document.createRange(); r.selectNodeContents(ref.current!); const s = getSelection(); s?.removeAllRanges(); s?.addRange(r); };
    try { navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1400); }, select); } catch { select(); }
  };
  return (
    <div className="snip"><span ref={ref}>{text}</span>
      <button className="copy" type="button" aria-label={label ? `${SETTINGS.copy} ${label}` : undefined} onClick={copy}>
        {copied ? SETTINGS.copied : SETTINGS.copy}</button>
    </div>
  );
}
