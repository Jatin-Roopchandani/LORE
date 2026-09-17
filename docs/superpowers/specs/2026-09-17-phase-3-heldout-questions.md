# Phase 3 held-out acceptance questions

> **Frozen 2026-09-17, before any run.** Written from the EPUB text alone, without access to the
> graph, the agent, or any eval output. No entry may be added, removed or reworded after the
> first run. A question the agent cannot answer is a finding.

**Chapter references.** "ch N" is the chapter number in spine order, as `parse_epub` in
`lore/ingest/epub.py` numbers the chapters of `data/inbox/study-in-scarlet.epub` and
`data/inbox/sign-of-the-four.epub` (the Gutenberg front matter takes ch 1–3 in both books, and
*A Study in Scarlet* has a ch 4 for "PART I."). The novel's own chapter heading follows in
brackets. *A Study in Scarlet* numbers its chapters afresh in Part II, so the part is given too.

Shapes: 3 cross-book (H1–H3), 3 multi-hop (H4–H6), 2 single-passage (H7–H8).

### H1. In both novels Holmes employs a gang of street boys. Who is their spokesman, and what does Holmes send them to find in each novel?

- **Answer (verified from the text):** Their spokesman in both books is **Wiggins**. In *A Study
  in Scarlet* Holmes sends them round the cab proprietors of London to find the cabman who turns
  out to be Jefferson Hope. In *The Sign of the Four* he sends them to find the steam launch
  **Aurora** on the river.
- **Where:**
  - *A Study in Scarlet*, ch 10 [Part I, Chapter VI, "Tobias Gregson Shows What He Can Do"]:
    "In future you shall send up Wiggins alone to report … Have you found it, Wiggins?"
  - *A Study in Scarlet*, ch 11 [Part I, Chapter VII, "Light in the Darkness"]: "the spokesman
    of the street Arabs, young Wiggins … 'Please, sir,' he said, … 'I have the cab downstairs.'"
  - *A Study in Scarlet*, ch 18 [Part II, Chapter VII, "The Conclusion"]: "I therefore organized
    my Street Arab detective corps, and sent them systematically to every cab proprietor in
    London until they ferreted out the man that I wanted."
  - *The Sign of the Four*, ch 11 [Chapter VIII, "The Baker Street Irregulars"]: "That wire was to
    my dirty little lieutenant, Wiggins" and "In future they can report to you, Wiggins, and you
    to me. … I want to find the whereabouts of a steam launch called the Aurora".
- **Shape:** cross-book

### H2. Watson was wounded by a Jezail bullet in Afghanistan. Where on his body does each novel say the bullet hit him?

- **Answer (verified from the text):** The two books disagree. *A Study in Scarlet* says the
  **shoulder** (at the battle of Maiwand). *The Sign of the Four* says his **leg**.
- **Where:**
  - *A Study in Scarlet*, ch 5 [Part I, Chapter I, "Mr. Sherlock Holmes"]: "I served at the fatal
    battle of Maiwand. There I was struck on the shoulder by a Jezail bullet, which shattered the
    bone and grazed the subclavian artery."
  - *The Sign of the Four*, ch 4 [Chapter I, "The Science of Deduction"]: "sat nursing my wounded
    leg. I had a Jezail bullet through it some time before".
- **Shape:** cross-book

### H3. Holmes makes use of a dog in each novel. Which dog is it in each book, and what does he use it for?

- **Answer (verified from the text):** In *A Study in Scarlet* it is the landlady's old, dying
  **terrier**. Holmes feeds it the pills to test them: the first half-pill does nothing, the
  second kills it, which shows one pill was deadly poison and the other harmless. In *The Sign of
  the Four* it is **Toby**, "half spaniel and half lurcher", borrowed from old Sherman at No. 3
  Pinchin Lane, Lambeth. Holmes has it follow the scent of the creosote that the intruder stepped
  in.
- **Where:**
  - *A Study in Scarlet*, ch 11 [Part I, Chapter VII, "Light in the Darkness"]: "that poor little
    devil of a terrier which has been bad so long, and which the landlady wanted you to put out of
    its pain" and "Of the two pills in that box one was of the most deadly poison, and the other
    was entirely harmless."
  - *The Sign of the Four*, ch 9 [Chapter VI, "Sherlock Holmes Gives a Demonstration"]: "No. 3,
    Pinchin Lane, down near the water's edge at Lambeth. … Sherman is the name. … I want Toby at
    once."
  - *The Sign of the Four*, ch 10 [Chapter VII, "The Episode of the Barrel"]: "Toby proved to be an
    ugly, long-haired, lop-eared creature, half spaniel and half lurcher" and "He pushed the
    creasote handkerchief under the dog's nose".
