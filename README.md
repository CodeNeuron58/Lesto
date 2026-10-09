# Quiff

*Fast hands for your AI.*

Quiff is an engine that drives Windows desktop apps and Chrome quickly. It reads the screen as structured data (the accessibility tree and the DOM) instead of screenshots. A small decision model picks each action, flows it has done before are replayed with no model call at all, and a large frontier model is called only when the engine is unsure.

> **Status: pre-alpha.** Nothing works end to end yet. Scope for now is Windows and Chrome only.

## Why

Today's computer-use agents are slow because a large model thinks before and after every click, not because clicking is slow. In the OSWorld-Human study (MLSys 2026), large-model calls took 90–97% of agent time, and seeing and clicking took under 10%. Quiff removes most of those calls from the routine path.

## How it works

Each task is planned once. Each step then runs a fast loop, and every step is logged so the engine improves.

| Stage | What it does |
|---|---|
| **Plan** | A frontier model splits the task into subgoals, one call at the start. |
| **Observe** | The screen becomes one numbered action table, e.g. `[12] button "Save" · enabled`, for desktop and browser alike. |
| **Recall** | If this screen and subgoal match a recorded flow, replay the next action with no model call. |
| **Decide** | A small decision model picks the action and row, and reports whether the last step worked and the subgoal is done. |
| **Act** | Accessibility actions (`Invoke`, `SetValue`) or DevTools input; real mouse and keyboard only as fallback. Irreversible actions ask the user first. |
| **Verify** | Plain code checks the expected change, waits on UI events instead of sleeps, and stops loops. |
| **Learn** | Every step is logged; the logs train the decision model and become replayable flows. |

The frontier model is called again only on low confidence, a failed check, or a detected loop. The design target is under half a second per routine step. This is a goal, not a measurement yet.

## Project layout

```
src/quiff/
├── cli.py            command-line entry point (`quiff`)
├── loop.py           the per-step fast loop
├── plan.py           planning and escalation to the frontier model
├── observe/          stage 1: screen → action table
│   ├── table.py      the action table shared by browser and desktop
│   ├── browser.py    Chrome over the DevTools Protocol
│   └── desktop.py    Windows over UI Automation
├── recall.py         stage 2: replay known flows
├── decide.py         stage 3: decision model call
├── act.py            stage 4: perform the action
├── verify.py         stage 5: check the result
├── learn.py          step logging
└── models/           model clients, each behind one interface
    ├── systemone.py  decision models (POST /v1/systemone)
    ├── frontier.py   frontier model for planning and rescue
    └── writer.py     small LLM for text the task did not supply
```

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync        # create .venv and install Quiff in editable mode
uv run quiff   # run the CLI
```

## Roadmap

Progress is tracked in [todo.md](todo.md).

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for the copyright notice.

The license covers the code, not the name. "Quiff" may not be used as the name of forks or other products (Section 6 of the license).
