import { ReactNode, useEffect, useRef, useState } from "react";
import { HOME } from "../../messages";
import { onScrollFrame, useEased, usePinProgress } from "./scroll";
import { Segs } from "./Segs";

// Line icons, 24 x 24: a book with a check, a bulb, a graph, an eye struck through, a lock.
const svg = (children: ReactNode) => (
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
);
const ICONS = [
  svg(<><path d="M3 5.5c2.6-1 5.4-1 8 .8v13c-2.6-1.8-5.4-1.8-8-.8z" /><path d="M21 5.5c-2.6-1-5.4-1-8 .8v13c2.6-1.8 5.4-1.8 8-.8z" /><path d="M15 12.2l1.6 1.6 3-3.3" /></>),
  svg(<><path d="M9 18h6" /><path d="M10 21h4" /><path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.4 1.1 2.2h5c0-.8.4-1.6 1.1-2.2A6 6 0 0 0 12 3z" /><path d="M12 7.5v3" /><path d="M10.5 9h3" /></>),
  svg(<><circle cx="6" cy="7" r="2.4" /><circle cx="18" cy="6" r="2.4" /><circle cx="13" cy="18" r="2.4" /><path d="M8.3 7.8l7.4-1.3" /><path d="M7.3 9.1l4.4 6.8" /><path d="M17 8.2l-3.1 7.6" /></>),
  svg(<><path d="M2.5 12s3.5-6.5 9.5-6.5c2 0 3.7.7 5.1 1.7" /><path d="M21.5 12s-3.5 6.5-9.5 6.5c-2 0-3.7-.7-5.1-1.7" /><path d="M9.6 14.4a3.2 3.2 0 0 1 4.6-4.6" /><path d="M3 21L21 3" /></>),
  svg(<><rect x="4.5" y="10.5" width="15" height="10" rx="2.2" /><path d="M8 10.5V7.6a4 4 0 0 1 8 0v2.9" /><path d="M12 14.6v2.2" /></>),
];

// A little hold at each end of the scene, so the first and last cards are seen at rest.
export const glideAt = (progress: number) => Math.min(1, Math.max(0, (progress - 0.08) / 0.84));

// How far the track must slide for its last card to end at the right gutter. Measured from the last card rather than
// scrollWidth, which leaves out a flex row's end padding in some browsers; both rects carry the same transform.
export function trackOverflow(track: HTMLElement): number {
  const last = track.lastElementChild;
  if (!last) return 0;
  const pad = parseFloat(getComputedStyle(track).paddingRight) || 0;
  const content = last.getBoundingClientRect().right - track.getBoundingClientRect().left + pad;
  return Math.max(0, content - (track.clientWidth || window.innerWidth));
}

// "Not another chatbot": the heading holds still while the five cards glide across. The track slides left by
// exactly its overflow, so the last card ends at the right gutter. With reduced motion nothing slides: the cards
// sit in a wrapped grid (home.css).
export function Features({ reduced }: { reduced: boolean }) {
  const scene = useRef<HTMLElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const progress = usePinProgress(scene);
  const [over, setOver] = useState(0);
  useEffect(() => {
    const measure = () => { if (track.current) setOver(trackOverflow(track.current)); };
    measure();
    return onScrollFrame(measure);
  }, []);
  // The scroll picks where the track should be; the track eases there, so wheel steps glide instead of jumping.
  const k = useEased(reduced ? 0 : glideAt(progress), reduced, 0.14, 0.0002);
  return (
    <section className="h-scene h-glide" id="why" aria-labelledby="why-title" ref={scene}>
      <div className="h-scene-view">
        <div className="h-band h-glide-head">
          <h2 className="h-section-title" id="why-title">{HOME.whyTitle}</h2>
          <p className="h-section-sub">{HOME.whySub}</p>
        </div>
        <div className="h-glide-rail">
          <div className="h-glide-track" data-testid="glide-track" ref={track}
            style={reduced ? undefined : { transform: `translate3d(${-k * over}px,0,0)` }}>
            {HOME.features.map((f, i) => (
              <article key={f.num} className="h-feature">
                <div className="h-ico">{ICONS[i]}</div><span className="h-num" aria-hidden="true">{f.num}</span>
                <h3>{f.title}</h3><p>{f.body}</p>
                <div className="h-eg"><Segs parts={f.eg} />{f.egNote && <small>{f.egNote}</small>}</div>
              </article>
            ))}
          </div>
        </div>
        <div className="h-band h-glide-bar" aria-hidden="true"><i style={{ width: `${k * 100}%` }} /></div>
      </div>
    </section>
  );
}
