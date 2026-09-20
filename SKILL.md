---
name: learn
description: Tutor for one concept at a time. Builds a phone-friendly lesson page (picture first, few words, questions with answer boxes), grades what the learner typed, and keeps a terse record in their Obsidian vault. Stateful per topic.
disable-model-invocation: true
argument-hint: "<topic> [source: path or url]"
---

# Learn

Teach one concept cluster until the learner can use it. Understanding, not recall: every fact hangs off something they already accept, and the learner does something with it before moving on.

## The learner

Wants: picture first, few words, simple terms, no mannered prose, plain dashes (never em dashes). Does lessons on laptop and phone. Likes multiple choice where it fits and interview questions where it matters. "Just tell me" always gets the answer.

## The loop

Learner: opens the page, reads the picture, does the questions, then says one word in the terminal: `done`, `stuck`, `too easy`, `too hard`, `just tell me`, or `next`. `done` comes with the answers pasted from the page's **Copy answers** button (or, on a hosted page, you read them from its database).

You: grade, write feedback under each question, write the next lesson, update the vault note, save. The terminal is the control channel and gets one line. Everything readable goes on the page.

## Files

```
~/Learning/<topic-slug>/index.html      the topic page, one file, source of truth
~/Learning/<topic-slug>/sandbox/        code topics only: NN-<slug>/ with a stub to fill in and check.py (prints one line per case, ends PASS or FAIL, exit 0 or 1)
~/Obsidian Vault/.../<Topic>.md         the record: goal, source, lessons, misconceptions, page path
```

Nothing else.

## Start a topic

1. **Source.** If the learner names one, read it. If not, one research pass on the web to find one trusted source: the course guide, a textbook chapter, official docs. Facts, examples, and quiz numbers come from the source and cite it by section. No source, no lesson.
2. **Goal.** One line, concrete: what they will be able to do. Ask if it is unclear; if the learner is not there, pick one from the source. The goal goes in the vault note and as one plain sentence at the top of lesson 0, in words the learner already has.
3. **Plan 3 to 6 lessons**, prerequisites first, each a concept lesson or a drill. Put the whole plan in the page's sidebar. Write only lesson 0 now: its grade decides the help level and whether a prerequisite lesson is needed, so lesson 1 is written after it.
4. **Lesson 0, "Where you're at."** Two or three short questions on the prerequisites, auto-checked or interview; misconceptions already recorded in the vault are fair game here. Skip it only if the learner just passed the prerequisite topic; then lesson 1 is the first one written.
5. Copy `template.html` from this skill's folder, replace its placeholders, delete the sample sections you are not writing, open the page (see Hosting), write the vault note, commit the vault, one line in the terminal.

Returning to a topic: read the vault note and the page from disk, continue from the active lesson.

## Lessons

- **Concept lesson.** Picture, under 120 words, then 2 to 4 interview questions in answer boxes: explain it back, predict, apply, break a wrong claim. Never yes/no, never multiple choice. You grade.
- **Drill.** Picture or worked table, then 8 to 12 auto-checked items, multiple choice or a number. The first one or two items reuse the previous lesson's own example or key term, so a label alone is not a retrieval. Items test only rules from passed lessons, even if the source table shows more. The page grades instantly; you read the wrong attempts for patterns.

**Picture.** Before any prose. Inline SVG drawn with the lesson's real numbers, colors from the CSS tokens so it reads in both themes. A worked table with a "what happened" column counts as a picture for procedures. No mermaid: the page has no renderer, draw flows as SVG. The caption points at one feature. If you cannot draw it, the lesson is too wide: split it.

**Prose.** Simple words. Define a term at first use. One idea per sentence. Plain dashes. No filler, no catchy section labels.

**Questions.** Multiple choice: 4 options, similar length, each with a `data-why`. Numeric: `data-answer` lists every form you accept with `|` (`1/6|0.1667|0.167`); the page matches exact values, not roundings you did not list. Interview prompts ask for reasoning, not a number. Frame items in the goal's context.

## Grading on "done"

1. Get the lesson's answers from the pasted JSON block `{topic, lesson, answers: [{q, kind, value, correct, attempts}]}`. Missing block: one line, "press Copy answers at the end of the lesson and paste it here". A null `value` is a blank.
2. Sort each wrong or weak answer and respond that way:
   - **wrong model**: show where their answer parts from reality (a delta picture or a two-line trace) and ask one question. Do not give the fix. Log it under Misconceptions.
   - **slip**: point at the step, ask what it assumes.
   - **edge case**: hand over the failing input only.
3. On the page: fill the `.feedback` block under each question and remove `hidden`. Quote their key line so the page stands alone. Lesson passed: set its `data-status="passed"`, write the next lesson's section with `data-status="active"`, add its rule to the cheat sheet. Not passed: leave it active, set `data-note="redo q2"`, and rewrite the lesson's `.done` line to say which answer to redo in its box and to copy again. The script draws the strip, the sidebar, and the progress bar from those attributes.
4. Vault note: tick the lesson line, add any misconception. Commit.
5. One line in the terminal.

`stuck` and `too hard`: help level up one now, add a hint or worked step to the current lesson. `too easy`: down one. `just tell me`: put the answer on the page and move on. `next`: move on and note it.

## Help level

`data-help` on each lesson, 3 to 0. 3: worked example traced, annotated picture. 2: outline, structural picture. 1: bare prompt (a drill keeps its picture, drops the scaffolding). 0: learner defines the check. Start at 2 unless lesson 0 says otherwise. Up one on `stuck`, `too hard`, or a second miss on the same question after a redo; a first wrong model gets the redo at the same level, because a worked example on that screen would hand over the fix. Down one on `too easy` or after two first-try passes of full lessons in a row (lesson 0 does not count). Say on the page when it moves. Hold the struggle by default; "just tell me" ends it. Direct factual questions outside a challenge get a direct answer.

## Accuracy

Everything factual traces to the source. Unsure of a fact, formula, name, or policy: verify before it goes on the page, or leave it out. Never state exam policy or a claim about a tool without a source. If a check corrects what you were about to say, say so on the page.

## Hosting

Default: the page opens from disk (`open`, `xdg-open`, or the browser; if you cannot open it, give the path). Answers live in that browser's localStorage and reach you through **Copy answers**. Saving the file is the whole publish step; tell the learner to reload.

If the host has a tool that publishes an HTML file to a URL with a small shared database (Claude Code's Artifact tool), the page can sync answers between laptop and phone instead. Read `references/hosted.md` before the first publish. Pick the tier on the first publish, record it in the vault note's `page:` line, keep it for the topic.

The page stays one file: inline style and script, no external assets, fonts only from Google Fonts. Keep the template's structure; the comment at its top lists what you edit and what the script derives.

## Vault note

File it by the vault's own conventions (inside the course or project folder when one exists, else the vault root), named after the topic (add a word if an atomic note already has that name), no frontmatter, terse, wikilinks to atomic notes (existing ones, or ones the vault would want). Link it from the course hub note or `Home.md`. The page holds the answers and feedback; the note holds what carries across topics.

```
# Stars and Bars

page: ~/Learning/stars-and-bars/index.html
source: course study guide 4.9, 4.10
goal: pick the right formula on sight and compute it under a minute
related: [[stars and bars]], [[combination]]

## Lessons
- [x] 0 where you're at - 2026-09-09
- [x] 1 bars make bins - 2026-09-09, help 2, first try
- [ ] 2 pick the formula - active, help 2, redo q4
- [ ] 3 at least one each

## Misconceptions
- 2026-09-09 l01 q3: treats identical items as distinct (k^n reflex) - watch on every counting item
```
