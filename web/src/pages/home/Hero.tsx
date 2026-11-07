import type { MouseEvent } from "react";
import { HOME } from "../../messages";
import { goToForm } from "./session";
import { StoryGraph } from "../StoryGraph";

export function Hero({ signedIn, reduced }: { signedIn: boolean; reduced: boolean }) {
  const invite = (e: MouseEvent) => { e.preventDefault(); goToForm(reduced); };
  return (
    <header className="h-band"><div className="h-inner h-hero">
      <div>
        <span className="h-eyebrow">{HOME.eyebrow}</span>
        <h1>{HOME.headline}<br /><span>{HOME.headlineGlow}</span></h1>
        <p className="h-lede">{HOME.lede}</p>
        <div className="h-ctas">
          {signedIn
            ? <a className="h-btn is-primary" href="/app">{HOME.openLore}</a>
            : <a className="h-btn is-primary" href="#beta" onClick={invite}>{HOME.requestInvite}</a>}
          <a className="h-btn" href="#how">{HOME.seeHow}</a>
        </div>
      </div>
      <div className="h-hero-art"><StoryGraph label={HOME.heroGraphLabel} /></div>
      <a className="h-scroll-cue" href="#how"><i /> {HOME.scrollCue}</a>
    </div></header>
  );
}
