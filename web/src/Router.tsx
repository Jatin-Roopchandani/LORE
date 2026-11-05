import { lazy, Suspense } from "react";
import { Admin } from "./pages/Admin";
import { Legal } from "./pages/Legal";
import { PageShell } from "./pages/PageShell";
import { SignIn } from "./pages/SignIn";
import { Waitlist } from "./pages/Waitlist";
import { AUTH } from "./messages";
import { isMulti, pageFor } from "./routing";

// The reading app (and its graph library) is loaded only for /app, so the sign-in and public pages stay small.
const App = lazy(() => import("./App"));
// The homepage is a chunk of its own too, with its CSS and the two extra font weights.
const Home = lazy(() => import("./pages/home/Home"));

export default function Router({ pathname = window.location.pathname, multi = isMulti() }: { pathname?: string; multi?: boolean }) {
  switch (pageFor(pathname, multi)) {
    case "app": return <Suspense fallback={null}><App /></Suspense>;
    case "home": return <Suspense fallback={null}><Home /></Suspense>;
    case "signin": return <SignIn />;
    case "waitlist": return <Waitlist />;
    case "admin": return <Admin />;
    case "privacy": return <Legal kind="privacy" />;
    case "terms": return <Legal kind="terms" />;
    default: return <PageShell><p>{AUTH.notFound}</p><a href="/">{AUTH.back}</a></PageShell>;
  }
}
