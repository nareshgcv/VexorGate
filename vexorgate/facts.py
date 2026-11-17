"""Hard-fact rules: compare the user's request with the tool call in plain code.

The model never decides these. We read a few facts from the user's own words and from
the call's arguments, and compare them:

  environment  staging / production / dev / test, or not stated
  count        how many refunds or payments, or not stated ("all" means no limit)
  amount       how much money, or not stated
  audience     internal / public, or not stated
  recipient    whether money goes to a newly added payee
  loop         the same call on the same target repeated

Principle: the request can only narrow what is allowed, never widen it. Fixed limits
from the policy (amount caps, always-ask tools) apply whatever the request says.

Every rule that fires produces a short "gap" message that shows what was asked and
what the agent is about to do, for example:
  "You asked for 1 refund. The agent wants 2 refunds, $178.00 total."
"""
import json
import re
from dataclasses import dataclass, field

ALL = "all"

ENV_PATTERNS = {
    "production": r"(?<![a-z])(prod|production|prd)(?![a-z])",
    "staging": r"(?<![a-z])(staging|stage|stg)(?![a-z])",
    "dev": r"(?<![a-z/])(dev|development|local)(?![a-z])",
    "sandbox": r"(?<![a-z])(sandbox)(?![a-z])",
}

NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
MONEY_NOUNS = r"(refunds?|payments?|transfers?|charges?|orders?|invoices?)"
ALL_WORDS = r"\b(all|every|each|any)\b"

PUBLIC_REQUEST = r"\b(public|publicly|publish\w*|post\w* (?:it )?on (?:x|twitter|linkedin)|anyone with the link|the whole internet|everyone on the internet)\b"
INTERNAL_REQUEST = r"\b(team|teammates?|internal(ly)?|colleagues?|coworkers?|company|private)\b"

PUBLIC_ACTION = [
    r"type\W{0,4}anyone",                      # Google Drive permission for anyone
    r"visibility\W{0,4}public",                # GitHub / GitLab repository visibility
    r"\bpublic[-_ ]read\b",                    # S3 canned ACL
    r"public_?access_?block\W.{0,60}\bfalse\b",  # S3 public access block switched off
    r"block_?public\w*\W{0,4}false",
    r"\bacl\W{0,4}public",
    r"\bmake\w*\W+.{0,40}\bpublic\b",
    r"\bpublish(ed|es)?\b",
]

NEW_PAYEE = (r"\bnew(ly)?([- ]added)?\s+(?:\w+[\s/]+){0,2}?(payee|beneficiary|recipient|account|routing)"
             r"|(payee|beneficiary|recipient|account)\W+(?:that\s+|which\s+)?(?:was\s+|were\s+)?(?:just\s+)?added")
REFUND_CALL = r"refunds?\W{0,3}(create|issue)|create_refund|issue_refund|\brefund\("
PAYMENT_CALL = r"payments?\W{0,3}(create|submit|send)|transfers?\W{0,3}(create|send)|submit_payment|\btransfer\("


@dataclass
class Facts:
    envs: set = field(default_factory=set)
    count: object = None          # int, ALL, or None (not stated)
    amount: float = None
    audience: str = None          # "internal", "public", or None
    money_kind: str = None        # "refund" or "payment" for money actions
    new_payee: bool = False


@dataclass
class RuleResult:
    decision: str                 # "allow", "ask" or "block"
    reasons: list = field(default_factory=list)
    gap: str = ""


def _money(text):
    """All money amounts written with a currency sign or word, as floats."""
    out = []
    for m in re.finditer(r"(?:[$€£₹]\s?([\d][\d,]*(?:\.\d+)?)|([\d][\d,]*(?:\.\d+)?)\s?(?:usd|dollars|eur|euros|inr|rupees))",
                         text, re.I):
        raw = (m.group(1) or m.group(2)).replace(",", "")
        try:
            out.append(float(raw))
        except ValueError:
            pass
    return out


def _envs(text):
    low = text.lower()
    return {name for name, pat in ENV_PATTERNS.items() if re.search(pat, low)}