- **Shape:** cross-book

### H4. In A Study in Scarlet, Constable John Rance saw a drunk man at the gate of the murder house. Who was that man really, and why had he come back to the house?

- **Answer (verified from the text):** He was the murderer himself, **Jefferson Hope**, who was
  only pretending to be drunk. He had come back for **Lucy's ring**, which he found was missing
  from his pocket and thought he had dropped over Drebber's body.
- **Where:**
  - *A Study in Scarlet*, ch 8 [Part I, Chapter IV, "What John Rance Had to Tell"]: "He was at the
    gate when I came out, a-leanin' up agin the railings, and a-singin' at the pitch o' his lungs"
    and Holmes: "The man whom you held in your hands is the man who holds the clue of this
    mystery" … "The ring, man, the ring: that was what he came back for."
  - *A Study in Scarlet*, ch 17 [Part II, Chapter VI, "A Continuation of the Reminiscences of John
    Watson, M.D."], Hope's statement: "I put my hand into the pocket in which I usually kept
    Lucy's ring, and found that it was not there. … I drove back … When I arrived there, I walked
    right into the arms of a police-officer who was coming out, and only managed to disarm his
    suspicions by pretending to be hopelessly drunk."
- **Shape:** multi-hop

### H5. How did Jonathan Small lose his leg, and how did his wooden leg lead to his capture on the Thames?

- **Answer (verified from the text):** As a young soldier with the 3rd Buffs he went swimming in
  the Ganges and **a crocodile bit off his right leg** just above the knee. At the end of the
  chase the Aurora ran onto a mud-bank, and when he jumped out **his wooden stump sank into the
  mud** and held him fast, so that Holmes and Watson had to haul him out with a rope.
- **Where:**
  - *The Sign of the Four*, ch 15 [Chapter XII, "The Strange Story of Jonathan Small"]: "fool
    enough to go swimming in the Ganges. … A crocodile took me, just as I was half-way across, and
    nipped off my right leg as clean as a surgeon could have done it, just above the knee."
  - *The Sign of the Four*, ch 13 [Chapter X, "The End of the Islander"]: "The fugitive sprang out,
    but his stump instantly sank its whole length into the sodden soil. … he was so firmly
    anchored that it was only by throwing the end of a rope over his shoulders that we were able
    to haul him out".
- **Shape:** multi-hop

### H6. Who is Mary Morstan's employer, what had Holmes once done for that employer, and in what district does she live?

- **Answer (verified from the text):** She is governess to **Mrs. Cecil Forrester**. Holmes had
  once helped Mrs. Forrester "unravel a little domestic complication". She lives in **Lower
  Camberwell**.
- **Where:**
  - *The Sign of the Four*, ch 5 [Chapter II, "The Statement of the Case"]: "you once enabled my
    employer, Mrs. Cecil Forrester, to unravel a little domestic complication" and "I had at that
    time just entered the family of Mrs. Cecil Forrester in the capacity of governess."
  - *The Sign of the Four*, ch 9 [Chapter VI, "Sherlock Holmes Gives a Demonstration"]: "She lives
    with Mrs. Cecil Forrester, in Lower Camberwell".
- **Shape:** multi-hop

### H7. According to Sherlock Holmes, what does the word "Rache", written in blood on the wall at Lauriston Gardens, mean?

- **Answer (verified from the text):** It is the **German for "revenge"**. Holmes says this to
  rebut the idea that the writer was starting the name "Rachel".
- **Where:**
  - *A Study in Scarlet*, ch 7 [Part I, Chapter III, "The Lauriston Gardens Mystery"]: "'Rache,'
    is the German for 'revenge'; so don't lose your time looking for Miss Rachel."
- **Shape:** single-passage

### H8. Who were "the four" named in the sign of the four?

- **Answer (verified from the text):** **Jonathan Small, Mahomet Singh, Abdullah Khan and Dost
  Akbar.**
- **Where:**
  - *The Sign of the Four*, ch 6 [Chapter III, "In Quest of a Solution"], on the paper Miss Morstan says
    "was found in papa's desk": "'The sign of the four,—Jonathan Small, Mahomet Singh, Abdullah Khan,
    Dost Akbar.'"
  - Also in ch 15 [Chapter XII, "The Strange Story of Jonathan Small"]: "a second meeting, at which
    Mahomet Singh, Abdullah Khan, and Dost Akbar were all present."
- **Shape:** single-passage
