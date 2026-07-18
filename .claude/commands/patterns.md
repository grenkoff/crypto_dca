---
description: Review recent changes for GoF design-pattern opportunities (and over-patterning). Advisory, non-blocking.
---

Review the current change set for **Gang of Four design-pattern** fit — where
a pattern would genuinely help, and where an existing/added pattern is
unnecessary. This is a **judgement pass, not a gate**: it produces
suggestions, it never blocks. Run it after `/qa` is green.

## Scope

Look only at what changed on this branch, not the whole repo:

- `git diff main...HEAD` for committed work, plus `git diff` / `git diff
  --staged` for uncommitted changes.
- Ignore `tests/**` and `**/migrations/**` unless the change adds real
  production logic there.

## How to judge

The bar is high — recommend a pattern **only** when it removes real
duplication, decouples a real seam, or tames conditional logic that will keep
growing. **KISS and YAGNI break every tie. Rule of Three**: don't suggest
abstracting until the third concrete case exists. Prefer the lightweight
Python idiom (first-class functions, dict dispatch, `@dataclass`, context
manager, duck typing) over a class hierarchy whenever it still scales.

For each significant hunk, check both directions:

**Opportunity** — does a smell appear?

| Smell | Pattern |
|---|---|
| growing `if/elif` on a type/mode selecting behavior | Strategy (or dict of callables) |
| repeated conditional object construction | Factory Method / named `from_*` ctor |
| gluing an incompatible external API to ours | Adapter |
| cross-cutting wrapping of a client (dry-run/retry/log) | Decorator/Proxy |
| callers reaching into a subsystem's internals | Facade |
| one action must notify N unrelated reactions | Observer / pub-sub |
| behavior depends on a lifecycle status with transitions | State |
| near-identical procedures differing in one step | Template Method |
| operations needing undo/queue/replay/log | Command |

**Over-patterning** — flag and recommend removal: a Strategy with one
strategy, a Factory making one thing, an interface with one impl, indirection
with a single caller, a pattern added "for future flexibility" (YAGNI).

## Output

A short, ranked list. For each item:

- **file:line** and which pattern.
- One sentence on the smell (or the over-engineering).
- A concrete **before → after** sketch (a few lines), and the trade-off — why
  it's worth it *here*, or why the lighter idiom wins.

If nothing is warranted, say so plainly: **"No pattern changes warranted —
the change is appropriately simple."** Do not invent suggestions to fill the
list; recommending *no change* is the correct answer when the code is already
the simplest thing that works.
