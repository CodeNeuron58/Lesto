# Quiff

*Fast hands for your AI.*

Quiff is an engine that drives Windows desktop apps and Chrome quickly. It reads the screen as structured data (the accessibility tree and the DOM) instead of screenshots. A small decision model picks each action from a numbered table, flows it has done before are replayed with no model call at all, and a large frontier model is called only when the engine is unsure.

> **Status: hackathon build (Nov 2026).** Real and working end to end on the tasks listed below — browser and Windows desktop — with Meta's models in the loop. Pre-alpha; Windows + Chrome only.

## Why

Today's computer-use agents are slow because a large model thinks before and after every click, not because clicking is slow. In the OSWorld-Human study (MLSys 2026), large-model calls took 90–97% of agent time, and one agent took 12 minutes to change line spacing. Quiff removes most of those calls from the routine path.

## How it works

Each task is planned once. Each step then runs a fast loop, and every step is logged so the engine improves.

| Stage | What it does |
|---|---|
| **Plan** | Muse Spark (high reasoning) splits the task into subgoals — one call at the start. |
| **Observe** | The screen becomes one numbered action table, e.g. `[12] button "Save" · enabled`, read from the accessibility tree (Windows) or the DevTools protocol (Chrome). No screenshots. |
| **Recall** | If this screen and subgoal match a recorded flow, replay the next action with **no model call**. |
| **Decide** | Muse Spark (minimal reasoning) answers one structured request: which action, which row, did the last step work, is the subgoal done. |
| **Act** | Accessibility patterns (`Invoke`, `SetValue`) on Windows, DevTools input in Chrome, real mouse and keyboard as fallback. Shared flows (Save As, menus) are driver skills; irreversible actions ask the user first. |
| **Verify** | Plain code checks the expected change and waits on evidence, never trusting the model's own "done". |
| **Learn** | Every step is logged (JSONL); successful runs become replayable flows. |

The frontier model is called again only on low confidence, a failed check, or a detected loop.

## The Meta models in the loop

- **Muse Spark** (`muse-spark-1.3`, Meta Model API) — the decision model: one JSON-schema-structured call per step picks the action and row from the numbered table. Measured from India: **~1.5 s median decision at minimal reasoning, 100% schema-valid, 100% correct row** on the go/no-go benchmark (`spikes/model_probe.py`).
- **Muse Spark (high reasoning)** — plans the subgoals and rescues the loop when confidence drops, an action fails twice, or the same screen repeats.
- **Muse Spark Open** (`meta-models/muse-spark-open-1026`, Baseten) — the same decision interface over the open-weights model; one constructor swaps it in.

The insight the build validated: **more reasoning made row-picking worse** (low effort: 55% valid JSON vs minimal's 100%) — narrow, well-structured decisions want speed, and the verification layer catches the rest.

## Results (live runs, Nov 1 2026, dev machine, India)

| Task | What the model did | Steps | Time | Verdict |
|---|---|---|---|---|
| b05 site login | typed credentials, submitted | 3 | 18.7 s | PASS (2 code checks) |
| b01 Google search | typed query, entered | 3 | 13.3 s | PASS (URL check) |
| b03 research task | navigated Wikipedia, read, answered "1991" | 3 | 19.5 s | PASS (answer check) |
| b04 order form | filled 6 fields in batches, hit the approval guard on "Submit order", submitted after approval | 8 | 19.1 s | PASS (3 page checks) |
| d01 Notepad save (desktop) | typed the document, drove the Save-As dialog via a driver skill | 2 | 20.0 s | PASS (file check) |
| d01 replay | same task, **0 model calls** | 2 | 11.1 s | PASS |

Against a faithful screenshot-agent stand-in (screenshot + one Muse vision call per step, pixel clicks — how today's frontier agents work), the telling difference isn't just speed: on the login task the screenshot agent **declared done without the login happening** — Quiff's code checks caught it. Deciding from the action table also cost ~1.5 s per step instead of a vision call, and replay costs nothing.

Honest caveats: these are 1–3 runs on a dev machine, not a benchmark; Google rate-limits headless automation (headed works); the desktop tasks run only when no other windows of the same app are open.

## Try it

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Windows, and Chrome.

```sh
uv sync
echo META_API_KEY=your-key > .env      # from ai.developer.meta.com
uv run quiff tasks                     # list the 20-task suite
uv run quiff run b05 --headed          # the model logs into a public test site
uv run quiff run b04 --interactive     # the approval guard asks before "Submit order"
uv run quiff run d01                   # records a replayable flow (run once first)
uv run quiff run d01 --replay          # then replay it: no model calls, machine speed
uv run python spikes/model_probe.py    # the go/no-go benchmark yourself
```

`quiff run` drives a real browser window; every step is logged to `runs/` as JSONL. The 20-task suite (`tasks/`) has dev and hidden splits, automatic checks (10 check types), and a sandboxed desktop folder per task.

## What's real vs what's next

Working today: browser observe→decide→act→verify with Muse Spark; Windows observe/act over UI Automation with the same loop; driver skills for Save-As dialogs and menus; approval guard for irreversible actions; rescue escalation; JSONL step logging; flow recording and replay with zero model calls.

Next: more of the 20 tasks green, event-driven waits (instead of bounded polls), a speed leaderboard, and the trained decision model (the logs are the training set).

## Built during the hackathon

Everything substantive after the `pre-hackathon` tag — the entire engine, the model layer, the task suite, and the probe — was built during the Meta Global AI Developer Hackathon (Oct 27 – Nov 4, 2026). Compare: [pre-hackathon...main](https://github.com/CodeNeuron58/quiff/compare/pre-hackathon...main). (Before the tag: only the package skeleton, README, and license.)

## License

Licensed under the [Apache License, 2.0](LICENSE). See [NOTICE](NOTICE) for the copyright notice.

The license covers the code, not the name. "Quiff" may not be used as the name of forks or other products (Section 6 of the license).
