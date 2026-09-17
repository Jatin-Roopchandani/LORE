# Phase 3 held-out acceptance questions, set C

> **Frozen 2026-09-17, before any Task 10 fix and before any run.** Written from the EPUB text
> alone, without access to the graph, the agent, or any eval output, and without reading the
> answers of spec §8 or set H (only their question lines, to avoid their subjects). No entry may
> be added, removed or reworded after this commit. A question the agent cannot answer is a
> finding.

**Chapter references.** Same numbering as set H. "ch N" is the chapter number in spine order, as
`parse_epub` in `lore/ingest/epub.py` numbers the chapters of `data/inbox/study-in-scarlet.epub`
and `data/inbox/sign-of-the-four.epub` (the Gutenberg front matter takes ch 1–3 in both books, and
*A Study in Scarlet* has a ch 4 for "PART I."). The novel's own chapter heading follows in
brackets. *A Study in Scarlet* numbers its chapters afresh in Part II, so the part is given too.

Shapes: 4 cross-book (C1–C4), 3 multi-hop (C5–C7), 3 single-passage (C8–C10).
Two-part "who/what … and why/how …" questions: C5, C6, C7, C9, C10.

### C1. In each novel Watson finds Holmes busy with a chemistry experiment. What was Holmes working on in each book?

- **Answer (verified from the text):** In *A Study in Scarlet*, a test for blood: at their first
  meeting Holmes shouts "I have found a re-agent which is precipitated by hæmoglobin, and by
  nothing else." In *The Sign of the Four*, while stuck on the case he did "a chemical analysis"
  and was "dissolving the hydrocarbon which I was at work at".
- **Where:**
  - *A Study in Scarlet*, ch 5 [Part I, Chapter I, "Mr. Sherlock Holmes"]: "I have found a
    re-agent which is precipitated by hæmoglobin, and by nothing else."
  - *The Sign of the Four*, ch 13 [Chapter X, "The End of the Islander"]: "I gave my mind a
    thorough rest by plunging into a chemical analysis … When I had succeeded in dissolving the
    hydrocarbon which I was at work at, I came back to our problem of the Sholtos".
- **Shape:** cross-book

### C2. Early in each novel Holmes shows off his powers of deduction. What does he deduce about the messenger in the street in A Study in Scarlet, and about Watson's watch in The Sign of the Four?

- **Answer (verified from the text):** In *A Study in Scarlet* he says a man in the street with a
  message is "the retired sergeant of Marines", inferred from "a great blue anchor tattooed on the
  back of the fellow's hand", "a military carriage … and regulation side whiskers", and his "air of
  command". In *The Sign of the Four* he deduces the watch "belonged to your elder brother, who
  inherited it from your father", and that the brother was careless, "lived for some time in
  poverty with occasional short intervals of prosperity, and finally, taking to drink, he died" —
  from the dents and scratches on the case, four pawnbroker's numbers inside it, and the scratches
  round the keyhole ("you will never see a drunkard's watch without them").
- **Where:**
  - *A Study in Scarlet*, ch 6 [Part I, Chapter II, "The Science of Deduction"]: "You mean the
    retired sergeant of Marines," said Sherlock Holmes.
  - *A Study in Scarlet*, ch 7 [Part I, Chapter III, "The Lauriston Gardens Mystery"]: "I could see
    a great blue anchor tattooed on the back of the fellow's hand. That smacked of the sea. He had
    a military carriage, however, and regulation side whiskers. There we have the marine."
  - *The Sign of the Four*, ch 4 [Chapter I, "The Science of Deduction"]: "I should judge that the
    watch belonged to your elder brother, who inherited it from your father" … "finally, taking to
    drink, he died."
- **Shape:** cross-book

### C3. In A Study in Scarlet, how does Watson rate Holmes's knowledge of literature, and which German writers does Holmes quote or discuss in The Sign of the Four?

