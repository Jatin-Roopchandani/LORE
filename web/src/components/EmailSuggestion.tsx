import { EMAIL_SUGGEST } from "../messages";
import { suggestEmail } from "../emailSuggest";

// Under an email field: "Did you mean …@gmail.com? Use it" when the domain looks like a slip of a common
// one. Nothing is blocked; the reader can ignore it.
export function EmailSuggestion({ email, onUse, className }: { email: string; onUse: (fixed: string) => void; className?: string }) {
  const fixed = suggestEmail(email);
  if (!fixed) return null;
  return (
    <p className={`email-suggest ${className ?? ""}`.trim()} role="status">
      {EMAIL_SUGGEST.didYouMean} <b>{fixed}</b>?{" "}
      <button type="button" className="email-suggest-use" onClick={() => onUse(fixed)}>{EMAIL_SUGGEST.use}</button>
    </p>
  );
}
