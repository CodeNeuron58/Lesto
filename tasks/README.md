# Task suite

The fixed set of tasks every speed and success claim is measured on. Each task has an automatic pass/fail check, so results never depend on someone's judgement.

**Status:** first 20 tasks (10 browser, 10 desktop). These are generic stand-ins built on public test sites and a sandbox folder. Real tasks from people's everyday computer work will be added after user interviews, and the set will grow to 50 (25 browser, 25 desktop).

## Rules

- **Freeze before building.** Tasks don't change to make Quiff look good. If a task is broken, fix the task and note why in git history.
- **Two splits.**
  - `dev`: we may look at these runs and tune on them.
  - `hidden`: only run at gates and for published numbers. Never tune thresholds or prompts on them, and never put their runs into training data.
- **Each task is run 5 times;** we report success rate, median and p95 time to success.
- **Desktop tasks only touch the sandbox** `~/quiff-tasks/<task id>/`. The runner recreates that folder from `setup` before every run.
- **No real accounts or private data.** Browser tasks use public sites. The login task uses a public test site whose credentials are printed on the page.

## Format

Tasks live in [`tasks.toml`](tasks.toml). One task:

```toml
[[task]]
id = "d03-rename-file"           # b = browser, d = desktop
app = "explorer"
split = "dev"                    # dev or hidden
goal = "Rename draft.txt to final.txt."
start = { app = "explorer", folder = "." }   # where the run begins
notes = "Optional context."

[task.setup]                     # desktop only: recreated before each run
dirs = ["archive"]
[task.setup.files]
"draft.txt" = "Quarterly report draft"

[[task.check]]                   # every check must pass
type = "file"
path = "final.txt"

[[task.check]]
type = "missing"
path = "draft.txt"
```

Paths in desktop checks are relative to the task's sandbox folder unless they start with `~`.

## Check types

| Type | Passes when | Fields |
|---|---|---|
| `url` | The active tab's URL matches a regex | `pattern` |
| `page_text` | The active page contains the text | `contains` |
| `dom` | A page element's property has a value | `selector`, `property`, `equals` |
| `answer` | Quiff's final answer matches a regex | `pattern` |
| `file` | A file exists, optionally with exact or partial content | `path` or `glob`, `equals`, `contains`, `created_after_start` |
| `missing` | A path does not exist | `path` |
| `dir` | A folder exists | `path` |
| `csv` | A CSV file's rows equal the expected rows | `path`, `rows` (or `last_row`) |
| `zip` | A zip file contains files with these names | `path`, `contains_files` |
| `window_title` | Some top-level window's title matches a regex | `pattern` |

The checker that runs these comes with the first loop. Until then, this file is the specification.

## Adding a real task

1. Write the goal the way the user would say it.
2. Pick a check that a computer can verify. If you can't think of one, the task isn't ready.
3. Use made-up data, never a customer's real data.
4. Choose the split before running it, and don't move tasks between splits afterwards.
