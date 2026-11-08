import "@fontsource/public-sans/700.css";
import "@fontsource/jetbrains-mono/500.css";
import "./home.css";
import { Fragment, useEffect } from "react";
import { LEGAL } from "../../messages";
import { Footer, Nav } from "./Chrome";
import { isSignedIn } from "./session";

// The contact address, wherever the copy mentions it, is a mail link.
function withContact(text: string) {
  const parts = text.split(LEGAL.contact);
  return parts.map((p, i) => (
    <Fragment key={i}>{p}{i < parts.length - 1 && <a href={`mailto:${LEGAL.contact}`}>{LEGAL.contact}</a>}</Fragment>
  ));
}

// Privacy and terms: short, plain pages in the homepage's style, with its nav and footer.
export default function Legal({ kind, signedIn = isSignedIn() }: { kind: "privacy" | "terms"; signedIn?: boolean }) {
  const page = LEGAL[kind];
  useEffect(() => { document.title = page.docTitle; }, [page]);
  return (
    <div className="lh">
      <Nav onHome={false} signedIn={signedIn} />
      <main>
        <div className="h-night">
          <div className="h-band h-legal-head">
            <h1>{page.title}</h1>
            <p className="h-lede">{page.lede}</p>
            <p className="h-updated">{LEGAL.updatedLabel} <time dateTime="2026-11-05">{LEGAL.updated}</time></p>
          </div>
        </div>
        <article className="h-band h-legal">
          {page.sections.map((s) => (
            <section key={s.h}>
              <h2>{s.h}</h2>
              {s.body.map((b, i) => Array.isArray(b)
                ? <ul key={i}>{b.map((li) => <li key={li}>{withContact(li)}</li>)}</ul>
                : <p key={i}>{withContact(b)}</p>)}
            </section>
          ))}
        </article>
      </main>
      <Footer signedIn={signedIn} />
    </div>
  );
}
