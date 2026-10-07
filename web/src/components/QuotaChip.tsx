import type { Quota } from "../types";

export function QuotaChip({ quota, showInternals }: { quota: Quota | null; showInternals: boolean }) {
  if (!showInternals || !quota || quota.provider !== "gemini" || !quota.models.length) return null;
  const left = quota.models.reduce((a, m) => a + m.remaining, 0);
  const limit = quota.models.reduce((a, m) => a + m.limit, 0);
  const cls = left === 0 ? "quota danger" : left / limit < 0.1 ? "quota warn" : "quota";
  return <span className={cls} title="Gemini requests left today">{`Gemini: ${left} / ${limit} left`}</span>;
}
