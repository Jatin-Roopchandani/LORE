// Set the saved theme before first paint (no flash). A file, not an inline script, so the Content-Security-Policy
// can refuse every inline script (lore/web/headers.py). The theme blocks themselves are the themes-*.css stylesheet.
try { var t = localStorage.getItem("lore-theme-v2") || localStorage.getItem("lore-theme"); if (t && t !== "system") document.documentElement.setAttribute("data-theme", t); } catch (e) {}
