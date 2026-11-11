import "@fontsource/public-sans/700.css";
import "@fontsource/jetbrains-mono/500.css";
import "../home/home.css";
import "./blog.css";
import { useEffect } from "react";
import { BLOG } from "../../messages";
import { Footer, Nav } from "../home/Chrome";
import { isSignedIn } from "../home/session";
import { FAQ, POSTS, longDate } from "./content";

export type BlogPage = { kind: "index" } | { kind: "post"; slug: string } | { kind: "faq" };

function Head({ title, lede, date }: { title: string; lede: string; date?: string }) {
  return (
    <div className="h-night">
      <div className="h-band h-legal-head">
        <h1>{title}</h1>
        <p className="h-lede">{lede}</p>
        {date && <p className="h-updated">{BLOG.byline} · <time dateTime={date}>{longDate(date)}</time></p>}
      </div>
    </div>
  );
}

function Body({ page }: { page: BlogPage }) {
  if (page.kind === "faq") {
    return (<>
      <Head title={FAQ.title} lede={FAQ.description} />
      <article className="h-band h-legal h-prose">
        {FAQ.items.map((item) => (
          <section key={item.q}><h2>{item.q}</h2><div dangerouslySetInnerHTML={{ __html: item.html }} /></section>
        ))}
      </article>
    </>);
  }
  if (page.kind === "post") {
    const post = POSTS.find((p) => p.slug === page.slug);
    if (!post) return (<><Head title={BLOG.notFound} lede="" /><p className="h-band h-legal"><a href="/blog">{BLOG.back}</a></p></>);
    return (<>
      <Head title={post.title} lede={post.description} date={post.date} />
      <article className="h-band h-legal h-prose" dangerouslySetInnerHTML={{ __html: post.html }} />
      <p className="h-band h-legal h-more"><a href="/blog">{BLOG.back}</a> · <a href="/faq">{BLOG.faqLink}</a></p>
    </>);
  }
  return (<>
    <Head title={BLOG.title} lede={BLOG.lede} />
    <div className="h-band h-legal">
      <ul className="h-posts">
        {POSTS.map((p) => (
          <li key={p.slug}>
            <a href={`/blog/${p.slug}`}><h2>{p.title}</h2></a>
            <p>{p.description}</p>
            <time dateTime={p.date}>{longDate(p.date)}</time>
          </li>
        ))}
      </ul>
      <p className="h-more"><a href="/faq">{BLOG.faqLink}</a></p>
    </div>
  </>);
}

const titleOf = (page: BlogPage) => page.kind === "faq" ? `${FAQ.title}`
  : page.kind === "post" ? `${POSTS.find((p) => p.slug === page.slug)?.title ?? BLOG.notFound} · LORE`
  : BLOG.docTitle;

// The blog, its posts and the FAQ: written pages in the homepage's style, pre-rendered for search engines.
export default function Blog({ page, signedIn = isSignedIn() }: { page: BlogPage; signedIn?: boolean }) {
  useEffect(() => { document.title = titleOf(page); }, [page]);
  return (
    <div className="lh">
      <Nav onHome={false} signedIn={signedIn} />
      <main><Body page={page} /></main>
      <Footer signedIn={signedIn} />
    </div>
  );
}
