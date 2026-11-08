// Multi-user mode: the server answers `key_required` (no Gemini key saved yet) or `key_invalid` (Gemini refused it) to an ask
// or a Continue. Whoever listens (the app) sends the user to the key screen; the call still returns its error as usual.
// Its own module so callers outside api.ts (the upload) report through the same listeners.
export type KeyProblem = "key_required" | "key_invalid";
const keyListeners = new Set<(problem: KeyProblem) => void>();
export function onKeyProblem(listener: (problem: KeyProblem) => void): () => void {
  keyListeners.add(listener);
  return () => { keyListeners.delete(listener); };
}
export function reportKeyProblem(code: string | undefined): void {
  if (code === "key_required" || code === "key_invalid") keyListeners.forEach((l) => l(code));
}