def request_facts(request):
    """Facts stated in the user's own words. Anything not clearly stated stays None."""
    text = request or ""
    low = text.lower()
    f = Facts(envs=_envs(text))

    if re.search(ALL_WORDS + r"[^.]{0,30}" + MONEY_NOUNS, low):
        f.count = ALL
    else:
        m = re.search(r"\b(\d+|" + "|".join(NUMBER_WORDS) + r")\s+(?:\w+\s+){0,2}?" + MONEY_NOUNS, low)
        if m:
            word = m.group(1)
            f.count = int(word) if word.isdigit() else NUMBER_WORDS[word]
        elif re.search(r"\b(refund|pay)\b\s+(?:the\s+|my\s+|our\s+)?(order|invoice|charge|bill)\s+#?\w*\d", low) \
                and not re.search(r"\b(orders|invoices|charges|bills)\b", low):
            f.count = 1                                           # "refund order #44819"

    amounts = _money(text)
    if amounts:
        f.amount = max(amounts)

    if re.search(PUBLIC_REQUEST, low):
        f.audience = "public"
    elif re.search(INTERNAL_REQUEST, low):
        f.audience = "internal"
    return f


def _call_text(tool_name, tool_input, action_text):
    if action_text is not None:
        return action_text
    args = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)
    return f"{tool_name} {args}"


def _arg_amounts(tool_name, tool_input, text):
    """Amounts from structured args (Stripe uses the smallest unit, e.g. cents) and from text."""
    vals = []
    stripe = "stripe" in (tool_name or "").lower() or "stripe" in text.lower()
    if isinstance(tool_input, dict):
        for k, v in tool_input.items():
            if re.fullmatch(r"amount(_\w+)?", str(k), re.I) and isinstance(v, (int, float, str)):
                try:
                    x = float(str(v).replace(",", ""))
                except ValueError:
                    continue
                vals.append(x / 100 if stripe and isinstance(v, int) else x)
    for m in re.finditer(r"amount\W{1,4}([\d][\d,]*(?:\.\d+)?)", text, re.I):
        raw = m.group(1).replace(",", "")
        x = float(raw)
        if stripe and "." not in raw:
            x = x / 100
        vals.append(x)
    vals += _money(text)
    return vals


def action_facts(tool_name, tool_input, action_text=None, org=""):
    text = _call_text(tool_name, tool_input, action_text)
    low = text.lower()
    f = Facts(envs=_envs(text))

    refunds = len(re.findall(REFUND_CALL, low))
    payments = len(re.findall(PAYMENT_CALL, low))
    if refunds:
        f.money_kind = "refund"
        f.count = ALL if re.search(ALL_WORDS, low) else refunds
    elif payments:
        f.money_kind = "payment"
        f.count = ALL if re.search(ALL_WORDS, low) else payments
    elif re.search(r"\brefund(s|ing)?\b", low):
        f.money_kind = "refund"
        f.count = ALL if re.search(ALL_WORDS, low) else 1
    elif re.search(r"\b(pay|transfer|wire)\b", low):
        f.money_kind = "payment"
        f.count = ALL if re.search(ALL_WORDS, low) else 1

    if f.money_kind:
        amts = _arg_amounts(tool_name, tool_input, text)
        if amts:
            f.amount = sum(amts) if refunds > 1 or payments > 1 else max(amts)
        f.new_payee = bool(re.search(NEW_PAYEE, low))

    if any(re.search(p, low) for p in PUBLIC_ACTION):
        f.audience = "public"
    return f


def _fmt_money(x):
    return f"${x:,.2f}"


def _noun(kind, n):
    return kind + ("s" if n != 1 else "")


class Ledger:
    """Remembers money totals and repeated calls per session and request, across calls."""

    def __init__(self, state=None):
        self.state = state if state is not None else {}

    def _bucket(self, session, request):
        key = f"{session}␟{request}"
        return self.state.setdefault(key, {"count": {}, "amount": {}, "calls": {}})

    def add(self, session, request, facts, signature):
        b = self._bucket(session, request)
        if facts.money_kind and isinstance(facts.count, int):
            b["count"][facts.money_kind] = b["count"].get(facts.money_kind, 0) + facts.count
            if facts.amount:
                b["amount"][facts.money_kind] = b["amount"].get(facts.money_kind, 0.0) + facts.amount
        b["calls"][signature] = b["calls"].get(signature, 0) + 1
        return b


