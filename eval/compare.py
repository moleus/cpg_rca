"""Compare coverage snapshots; reject losses and a smaller measured graph.

Usage: python3 -m eval.compare OLD.json NEW.json

Totals and every service of OLD are checked, on union and pooled counts.
Entrypoint, log-point and reachable counts must not decrease. A smaller FLOW
can lower reachable and lost together without recovering a single log.
Use the same source and builder options. An intentional reduction needs review
before it becomes a new baseline. Counts do not prove call-site identity.
"""

import json
import sys
from pathlib import Path

SHOWN = ("entrypoints", "log_points", "reachable", "visible", "lost",
         "pooled_reachable", "pooled_visible", "pooled_lost")
MUST_NOT_DECREASE = ("entrypoints", "log_points", "reachable", "visible",
                     "pooled_reachable", "pooled_visible")


def main(argv):
    old, new = (json.loads(Path(p).read_text(encoding="utf-8")) for p in argv)
    rows = [("total", old["totals"], new["totals"])]
    rows += [(name, s, new["services"].get(name)) for name, s in old["services"].items()]
    regressed = False
    print("| service | " + " | ".join(SHOWN) + " | status |\n|" + "---|" * (len(SHOWN) + 2))
    for name, o, n in rows:
        if n is None:
            regressed = True
            print(f"| {name} | " + " | ".join(f"{o[k]} -> missing" for k in SHOWN)
                  + " | MISSING |")
            continue
        bad = any(n[k] < o[k] for k in MUST_NOT_DECREASE) or any(
            n[k] > o[k] for k in ("lost", "pooled_lost"))
        regressed |= bad
        print(f"| {name} | " + " | ".join(f"{o[k]} -> {n[k]}" for k in SHOWN)
              + f" | {'REGRESSION' if bad else 'ok'} |")
    return int(regressed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
