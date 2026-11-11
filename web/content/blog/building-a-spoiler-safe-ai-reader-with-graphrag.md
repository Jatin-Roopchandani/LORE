---
title: How I built a spoiler-safe AI reader for your ebooks with GraphRAG
description: How LORE turns ebooks into a knowledge graph, makes every sentence of an answer cite and pass a check against its source, and keeps answers inside the reader's chapter.
date: 2026-11-11
---

I was halfway through a mystery novel and couldn't remember who a minor character was. I searched for it, and the first result spoiled the ending.

That's why I built **LORE**, an open-source reader that turns the books you own into a knowledge graph and answers questions about them. Every sentence is cited, and nothing past the chapter you're on is spoiled. This post covers how it works, and the bugs that taught me the most.

## Why not just ask ChatGPT?

You can ask a general chatbot about a famous book, and it'll answer from memory. That breaks down in three ways:

1. **It can't cite.** You get a confident paragraph and no way to check it.
2. **It doesn't know your book.** A less famous novel, a new release, or your own copy: it's guessing.
3. **It has no idea where you are.** Ask "is this character trustworthy?" at chapter 4, and it'll happily tell you about chapter 30.

So the rule for LORE was: **answer only from the text, cite every sentence, and never look past the reader's position.**

## The pipeline

When you upload an EPUB or PDF, LORE reads it once:

1. **Split into chapters and passages.** It uses the table of contents when the book has no clean headings.
2. **Extract** characters, places, groups, objects and events, and the relationships between them, with an LLM. You bring your own key: Gemini, Claude or OpenAI.
3. **Resolve entities.** "Holmes", "Mr. Sherlock Holmes" and "the detective" become one node. Claimed identities (a character secretly being someone else) become explicit links.
4. **Store** the graph in Kuzu and the passage embeddings in LanceDB. Embeddings are computed locally with `nomic-embed-text` through Ollama.

Then, when you ask a question:

```
question
  → split into the parts that need answering
  → find starting entities (vector search + the graph)
  → walk the graph for the relationships that matter
  → pull the exact passages behind those relationships
  → extract claims, each with a quote from a passage
  → check every claim against its quote; cut what fails
  → write the answer as prose, every sentence cited
```

The graph is what makes multi-book questions possible. "How does this character change across the series?" becomes a walk over one entity's relationships in story order, rather than a hope that the right passages land in the top 10 of a vector search.

## Every sentence has to earn its place

The core data structure is boring on purpose:

```python
class Claim(BaseModel):
    text: str      # "Stamford introduces Watson to Holmes."
    passage: int   # which retrieved passage it stands on
    quote: str     # the exact words from that passage
```

A claim with no quote, or a quote that isn't in the passage it cites, never reaches the reader. A second pass then checks that the passage actually *supports* the claim, not just that the quote exists. Only the survivors get written up as prose.

Getting the checker right was its own project. Some of what went wrong:

- **Flip-flopping verdicts.** A claim accepted in round 1 got rejected in round 2 of the same answer. Fix: later rounds keep the first round's verdicts.
- **Contradicting itself.** The checker would write a reason that supported the claim, then reject it anyway. Fix: ask for the reason before the verdict, and re-ask any rejection whose own reason contradicts it.
- **Getting who-did-what wrong.** The writer would turn "A accused B" into "B accused A" while keeping the citation. Fix: explicit rules in the writer and both checks about who did what to whom.

Each fix ships behind a switch, and each switch has a **blind paired eval**: the same questions answered with the switch on and off, graded without knowing which is which, compared with a sign test. If a change doesn't win on held-out questions, it doesn't ship. It's very easy to "improve" a RAG system on the five questions you keep testing by hand.

## Spoiler protection is a filter, not a prompt

The tempting way to avoid spoilers is a prompt: "don't reveal anything after chapter 4." That doesn't work. If the evidence is in the context, it leaks.

So LORE never retrieves it. Your reading progress becomes a per-book chapter cap, and that cap is applied as a **prefilter** on every vector search, every graph query and every character profile:

```python
# simplified
where = ("book_id IN ('study-in-scarlet', 'sign-of-four') AND "
         "(book_id != 'sign-of-four' OR chapter_seq <= 6)")
rows = table.search(vector).where(where, prefilter=True).limit(k)
```

The model can't spoil what it never sees.

### The bug that looked like an outage

While recording the demo, I asked "who is the killer?" with my progress at chapter 4. The answer came back *instantly*: "The books don't contain enough to answer that." No graph, no passages, zero model calls.

It looked like something was broken. It wasn't. The best passage within the chapters I'd read scored **0.618** against a relevance floor of **0.62**, so the pipeline correctly stopped before spending a single request. The answer lives in chapter 7 (score 0.679), exactly where it should be hidden.

The behaviour was right; the *message* was wrong. "The books don't contain it" is false; "you haven't got there yet" is true. So now, when the capped search comes up empty, LORE runs one more vector search without the cap. That's about 50 ms, with no model call and nothing sent to a model. If something relevant is further on, it says:

> You haven't reached the part of the book that answers this yet. Tick "Show spoilers for this question" to see it anyway.

## A React bug: the Stop button that re-asked the question

I added a Stop button. Pressing it cancelled the answer, and then asked the same question again a second later.

The Stop and Ask buttons were the same `<button>` element, with React switching its `type` from `button` to `submit`. Clicking Stop updated state, React re-rendered *during the click*, and by the time the browser ran the click's default action the button was a submit button, so the form submitted the question that had just been put back in the box.

```tsx
// before: one element whose type changes mid-click
{busy ? <button type="button" onClick={stop}>Stop</button>
      : <button type="submit">Ask</button>}

// after: two elements; React never reuses one for the other
{busy ? <button key="stop" type="button" onClick={stop}>Stop</button>
      : <button key="ask" type="submit">Ask</button>}
```

A test's synthetic click can't reproduce it, because the re-render runs after the whole dispatch. So the test checks the structure instead: after Stop, the Ask button must be a *different element*.

## Use it from Claude

LORE also runs an **MCP server** with OAuth, so you can add your library to Claude as a custom connector and ask about your books from inside Claude. You get the same citations, and the same reading position applies. See [Use your ebook library in Claude](/blog/use-your-ebook-library-in-claude).

## Stack

- **Backend:** Python, FastAPI, Kuzu (graph), LanceDB (vectors), Postgres (accounts), Ollama (embeddings)
- **Models:** bring your own key: Gemini (the free tier works), Claude or OpenAI
- **Frontend:** React and TypeScript; the graph animates as the answer searches it
- **Hosting:** Docker Compose on a single ARM VM behind a Cloudflare Tunnel

## Try it

- Source: [github.com/Jatin-Roopchandani/LORE](https://github.com/Jatin-Roopchandani/LORE)
- Hosted beta (invite-only): [request an invite](/#beta)

It's rough in places. Answers are only as good as the extraction, and long series take a while to process. I'd love feedback, especially examples where it gets something wrong.
