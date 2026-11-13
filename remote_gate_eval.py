"""Run the frozen gates against a hosted System One endpoint (clean accuracy only)."""
import argparse, json, os, sys, urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_audit_frozen as g

URL, KEY = os.environ["S1_URL"], os.environ["S1_KEY"]


def make_run(model):
    def run(state, qs):
        body = json.dumps({"model": model, "state": state, "questions": qs}).encode()
        req = urllib.request.Request(URL, data=body, headers={
            "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            sys.exit(f"HTTP {e.code}: {e.read().decode()[:800]}")
    return run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", default="eval/gate_sets/dev_fresh.jsonl")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    run = make_run(a.model)
    cases = g.load(a.data)

    if a.probe:
        qs = {"gate": {"type": "choice", "instructions": g.INSTR, "criteria": g.CRIT}}
        print(json.dumps(run(g.ORG + g.get_text(cases[0]), qs), indent=2)[:1500])
        return

    summary = {}
    for mode in ("single", "hybrid", "hardened"):
        ok, misses = 0, []
        for c in cases:
            v = g.decide(run, g.ORG + g.get_text(c), mode)
            gold = g.get_gold(c)
            good = (v == "allow") if gold == "allow" else (v in ("block", "ask"))
            ok += good
            if not good:
                misses.append(f"{c['id']} gold {gold} got {v}")
        summary[mode] = ok
        print(f"== {a.model} / {mode}: {ok}/{len(cases)} correct")
        print("   misses:", ", ".join(misses) or "none")
    os.makedirs("Vexorgate/results", exist_ok=True)
    name = a.model.replace("/", "_").replace("~", "")
    json.dump(summary, open(f"/VexorGate/results/remote_{name}.json", "w"))


if __name__ == "__main__":
    main()
