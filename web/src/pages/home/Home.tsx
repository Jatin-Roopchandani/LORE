import "@fontsource/public-sans/700.css";
import "@fontsource/jetbrains-mono/500.css";
import "./home.css";
import { useEffect } from "react";
import { HOME } from "../../messages";
import { Beta } from "./Beta";
import { Footer, Nav } from "./Chrome";
import { Features } from "./Features";
import { Hero } from "./Hero";
import { HowScene } from "./HowScene";
import { useReducedMotion } from "./scroll";
import { goToForm, isSignedIn } from "./session";

// The public front door (multi mode's "/"), a port of docs/design/homepage.html. Loaded as its own chunk, so a
// visitor never downloads the reading app or its graph library.
export default function Home({ signedIn = isSignedIn() }: { signedIn?: boolean }) {
  const reduced = useReducedMotion();
  useEffect(() => { document.title = HOME.title; }, []);
  // Arriving from privacy or terms as /#how or /#beta: this page was drawn after the browser looked for the anchor.
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (id === "beta") goToForm(true); // at once: the CSS smooth scroll would still be travelling
    else if (id) document.getElementById(id)?.scrollIntoView?.({ behavior: "instant" });
  }, []);
  return (
    <div className="lh">
      <Nav onHome signedIn={signedIn} />
      <main>
        <div className="h-night" id="top">
          <Hero signedIn={signedIn} reduced={reduced} />
          <HowScene reduced={reduced} />
        </div>
        <Features reduced={reduced} />
        <Beta />
      </main>
      <Footer signedIn={signedIn} />
    </div>
  );
}
