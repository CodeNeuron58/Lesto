"""The go/no-go test: is Muse Spark fast and reliable enough to pick rows?

    uv run python spikes/model_probe.py            # needs META_API_KEY in .env
    uv run python spikes/model_probe.py --efforts minimal low --calls 10

Measures, per reasoning effort: median latency of small calls, then structured-
output reliability on a row-picking question with a known right answer
(% valid JSON, % correct row, median decision latency). Writes runs/model-probe-*.json.

Verdict rule from the plan: median decision <= ~2 s -> full plan; slower ->
the demo leans on replay and form batching.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from quiff.models.meta import DECISION_SCHEMA, muse_spark, muse_spark_open
from quiff.observe.table import ActionRow, ActionTable

CLICK_ROLES = {"link", "button"}


def probe_table() -> ActionTable:
    """A small synthetic Google-shaped table. The right answer is row 4."""
    rows = [
        ("link", "About"), ("link", "Gmail"), ("link", "Images"),
        ("textbox", "Search"), ("button", "Search by voice"), ("button", "Search by image"),
        ("button", "Google Search"), ("button", "I'm Feeling Lucky"), ("link", "Sign in"),
        ("button", "Settings"), ("link", "Privacy"), ("link", "Terms"),
    ]
    return ActionTable(
        title="Google", url="https://www.google.com/",
        rows=tuple(
            ActionRow(
                index=i, role=role, name=name,
                actions=("click",) if role in CLICK_ROLES else ("type",),
                enabled=True, ref=f"{role}='{name}'",
            )
            for i, (role, name) in enumerate(rows, 1)
        ),
    )

ROW_PICK_PROMPT = (
    "Goal: {goal}\n"
    "Current subgoal: {subgoal}\n\n"
    "Action table:\n{table}\n\n"
    "Last two actions taken:\n(none yet)\n"
    "Result of the last action: (the last step has not happened yet)\n\n"
    "Reply with JSON only: pick the ONE next action. The user asked to {task}. "
    "Choose the correct row and action. Confidence between 0 and 1."
)


def _percent(values: list) -> float:
    return round(100 * sum(values) / len(values), 1) if values else 0.0


def run(client, efforts: list[str], calls: int) -> dict:
    results = {"model": client.model, "efforts": {}}
    table = probe_table()
    for effort in efforts:
        # 1) plain latency: tiny prompts
        latencies = []
        errors = 0
        for _ in range(calls):
            try:
                reply = client.chat("Reply with the single word: ok", effort=effort)
                latencies.append(reply.latency_ms)
            except Exception:
                errors += 1
        # 2) structured row picking: 20 calls, known correct answer (row 4)
        picks, correct, valid, decision_ms = [], 0, 0, []
        for i in range(20):
            goal = "Search Google for 'langchain'."
            prompt = ROW_PICK_PROMPT.format(
                goal=goal, subgoal=goal, table=table.render(),
                task="search Google for 'langchain' by typing into the search box",
            )
            try:
                parsed, reply = client.structured(
                    prompt, DECISION_SCHEMA, name="decision", effort=effort
                )
                decision_ms.append(reply.latency_ms)
                valid += 1
                picks.append(parsed.get("row"))
                if parsed.get("action") == "type" and parsed.get("row") == 4:
                    correct += 1
            except Exception:
                picks.append(None)
        entry = {
            "latency_ms": sorted(latencies),
            "latency_median_ms": statistics.median(latencies) if latencies else None,
            "errors": errors,
            "valid_json_pct": _percent([p is not None for p in picks]) if picks else 0,
            "correct_row_pct": _percent([p == 4 for p in picks]),
            "decision_median_ms": statistics.median(decision_ms) if decision_ms else None,
        }
        results["efforts"][effort] = entry
        print(f"[{effort:>7}] latency median {entry['latency_median_ms']} ms"
              f" · decision median {entry['decision_median_ms']} ms"
              f" · valid JSON {entry['valid_json_pct']}%"
              f" · correct row {entry['correct_row_pct']}%"
              f" · errors {errors}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--efforts", nargs="+", default=["minimal", "low", "high"])
    parser.add_argument("--calls", type=int, default=10, help="latency calls per effort")
    parser.add_argument("--open", action="store_true",
                        help="test Muse Spark Open on Baseten instead of Muse Spark")
    args = parser.parse_args()

    client = muse_spark_open() if args.open else muse_spark()
    print(f"Probing {client.model} ...")
    results = run(client, args.efforts, args.calls)

    best = min(
        (e["decision_median_ms"] for e in results["efforts"].values()
         if e["decision_median_ms"] and e["correct_row_pct"] >= 80),
        default=None,
    )
    verdict = ("GO: fast enough for the full plan"
               if best is not None and best <= 2000
               else "CAUTION: decision latency over 2 s — demo leans on replay + form batching")
    results["verdict"] = verdict
    print(f"\nVERDICT: {verdict}")

    out = Path("runs") / f"model-probe-{time.strftime('%Y-%m-%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