def check_rules(request, tool_name, tool_input, action_text=None, org="", policy=None,
                ledger=None, session=None):
    """Compare request facts with call facts. Returns a RuleResult (allow means no rule fired)."""
    policy = policy or {}
    sensitive = set(policy.get("sensitive_envs", ["production"]))
    loop_limit = int(policy.get("loop_threshold", 3))
    max_amount = policy.get("max_amount")
    always_ask = [t.lower() for t in policy.get("always_ask_tools", [])]

    req = request_facts(request)
    act = action_facts(tool_name, tool_input, action_text, org)
    text = _call_text(tool_name, tool_input, action_text)

    blocks, asks = [], []

    # Totals across this request's calls in this session
    total_count, total_amount = act.count, act.amount
    if ledger is not None:
        sig = re.sub(r"\s+", " ", text.strip().lower())[:400]
        b = ledger.add(session or "default", request or "", act, sig)
        if act.money_kind and isinstance(act.count, int):
            total_count = b["count"].get(act.money_kind, act.count)
            total_amount = b["amount"].get(act.money_kind, act.amount)
        if b["calls"][sig] >= loop_limit:
            blocks.append(("loop", f"The agent has repeated the same action {b['calls'][sig]} times."))

    # Environment: only a sensitive environment the request didn't name is a problem
    hit = (act.envs & sensitive) - req.envs
    if hit and req.envs:
        asked = " and ".join(sorted(req.envs))
        blocks.append(("environment", f"You asked for {asked}. The agent targets {' and '.join(sorted(hit))}."))
    elif hit:
        asks.append(("environment", f"You didn't say which environment. The agent picked {' and '.join(sorted(hit))}."))

    # Count
    if act.money_kind:
        kind = act.money_kind
        if req.count not in (None, ALL):
            if act.count == ALL:
                blocks.append(("count", f"You asked for {req.count} {_noun(kind, req.count)}. "
                                        f"The agent wants to {kind} all of them."))
            elif isinstance(total_count, int) and total_count > req.count:
                money = f", {_fmt_money(total_amount)} total" if total_amount else ""
                blocks.append(("count", f"You asked for {req.count} {_noun(kind, req.count)}. "
                                        f"The agent wants {total_count} {_noun(kind, total_count)}{money}."))
        elif req.count is None and act.count == ALL:
            asks.append(("count", f"You didn't say how many. The agent wants to {kind} all of them."))

    # Amount
    if act.money_kind and total_amount:
        if req.amount is not None and total_amount > req.amount * 1.005 + 0.01:
            blocks.append(("amount", f"You asked for {_fmt_money(req.amount)}. "
                                     f"The agent wants {_fmt_money(total_amount)}."))
        if max_amount is not None and total_amount > float(max_amount):
            asks.append(("amount_cap", f"{_fmt_money(total_amount)} is above your limit of "
                                       f"{_fmt_money(float(max_amount))}."))

    # New payee
    if act.money_kind == "payment" and act.new_payee:
        asks.append(("new_payee", "The agent is paying a newly added recipient."))

    # Audience
    if act.audience == "public" and req.audience != "public":
        if req.audience == "internal":
            blocks.append(("audience", "You asked to share with your team. The agent makes it public."))
        else:
            asks.append(("audience", "You didn't ask for this to be public. The agent makes it public."))

    # Always-ask tools
    tname = (tool_name or "").lower()
    if tname and any(t == tname or (t.endswith("*") and tname.startswith(t[:-1])) for t in always_ask):
        asks.append(("always_ask", f"`{tool_name}` always needs your approval."))

    if blocks:
        return RuleResult("block", [f"rule:{n}" for n, _ in blocks], " ".join(g for _, g in blocks))
    if asks:
        return RuleResult("ask", [f"rule:{n}" for n, _ in asks], " ".join(g for _, g in asks))
    return RuleResult("allow")
