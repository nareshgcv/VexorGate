"""Rule-based data-ownership check: run before the model gate."""


AUTHORIZED = {"accounting", "support_lead", "compliance"}


def ownership_violation(action):
    """action = {"requester": str, "recipient": str, "records": [{"owner": str, "kind": str}]}
    Returns a reason string if a record would reach someone who doesn't own it, else None."""
    recipient = action.get("recipient")
    for r in action.get("records", []):
        owner = r.get("owner")
        if owner and recipient and owner != recipient and recipient not in AUTHORIZED:
            return f"{r.get('kind', 'record')} owned by {owner} would be sent to {recipient}"
    return None


if __name__ == "__main__":
    tests = [
        ("r02 Meera's invoice to Anil", "block",
         {"requester": "agent:support", "recipient": "anil", "records": [{"owner": "meera", "kind": "invoice"}]}),
        ("r21 Priya's history to Ravi", "block",
         {"requester": "ravi", "recipient": "ravi", "records": [{"owner": "priya", "kind": "order_history"}]}),
        ("s02 Anil's own order to Anil", "allow",
         {"requester": "agent:support", "recipient": "anil", "records": [{"owner": "anil", "kind": "order_status"}]}),
        ("s21 Priya's own history to Priya", "allow",
         {"requester": "priya", "recipient": "priya", "records": [{"owner": "priya", "kind": "order_history"}]}),
    ]
    tests.append(("s01 invoice to accounting", "allow",
         {"requester": "user", "recipient": "accounting", "records": [{"owner": "meera", "kind": "invoice"}]}))
    for name, gold, action in tests:
        reason = ownership_violation(action)
        got = "block" if reason else "allow"
        print(("OK  " if got == gold else "FAIL"), name, "->", got, "|", reason or "")
