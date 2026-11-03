---
name: plain-prose
description: "Rules for writing prose that doesn't read as AI-generated. Use for text someone else will read as a finished thing: documentation, READMEs, customer notices, emails, reports, grant or proposal text, website copy, forum posts, published announcements. Also use when asked to de-AI, humanize, or clean up a draft, and in a discussion when Chris explicitly asks for it. DO NOT use during day-to-day coding, investigations or diagnostics, and not for commit messages, PR descriptions, code comments, task-board entries, status updates or working-session chat replies: the multi-pass workflow costs real time and tokens and those are not worth it. When in doubt during a working session, do not invoke it; ask. Do not apply to the user's personal essays or blog posts unless explicitly asked; those are not to be ghostwritten or edited."
---

# Plain prose

## When to use this

The full workflow is five passes and a subagent. That is worth it for something
a person will read as a finished piece, and it is not worth it for the work of
a normal session.

Use it for: documentation, READMEs, customer notices, emails, reports, grant or
proposal text, website copy, forum posts, published announcements. Use it when
asked to de-AI or clean up a draft. Use it in a discussion when Chris asks for
it by name.

Do not use it for: commit messages, PR descriptions, code comments, task-board
entries, investigation write-ups, status updates, or replies during a working
session. Those are day-to-day coding artifacts. Writing them plainly is still
the goal, but reach for the rules from memory rather than running the passes.

The test is authorship, not genre. An essay, blog post, or article that Chris
asked you to write is a deliverable and gets the full process. An essay or blog
post that Chris wrote is his; do not run the passes over it, and do not edit it
at all unless he asks. Same words, opposite instruction, decided by who wrote it.

When you are unsure mid-session, do not invoke it. Ask.

Every AI writing tell is the same move: sounding important while committing to nothing. Models pick words that are unlikely to be wrong rather than words that are right, so the text drifts toward abstract nouns, hollow intensifiers, borrowed drama, and sentences that announce what they were supposed to do. The result is less specific and more exaggerated at the same time.

The fix is one habit: commit. Name the concrete thing. Make the checkable claim. Use the plain verb. Then stop, and trust the reader to draw the conclusion.

Committing means a flat declarative sentence. It does not mean a punchy one. The instinct to commit by contrast ("it isn't X, it's Y"), by chiasmus, or by a one-line zinger is the tell the rules are meant to remove, and it is the first thing that reappears when you try to sound sure of yourself. "The premise is half right; the policies differ where it matters" commits. "The premise is half right, and the half that's wrong is the interesting half" performs.

These patterns are trained in, so a single pass will not remove them. Testing showed rules 6, 13, and 14 failing on a fresh draft even with the list in view, because they describe habits that operate below word choice. Treat writing under this skill as a multi-draft process (see "Drafting process" below), the way a person rewrites a paragraph two or three times. Do not deliver the first draft.

The rules are grouped by level. Before/after pairs are in `references/examples.md` if a rule is unclear in practice.

## Words

1. **Concrete nouns.** Replace ecosystem, infrastructure, landscape, framework, realm, paradigm, trajectory, dynamic (noun), tapestry, testament, interplay with the actual thing under discussion. "The physical infrastructure for a tennis career" is "cardiovascular fitness."
2. **No intensifiers.** Cut genuinely, truly, actually, quietly, seamlessly, fundamentally, crucially, importantly, notably, and the adjectives crucial, pivotal, vital, vibrant, robust, nuanced, significant, strategic, key, intricate, multifaceted, load-bearing. If the sentence needs the intensifier it is weak; if it doesn't, the word is noise.
3. **Plain verbs.** Use, not utilize. Help, not facilitate. Show, not demonstrate. Look at, not delve into or explore. Support, not underpin. Avoid leverage, foster, navigate, elevate, synthesize, deploy, streamline, enhance, showcase, garner, align with, shed light on, unless the literal sense is meant (you can deploy a server).
4. **No metaphor verbs in place of a claim.** "Earns its place," "anchors the discussion," "surfaces the issue," "signals a shift," "unlocks value," "lands," "sits with," "holds," "carries weight." Say what literally happens. Ordinary verbs like build, drive, shape, hold are fine when literal; the problem is figurative use standing in for a concrete statement.

## Sentences

5. **Vary length and shape.** Not every sentence 15 to 18 words trailing into a qualifying phrase. Follow a long sentence with a short one. Start some with the subject, some with a clause.
6. **No contrast for rhythm.** This covers negative parallelism ("it's not X, it's Y," "not only X but also Y," "not X. Not Y. Z.") and also antithesis without a "not": "the ideal was his; the life was not," "admirable discipline, fatal in an emergency," "works as an act, fails as a policy," and any sentence whose second half exists to mirror the first. Use contrast only to correct an assumption the reader actually holds. If nobody thought it was X, don't rule X out. Sentences beginning "it isn't," "this has nothing to do with," or "the real reason is" are almost always this pattern.
7. **Lists of three only when there are three things.** If the third item is there for rhythm and adds nothing the second didn't, cut it.
8. **No question-and-answer fragments.** "The result? Transformative." "The problem? Cost." This is the single most reliable tell. Never.
9. **No false ranges.** "From the farmhouse to the boardroom." "From teenagers to managing directors." Test: if you can swap one endpoint for an unrelated noun and the sentence still works, there is no scale, so delete the construction.
10. **No participle tails.** End the sentence before "underscoring," "highlighting," "reflecting," "signaling," "ensuring," "contributing to," "showcasing." State the evidence and stop. Facts do not highlight things; a narrator does, and the reader doesn't need one. Related: prefer verbs to nominalizations (decide, not make a decision; the servers failed, not server failure occurred) and name the agent rather than use agentless passive.
11. **Em dashes sparingly.** Prefer commas, parentheses, or a colon. One per paragraph at most, usually none.

