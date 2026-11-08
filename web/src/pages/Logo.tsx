// The LORE mark: an open book with a small constellation over it (the same drawing as favicon.svg). Shared by the
// homepage nav and the sign-in pages.
export function Logo({ size = 34 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" focusable="false">
      <path d="M32 56 C24 51 14 50 6 52 V30 C14 28 24 29 32 34 C40 29 50 28 58 30 V52 C50 50 40 51 32 56 Z" fill="none" stroke="#78AEEA" strokeWidth="3.4" strokeLinejoin="round" />
      <path d="M32 34 V56" stroke="#78AEEA" strokeWidth="3.4" />
      <path d="M32 30 L16 14 M32 30 L32 8 M32 30 L48 14 M16 14 L32 8 L48 14" fill="none" stroke="#78AEEA" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="32" cy="30" r="3.2" fill="#78AEEA" /><circle cx="16" cy="14" r="3.4" fill="#78AEEA" />
      <circle cx="32" cy="8" r="3.8" fill="#EBAA3C" /><circle cx="48" cy="14" r="3.4" fill="#78AEEA" />
    </svg>
  );
}