- **Answer (verified from the text):** In *A Study in Scarlet* Watson's list of Holmes's limits
  gives "Knowledge of Literature.—Nil", and Holmes did not even know who Thomas Carlyle was. In
  *The Sign of the Four* Holmes quotes Goethe in German ("Goethe is always pithy"; later "those
  lines of old Goethe") and asks Watson "Are you well up in your Jean Paul?", discussing a remark of
  his (Watson says he "worked back to him through Carlyle").
- **Where:**
  - *A Study in Scarlet*, ch 6 [Part I, Chapter II, "The Science of Deduction"]: "Upon my quoting
    Thomas Carlyle, he inquired in the naivest way who he might be and what he had done" and
    "1. Knowledge of Literature.—Nil."
  - *The Sign of the Four*, ch 9 [Chapter VI, "Sherlock Holmes Gives a Demonstration"]: "'Wir sind
    gewohnt dass die Menschen verhöhnen was sie nicht verstehen.' Goethe is always pithy."
  - *The Sign of the Four*, ch 10 [Chapter VII, "The Episode of the Barrel"]: "Are you well up in
    your Jean Paul?" / "Fairly so. I worked back to him through Carlyle."
  - *The Sign of the Four*, ch 15 [Chapter XII, "The Strange Story of Jonathan Small"]: "I often
    think of those lines of old Goethe".
- **Shape:** cross-book

### C4. Does Holmes take drugs? What does Watson say about this in each novel?

- **Answer (verified from the text):** In *A Study in Scarlet* Watson only wonders: during Holmes's
  listless spells he "might have suspected him of being addicted to the use of some narcotic, had
  not the temperance and cleanliness of his whole life forbidden such a notion." In *The Sign of
  the Four* Holmes injects cocaine, "a seven-per-cent. solution", which Watson had watched "Three
  times a day for many months"; and at the end, asked what remains for him, Holmes says "there
  still remains the cocaine-bottle."
- **Where:**
  - *A Study in Scarlet*, ch 6 [Part I, Chapter II, "The Science of Deduction"]: "I might have
    suspected him of being addicted to the use of some narcotic, had not the temperance and
    cleanliness of his whole life forbidden such a notion."
  - *The Sign of the Four*, ch 4 [Chapter I, "The Science of Deduction"]: "Which is it to-day?" I
    asked,—"morphine or cocaine?" … "It is cocaine," he said,—"a seven-per-cent. solution."
  - *The Sign of the Four*, ch 15 [Chapter XII, "The Strange Story of Jonathan Small"]: "For me,"
    said Sherlock Holmes, "there still remains the cocaine-bottle."
- **Shape:** cross-book

### C5. What medical condition did Watson discover in Jefferson Hope, and how did it keep Hope from ever standing trial?

- **Answer (verified from the text):** An aortic aneurism, which Hope said he got "from
  over-exposure and under-feeding among the Salt Lake Mountains". On the night after his capture
  the aneurism burst and he was found dead in his cell, so he never came before the magistrates.
- **Where:**
  - *A Study in Scarlet*, ch 17 [Part II, Chapter VI, "A Continuation of the Reminiscences of John
    Watson, M.D."]: "Why," I cried, "you have an aortic aneurism!" … "I got it from over-exposure
    and under-feeding among the Salt Lake Mountains."
  - *A Study in Scarlet*, ch 18 [Part II, Chapter VII, "The Conclusion"]: "On the very night after
    his capture the aneurism burst, and he was found in the morning stretched upon the floor of the
    cell".
- **Shape:** multi-hop

### C6. Where was Joseph Stangerson's body found, and how did he come to be killed?

- **Answer (verified from the text):** In his second-floor room at Halliday's Private Hotel, in
  Little George Street, dead from "a deep stab in the left side, which must have penetrated the
  heart". Jefferson Hope climbed in at dawn by ladders from the lane behind the hotel and offered
  him the same choice of pills as Drebber, but Stangerson "sprang from his bed and flew at my
  throat", and Hope "stabbed him to the heart" in self-defence.
- **Where:**
  - *A Study in Scarlet*, ch 11 [Part I, Chapter VII, "Light in the Darkness"] (Lestrade's
    account): "I reached Halliday's Private Hotel, in Little George Street" … "The cause of death
    was a deep stab in the left side, which must have penetrated the heart."
  - *A Study in Scarlet*, ch 17 [Part II, Chapter VI, "A Continuation of the Reminiscences of John
    Watson, M.D."] (Hope's statement): "I took advantage of some ladders which were lying in the
    lane behind the hotel" … "he sprang from his bed and flew at my throat. In self-defence I
    stabbed him to the heart."
- **Shape:** multi-hop

### C7. When Watson forced open the Agra treasure chest, what was inside it, and why?

- **Answer (verified from the text):** Nothing: "The box was empty!" Jonathan Small had thrown the
  treasure into the Thames when he saw the police launch would catch them, scattering it "over five
  miles or so", because he would rather lose it than "let it go to kith or kin of Sholto or of
  Morstan".
- **Where:**
  - *The Sign of the Four*, ch 14 [Chapter XI, "The Great Agra Treasure"]: "The box was empty!" …
    "It was absolutely and completely empty."
  - *The Sign of the Four*, ch 15 [Chapter XII, "The Strange Story of Jonathan Small"]: "throw the
    treasure into the Thames rather than let it go to kith or kin of Sholto or of Morstan" … "When I
    saw that your launch must catch us, I put the loot away in a safe place" … "Now that they are
    scattered over five miles or so, it may be a harder job."
- **Shape:** multi-hop

### C8. How did Bartholomew Sholto work out where the treasure was hidden in Pondicherry Lodge?

- **Answer (verified from the text):** He measured the whole house. The building was seventy-four
  feet high, but the rooms added up to no more than seventy feet, leaving "four feet unaccounted
  for" at the top. He knocked through the ceiling of the highest room and found a sealed garret
  with the treasure-chest in it.
- **Where:** *The Sign of the Four*, ch 7 [Chapter IV, "The Story of the Bald-Headed Man"]
  (Thaddeus Sholto): "he found that the height of the building was seventy-four feet, but on adding
  together the heights of all the separate rooms … he could not bring the total to more than
  seventy feet. There were four feet unaccounted for. … He knocked a hole, therefore, in the
  lath-and-plaster ceiling of the highest room, and there, sure enough, he came upon another little
  garret above it, which had been sealed up".
- **Shape:** single-passage

### C9. When the Mormons found John Ferrier and the child Lucy starving on the plain, on what condition did they agree to take them along, and who was put in charge of them?

- **Answer (verified from the text):** Only "as believers in our own creed"; Ferrier said he would
  come "on any terms". The leader then told Brother Stangerson to take them, feed them, and teach
  Ferrier the Mormon creed.
- **Where:** *A Study in Scarlet*, ch 12 [Part II, Chapter I, "On the Great Alkali Plain"]: "If we
  take you with us," he said, in solemn words, "it can only be as believers in our own creed." …
  "Take him, Brother Stangerson," he said, "give him food and drink, and the child likewise. Let it
  be your task also to teach him our holy creed."
- **Shape:** single-passage

### C10. At the end of The Sign of the Four, whom does Watson become engaged to, and why does Holmes say he cannot congratulate him?

- **Answer (verified from the text):** Mary Morstan. Holmes thinks her "one of the most charming
  young ladies I ever met", but "love is an emotional thing, and whatever is emotional is opposed to
  that true cold reason which I place above all things"; he would never marry himself, "lest I bias
  my judgment."
- **Where:** *The Sign of the Four*, ch 15 [Chapter XII, "The Strange Story of Jonathan Small"]:
  "Miss Morstan has done me the honour to accept me as a husband in prospective." … "I really cannot
  congratulate you." … "But love is an emotional thing, and whatever is emotional is opposed to that
  true cold reason which I place above all things. I should never marry myself, lest I bias my
  judgment."
- **Shape:** single-passage
