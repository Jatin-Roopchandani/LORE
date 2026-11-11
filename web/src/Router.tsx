import { lazy, Suspense } from "react";
import { Admin } from "./pages/Admin";
import { Consent } from "./pages/Consent";
import { ContinueSignIn } from "./pages/ContinueSignIn";
import { PageShell } from "./pages/PageShell";
import { SignIn } from "./pages/SignIn";
import { Waitlist } from "./pages/Waitlist";
import { AUTH } from "./messages";
import { isMulti, pageFor } from "./routing";

// The reading app (and its graph library) is loaded only for /app, so the sign-in and public pages stay small.
const App = lazy(() => import("./App"));
// The homepage, privacy and terms share a chunk of their own, with their CSS and the two extra font weights.
const Home = lazy(() => import("./pages/home/Home"));
const Legal = lazy(() => import("./pages/home/Legal"));
// The blog, its posts and the FAQ: their own chunk, with the Markdown they render.
const Blog = lazy(() => import("./pages/blog/Blog"));

export default function Router({ pathname = window.location.pathname, multi = isMulti() }: { pathname?: string; multi?: boolean }) {
  switch (pageFor(pathname, multi)) {
    case "app": return <Suspense fallback={null}><App /></Suspense>;
    case "home": return <Suspense fallback={null}><Home /></Suspense>;
    case "signin": return <SignIn />;
    case "verify": return <ContinueSignIn />;
    case "consent": return <Consent />;
    case "waitlist": return <Waitlist />;
    case "admin": return <Admin />;
    case "privacy": return <Suspense fallback={null}><Legal kind="privacy" /></Suspense>;
    case "terms": return <Suspense fallback={null}><Legal kind="terms" /></Suspense>;
    case "blog": return <Suspense fallback={null}><Blog page={{ kind: "index" }} /></Suspense>;
    case "faq": return <Suspense fallback={null}><Blog page={{ kind: "faq" }} /></Suspense>;
    case "post": return <Suspense fallback={null}><Blog page={{ kind: "post", slug: pathname.replace(/\/+$/, "").slice("/blog/".length) }} /></Suspense>;
    default: return <PageShell><p>{AUTH.notFound}</p><a href="/">{AUTH.back}</a></PageShell>;
  }
}