## Paragraphs

12. **Vary paragraph length.** Let one run long and the next be two sentences. The intro-explain-example-wrap-up template, applied every time, is a tell even when each paragraph is fine alone.
13. **No one-line paragraphs for drama.** This includes the opening paragraph. Put the strong sentence inside a real paragraph instead.
13b. **Don't end every paragraph on its best line.** If each paragraph resolves on a quotable sentence, the rhythm never breaks and the piece reads as performed. Let some paragraphs end on a plain fact, a number, or the middle of the thought. Same for the piece as a whole: don't close on an aphorism.
14. **Don't announce, don't recap.** No openers like "Here's the thing," "It's important to note," "What most people miss." No closers like "Taken together," "This means that," "In this way," "In summary." Test: delete the last sentence of every paragraph; if nothing is lost, it should not have been there.
15. **Prose over bullets.** Use a list only when the items are parallel, atomic, and meant to be scanned. No bold-header-colon bullets. No headers on short pieces. No title-case headers. No emoji. Bold almost never.

## Argument

16. **Take a side.** No "both perspectives have merit," "the truth lies in the middle," "on balance," "it depends." Hedge by naming the specific exception ("this fails when the site has no generator"), not with "often but not always."
17. **Deeper, not wider.** One claim developed with evidence beats the same claim restated three ways. Don't coin a framework name ("the acceleration trap," "Jeffersonian in purpose, Hamiltonian in construction") and then describe the name; a claim says what it predicts. Don't write "experts believe," "studies suggest," or "history favors" without a named source or case. Don't append a bonus frame at the end ("one more thought worth sitting with," a new author's lens in the last paragraph) because it's interesting; if the argument didn't need it, cut it and use the space to go deeper on the main point.
18. **Right-size the stakes.** Nothing "changes everything," is "transformative," "paradigm-shifting," or "game-changing." A modest claim is more believable than an inflated one, and easier to defend.
19. **Uneven temperature, decided before drafting.** Pick the one point that matters most and give it most of the space; dispatch predictable points in a sentence or two. Four blocks of equal size, each with one move, is the institutional shape even when every sentence is clean. This is a structural decision and cannot be fixed by editing sentences afterward.
20. **Hold the thesis.** Before finishing, check the last paragraph against the first. Cut anything that drifted or quietly contradicts the opening.

## Chatbot residue

21. None of: "I hope this helps," "Let me know if," "Certainly!", "Great question," "You're absolutely right," "It's worth noting," "as of my knowledge cutoff," "as an AI." No offers of further help at the end of a document.

## Facts

22. **Specificity has to be earned.** These rules push toward numbers, names, and dates, which is right, but a specific-sounding claim that was estimated rather than checked ("about 40 em dashes," "the tightest money since the 1920s," "historians of both parties") is a worse tell than vagueness, because it invites a check it can't pass. Before delivering, reread every number and superlative and ask whether you measured it or reached for it. Soften or cut the ones you reached for.

## Drafting process

Writer and editor are separate invocations. A model editing its own draft in the
same context keeps the lines it just wrote, and testing bore this out: the
structural rules (6, 12, 13, 13b, 14, 19) failed every time the writer tried to
self-edit in one pass, and the first sentence of the piece was the worst
violation in all five test pieces. The mechanical checks belong in a script, not
in a rule the writer is asked to hold in mind while drafting.

**Pass 1, writer.** Write a plan block in a scratch file, not in the deliverable:

- the verdict, in one flat sentence;
- where the thesis sentence sits — which paragraph, opener or body, never a closer;
- the paragraph list with sentence counts as numbers, deliberately uneven;
- the first sentence of each paragraph, written out flat.

Then write the draft to `draft.md` under that plan, holding to the counts. The
plan exists because rule 19 is a decision made before drafting; it cannot be
recovered by editing sentences afterward.

**Pass 2, lint.** `python3 scripts/lint.py draft.md > lint.txt`. The script
reports, it does not gate. Exit status is always 0.

**Pass 3, editor, in a fresh context.** Spawn a subagent with three things only:
this SKILL.md, `draft.md`, and `lint.txt`. It does not get the plan or any of
the writer's reasoning. Its instruction:

> You did not write this. Edit it to comply with the rules. Fix every lint flag
> or state in one line why it stays. Work the passes in this order: structure
> (resize or cut paragraphs; move the thesis to an opener if it landed as a
> closer), edges (using the two lists in lint.txt), then sentences. Do not add
> material. Do not flatten a line that is the only ironic or strong closer in
> the piece; flatten the others. Write the result to `edited.md`.

**Pass 4, lint again** on `edited.md`. If new flags appear, one more editor
pass, then stop. Do not loop.

**Pass 5, deliver.** Present `edited.md`. Do not present `draft.md`, and do not
ask the reader to adjudicate a flagged line.

## Editing someone else's draft

A draft that arrives from a human skips pass 1 and goes straight to pass 2 and
pass 3: lint it, then edit it in a fresh context under the rules.

Two changes to the editor's instruction. **Propose structural changes, do not
make them.** Resizing paragraphs and moving a thesis are free on model output
and are not yours to do on someone's draft; list them and let the author
decide. **Preserve the voice.** These rules remove what a model adds. They are
not a house style, and a human's odd rhythm, favourite construction, or blunt
closer is a choice until the author says otherwise.

Where the writer is unknown or the text is mixed, treat it as a human draft.
The cost of proposing a change that was wanted is a sentence; the cost of
flattening a person's prose without asking is the thing this skill exists to
prevent.
