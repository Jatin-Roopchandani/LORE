import "@fontsource/newsreader/400.css";
import "@fontsource/newsreader/600.css";
import "@fontsource/newsreader/400-italic.css";
import "@fontsource/public-sans/400.css";
import "@fontsource/public-sans/500.css";
import "@fontsource/public-sans/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "./theme.css";
import { StrictMode } from "react";
import { createRoot, hydrateRoot } from "react-dom/client";
import Router from "./Router";

// The public pages arrive pre-rendered (scripts/prerender.tsx): take over that HTML rather than redraw it.
const root = document.getElementById("root")!;
const app = <StrictMode><Router /></StrictMode>;
if (root.hasChildNodes()) hydrateRoot(root, app);
else createRoot(root).render(app);
