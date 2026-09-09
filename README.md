# learn

A Claude Code skill that tutors one concept at a time.

Each topic gets a single web page: picture first, few words, then questions with answer boxes. Concept lessons end with questions you answer in your own words and the tutor grades. Drills check themselves as you go. The page is published as a Claude artifact so it opens on your laptop and your phone, and your answers sync between them. A terse note in your Obsidian vault keeps the record.

## Install

```
rm -rf ~/.claude/skills/learn
ln -s ~/Scripts/learn-skill ~/.claude/skills/learn
```

Paths are in `SKILL.md`: pages under `~/Learning/<topic>/`, notes in `~/Obsidian Vault/`. Edit to match your setup.

## Use

```
/learn stars and bars, source ~/Documents/WGU/C960/C960_Discrete_Math_II_Study_Guide.md
```

The tutor asks for a goal if it is unclear, plans 3 to 6 lessons, writes lesson 0 (where you're at) and lesson 1, publishes the page, and opens it.

Do the lesson on the page, then say one word in the terminal:

| word | what happens |
|---|---|
| `done` | your answers are graded, feedback appears under each question, the next lesson unlocks |
| `stuck` / `too hard` | more help now: a hint or a worked step is added to the current lesson |
| `too easy` | less help from here on |
| `just tell me` | the answer, no argument |
| `next` | move on |

Come back any time with `/learn <topic>`; it picks up at the active lesson.

## Files

- `SKILL.md` - the skill
- `template.html` - the topic page template, with a worked example (stars and bars) showing a probe, a passed concept lesson with feedback, an active drill, cheat sheet, glossary, and the how-it-works page
