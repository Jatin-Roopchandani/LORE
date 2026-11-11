---
title: How to ask questions about a book without getting spoiled
description: Searching for a character mid-book often spoils the ending. Here is how to get answers about a book you're still reading, limited to the chapters you've actually read.
date: 2026-11-11
---

The safest way to ask about a book you're still reading is to use a tool that only knows the chapters you've read. Search engines, wikis and general AI chatbots all know the whole book, so the answer to "who is this character?" can include what happens to them three hundred pages later. LORE avoids that by never retrieving anything past your reading position in the first place.

## Why searching mid-book goes wrong

You're halfway through a long novel and a name comes back that you half remember. You search it. The first result is a wiki page whose opening line gives away a twist, or a forum thread titled with the ending.

The problem isn't carelessness. Every general source is written for someone who has finished the book:

- **Wikis and summaries** describe a character's whole arc, often in the first sentence.
- **Search results** surface the most discussed facts, and the most discussed facts are usually the twists.
- **AI chatbots** answer from everything they learned about the book. Telling one "don't spoil anything after chapter 12" doesn't reliably work: if the model knows the ending, it can slip into the answer.

## What a spoiler-safe answer needs

To answer without spoiling, a tool has to do three things:

1. **Know where you are**, book by book. In a series, you might have finished the first two books and be partway through the third.
2. **Only read up to that point.** Not "try not to mention" later chapters, but never look at them at all.
3. **Show its sources**, so you can see that every fact comes from a chapter you've already read.

## How LORE does it

In LORE, every book in your library has a reading state: **Finished**, **Reading** (with the chapter you're on) or **Not started**. When you ask a question, that position becomes a hard limit on what LORE searches:

- The passage search only looks at chapters you've read.
- The knowledge graph only includes the people, places and relationships you've met so far, under the names you know them by. If a character is later revealed to be someone else, that link stays hidden until you get there.
- Character profiles and timelines stop at your chapter.

Because the later chapters are never retrieved, there is nothing for the AI to leak. Every sentence in the answer cites the passage it comes from, so you can check that each one is from a chapter you've read.

## What it looks like

Say you're on chapter 4 of *The Sign of the Four* and ask: "Who is the killer?"

LORE searches chapters 1 to 4, finds nothing that answers the question, and tells you so honestly:

> You haven't reached the part of the book that answers this yet. Tick "Show spoilers for this question" to see it anyway.

Ask instead "What do we know about Thaddeus Sholto so far?" and you get an answer built only from what the first four chapters say about him, with each sentence cited.

If you do want to look ahead for one question, the spoiler toggle answers that single question from the whole book, then goes back to your reading position.

## Tips for spoiler-free reading

- **Keep your position up to date.** Moving it forward as you read means answers keep pace with you.
- **Ask about what you've seen, not what's coming.** "What has Mira said about the gate so far?" gets you a useful recap. "Does Mira survive?" will just tell you that you haven't got there.
- **Use the graph to refresh your memory.** Clicking a character shows who they're connected to and the passages behind each link, all within what you've read.
- **For a series, mark the earlier books Finished.** Questions then draw on everything you've read across the series, and nothing past your current chapter.

## Try it

LORE is open source on [GitHub](https://github.com/Jatin-Roopchandani/LORE), and the hosted version is in an invite-only beta. You can [request an invite](/#beta), or read the [FAQ](/faq) for more on how it works.
