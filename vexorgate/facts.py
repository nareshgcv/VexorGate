"""Hard-fact rules: compare the user's request with the tool call in plain code.

The model never decides these. We read a few facts from the user's own words and from
the call's arguments, and compare them:

  environment  staging / production / dev / sandbox, plus "only staging" and "don't touch production"
  count        how many refunds, payments, orders, items or shares ("all" means no limit)
  amount       how much money, in which currency, or what percentage
  recipient    emails, domains and record IDs named in the request, including look-alikes
  action       a stronger action than asked: draft -> send, check -> change, pause -> delete
  audience     internal / public
  duplicates   the same money action again, judged by its economic fields and its dedupe ID
  loop         the same call repeated

Principle: the request can only narrow what is allowed, never widen it. Fixed limits
from the policy (amount caps, always-ask tools) apply whatever the request says.

Session memory (the Ledger) keeps, per session:
  - the current task: a short reply such as "yes" or "go ahead" continues the previous request
    instead of replacing it, so counts do not reset;
  - limits the user set earlier ("staging only", "don't touch production", "$40 max"), which
    last for the whole session until the user names that environment again;
  - every money action that was not blocked, with its outcome (pending, ok, failed, unknown).
    Blocked calls never ran, so they never use up the budget. A failed call is released.

Every rule that fires produces a short "gap" message, for example:
  "You asked for 1 refund. The agent wants 2 refunds, $178.00 total."
"""
import json
import re
from dataclasses import dataclass, field

from .redact import redact

ALL = "all"

# ---------------------------------------------------------------- environments

ENV_PATTERNS = {
    "production": r"(?<![a-z])(prod|production|prd)(?![a-z])",
    "staging": r"(?<![a-z])(staging|stage|stg)(?![a-z])",
    "dev": r"(?<![a-z/])(dev|development|local)(?![a-z])",
    "sandbox": r"(?<![a-z])(sandbox)(?![a-z])",
}
ENV_WORD = r"(prod|production|prd|staging|stage|stg|dev|development|sandbox)"
_ENV_GLUE = r"(?:\s+(?:touch|use|deploy|push|change|restart|modify|affect|run|go|near|hit|the|any|in|on|to|into|with|anything|update|break|deploying|touching|changing|restarting|deployments?|db|database|cluster|servers?|environment|env))*\s+"
FORBID_ENV = re.compile(r"\b(?:don'?t|do not|never|not|without|avoid|no|stay (?:out of|away from)|keep (?:out of|away from)|leave)"
                        + _ENV_GLUE + ENV_WORD + r"(?![a-z])")
ONLY_ENV = re.compile(r"\b(?:only|just)\s+(?:(?:on|in|to|the|use|touch)\s+)*" + ENV_WORD + r"(?![a-z])"
                      r"|(?<![a-z])" + ENV_WORD + r"[- ]only\b")


def _canon_env(word):
    for name, pat in ENV_PATTERNS.items():
        if re.fullmatch(pat, word.lower()):
            return name
    return None


def _envs(text):
    low = text.lower()
    return {name for name, pat in ENV_PATTERNS.items() if re.search(pat, low)}

# ---------------------------------------------------------------- money

CUR_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
CUR_WORDS = {"usd": "USD", "dollars": "USD", "dollar": "USD", "eur": "EUR", "euros": "EUR", "euro": "EUR",
             "gbp": "GBP", "pounds": "GBP", "inr": "INR", "rupees": "INR", "rs": "INR", "rs.": "INR",
             "jpy": "JPY", "yen": "JPY", "cad": "CAD", "aud": "AUD", "chf": "CHF", "sgd": "SGD", "aed": "AED"}
_NUM = r"\d(?:[\d.,\s]*\d)?"
_SYM = r"[$€£₹¥]"
_WORD = r"(?:usd|dollars?|eur|euros?|gbp|pounds|inr|rupees|rs\.?|jpy|yen|cad|aud|chf|sgd|aed)"
MONEY_RE = re.compile(r"(?:(?P<s1>" + _SYM + r")\s?(?P<n1>" + _NUM + r"))"
                      r"|(?:(?P<w1>\b" + _WORD + r")\s?(?P<n2>" + _NUM + r"))"
                      r"|(?:(?P<n3>" + _NUM + r")\s?(?P<w2>" + _WORD + r"\b|" + _SYM + r"))", re.I)


def _number(raw):
    raw = raw.strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d{1,2})?", raw):          # 1.234,56
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", raw):                          # 49,99
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _money(text):
    """All money amounts written with a currency sign or word, as (value, currency)."""
    out = []
    for m in MONEY_RE.finditer(text or ""):
        raw = m.group("n1") or m.group("n2") or m.group("n3")
        cur = m.group("s1") or m.group("w1") or m.group("w2") or ""
        x = _number(raw)
        if x is None:
            continue
        out.append((x, CUR_SYMBOLS.get(cur) or CUR_WORDS.get(cur.lower().strip())))
    return out


def _fmt_money(x, cur=None):
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥"}.get(cur or "USD")
    return f"{sym}{x:,.2f}" if sym else f"{x:,.2f} {cur}"


def _percent(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s?(?:%|percent\b)", text or "", re.I)
    if m:
        return float(m.group(1))
    if re.search(r"\bhalf\b", text or "", re.I):
        return 50.0
    return None

# ---------------------------------------------------------------- counts

NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
MONEY_NOUNS = r"(refunds?|payments?|transfers?|charges?|orders?|invoices?|payouts?)"
QTY_NOUNS = r"(items?|boxes|box|units?|shares?|seats?|licenses?|copies|copy|tickets?|contracts?|lots?)"
COUNT_NOUNS = r"(?:" + MONEY_NOUNS + r"|" + QTY_NOUNS + r")"
ALL_WORDS = r"\b(all|every|each|any)\b"

# ---------------------------------------------------------------- audience, payees, calls

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
PAYMENT_CALL = r"payments?\W{0,3}(create|submit|send)|transfers?\W{0,3}(create|send)|submit_payment|\btransfer\(|payouts?\W{0,3}create"
ORDER_CALL = r"(place|create|submit|new)_?orders?\b|orders?\W{0,3}(create|place|submit)\b|\b(buy|sell)_?(order|stock|shares?)?\b"

# Field names for an ID the provider dedupes on, and for an order size, across APIs and venues.
# Compared after lower-casing and dropping "_" and "-": client_order_id, clientOrderId and
# client-order-id are the same. Users add their own with the dedupe_keys and qty_keys policy settings.
DEDUPE_NAMES = {"idempotencykey", "clientorderid", "clientorderref", "requestid", "dedupekey", "dedupeid", "nonce",
                "cloid",                     # Hyperliquid
                "newclientorderid",          # Binance
                "clordid",                   # FIX, BitMEX
                "orderref",                  # Interactive Brokers
                "clientoid",                 # OKX, KuCoin, Bitget
                "orderlinkid",               # Bybit
                "clientorderidentifier", "clientref", "externalorderid", "userref"}
QTY_NAMES = {"qty", "quantity", "shares", "units", "count", "numshares", "size",
             "sz",                           # Hyperliquid, OKX
             "orderqty",                     # FIX, BitMEX
             "origqty",                      # Binance
             "totalquantity",                # Interactive Brokers
             "contracts", "lots", "volume", "baseqty", "basesize", "orderquantity", "filledqty"}


def _keyname(k):
    return re.sub(r"[_\-\s]", "", str(k)).lower()


def _is_dedupe(k, extra=()):
    n = _keyname(k)
    return n in DEDUPE_NAMES or n in extra


def _is_qty(k, extra=()):
    n = _keyname(k)
    return n in QTY_NAMES or n in extra


DEDUPE_KEYS = re.compile(r"^(idempotency[_-]?key|client[_-]?order[_-]?id|clientorderid|client[_-]?order[_-]?ref|"
                         r"request[_-]?id|dedupe[_-]?(key|id)|nonce|cloid|new[_-]?client[_-]?order[_-]?id|"
                         r"cl[_-]?ord[_-]?id|order[_-]?ref|client[_-]?oid|order[_-]?link[_-]?id)$", re.I)
VOLATILE_KEYS = re.compile(r"^(timestamp|ts|time|created[_-]?at|sent[_-]?at|trace[_-]?id|retry|attempt)$", re.I)
QTY_KEYS = re.compile(r"^(qty|quantity|shares|units|count|num[_-]?shares|size)$", re.I)
LIST_KEYS = re.compile(r"(ids|charges|orders|items|invoices|payments|refunds|recipients|targets|accounts|customers)$", re.I)
WILDCARD_KEYS = re.compile(r"^(filter|scope|which|target|targets|ids|selector|where|select|query)$", re.I)
TARGET_KEYS = re.compile(r"^(order|invoice|charge|customer|account|acct|payment|ticket|user|patient|record|card|last4|"
                         r"iban|payee|ref|reference|number|no)s?$|(_id|_no|_number|_ref|last4|last_four)$|^id$", re.I)
NAME_KEYS = re.compile(r"(^|_)(name|customer_name|recipient_name|to_name|full_name)$", re.I)
SENDER_KEYS = re.compile(r"^(from|sender|reply_to|from_email|sender_email)$", re.I)
AMOUNT_KEYS = re.compile(r"^(amount|amount_\w+|value|total|sum|notional|price_total)$", re.I)
CENTS_KEYS = re.compile(r"(cents|_minor|minor_units)$", re.I)
PERCENT_KEYS = re.compile(r"^(percent|percentage|pct|refund_percent)$", re.I)

EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", re.I)
DOMAIN_RE = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|io|in|org|net|co|ai|dev|app|us|uk|de|fr|biz|info|co\.uk))\b", re.I)

# ---------------------------------------------------------------- actions (weaker -> stronger)

READ_VERBS = r"\b(check|view|look|show|list|get|find|see|tell me|what|which|whether|how many|how much|status|preview|review|read|summari[sz]e|compare|explain|search|fetch|is|are|was|were|did|does|do i|can you see)\b"
WRITE_VERBS = (r"\b(fix|update|change|set|create|add|delete|remove|send|pay|refund|buy|sell|place|cancel|close|deploy|"
               r"restart|run|install|move|share|invite|grant|make|publish|post|write|edit|rename|merge|push|approve|"
               r"reply|book|schedule|order|transfer|apply|upgrade|rollback|roll back|scale|kill|stop|start|enable|disable|"
               r"pause|archive|clean|clear|reset|submit|draft|prepare|compose|email|message|notify|issue|execute|trade|"
               r"open|resolve|assign|mark|tag|upload|import|export|sync|migrate|drop|purge|revoke|rotate|charge|"
               r"credit|debit|refill|reschedule|confirm|accept|decline|reject|block|unblock|ban|suspend|tidy|organi[sz]e|sort|"
               r"cleanup|process|handle|sign|generate|build|test|commit|format|lint|refactor|implement|convert|restore|"
               r"backup|retry|trigger|launch|provision|renew|extend|settle|top up|follow up|take care|do it|go ahead)\b")
READ_START = re.compile(r"^(?:(?:please|hey|hi|can you|could you|would you|pls|quick question)\W+)*"
                        r"(what|which|who|where|when|how|is|are|was|were|does|did|do|has|have|check|show|list|tell me|"
                        r"look up|find|view|see|get me|summari[sz]e|review|preview|compare|explain|any)\b")
DRAFT_VERBS = r"\b(draft|prepare|compose|write up|write a draft)\b"
SEND_VERBS = r"\b(send|post|publish|submit|deliver|forward|tweet|share|notify)\b"
PAUSE_VERBS = r"\b(pause|disable|suspend|stop|deactivate|archive|hide|mute|turn off)\b"
DELETE_VERBS = r"\b(delete|remove|destroy|drop|purge|terminate|erase|wipe)\b"

SEND_TOKENS = {"send", "post", "publish", "submit", "deliver", "reply", "forward", "tweet", "broadcast", "notify"}
DELETE_TOKENS = {"delete", "remove", "destroy", "drop", "purge", "terminate", "erase", "wipe", "rm", "del"}
READ_TOKENS = {"get", "list", "read", "search", "fetch", "query", "describe", "view", "show", "status", "find",
               "lookup", "retrieve", "check", "preview", "count", "head", "inspect", "info"}
WRITE_TOKENS = SEND_TOKENS | DELETE_TOKENS | {
    "create", "update", "set", "put", "patch", "modify", "edit", "write", "pay", "refund", "transfer", "charge",
    "buy", "sell", "order", "place", "cancel", "close", "execute", "deploy", "restart", "scale", "grant", "invite",
    "share", "archive", "disable", "enable", "approve", "merge", "push", "rollback", "payout", "insert", "upsert",
    "trade", "book", "schedule", "issue", "revoke", "move", "rename", "upload", "apply", "submit_order"}


@dataclass
class Facts:
    envs: set = field(default_factory=set)
    count: object = None          # int, ALL, or None (not stated)
    count_noun: str = None        # the noun the count is about ("refund", "share", ...)
    amount: float = None
    currency: str = None
    percent: float = None
    audience: str = None          # "internal", "public", or None
    money_kind: str = None        # "refund", "payment" or "order" for money actions
    new_payee: bool = False
    # request-only
    forbid_envs: set = field(default_factory=set)
    only_envs: set = field(default_factory=set)
    cap_amount: float = None
    emails: set = field(default_factory=set)
    domains: set = field(default_factory=set)
    entities: set = field(default_factory=set)
    ids: set = field(default_factory=set)
    read_only: bool = False
    draft_only: bool = False
    pause_only: bool = False
    wants_limit: bool = False
    splits: int = None            # "in two orders of 5", "in 3 tranches": identical orders the user asked for
    # action-only
    qty: float = None
    list_count: int = None
    recipients: list = field(default_factory=list)
    target_ids: list = field(default_factory=list)
    names: list = field(default_factory=list)
    dedupe_id: str = None
    econ: str = None
    action_class: str = None      # "read", "write", "send", "delete" or None
    order_type: str = None


@dataclass
class RuleResult:
    decision: str                 # "allow", "ask" or "block"
    reasons: list = field(default_factory=list)
    gap: str = ""
    confirmed_recipient: bool = False   # every outside recipient was named in the request
    pending: dict = None          # what to record in the ledger once the final decision is known


# ---------------------------------------------------------------- reading the request

def _singular(noun):
    noun = noun.lower()
    if noun in ("boxes",):
        return "box"
    if noun == "copies":
        return "copy"
    return noun[:-1] if noun.endswith("s") and not noun.endswith("ss") else noun


def _money_spans(text):
    return [m.span() for m in MONEY_RE.finditer(text or "")]


def _ids(text):
    """Record IDs written in the request: numbers with 3+ digits (not money, not years, not percentages)."""
    spans = _money_spans(text)
    out = set()
    for m in re.finditer(r"(?<![\d.,])[A-Za-z]*[_#-]?(\d{3,})(?![\d.,]*\s?%)(?![.,]\d)", text or ""):
        if any(a <= m.start(1) < b for a, b in spans):
            continue
        digits = m.group(1)
        if len(digits) == 4 and digits[:2] in ("19", "20"):
            continue
        out.add(digits)
    return out


def request_facts(request):
    """Facts stated in the user's own words. Anything not clearly stated stays None."""
    text = request or ""
    low = text.lower()
    f = Facts()

    f.forbid_envs = {e for e in (_canon_env(m.group(1)) for m in FORBID_ENV.finditer(low)) if e}
    for m in ONLY_ENV.finditer(low):
        e = _canon_env(m.group(1) or m.group(2))
        if e:
            f.only_envs.add(e)
    f.envs = _envs(text) - f.forbid_envs

    if re.search(ALL_WORDS + r"[^.]{0,30}" + COUNT_NOUNS, low):
        f.count = ALL
    else:
        m = re.search(r"\b(\d+|" + "|".join(NUMBER_WORDS) + r")\s+(?:[\w-]+\s+){0,2}?" + COUNT_NOUNS + r"\b", low)
        if m and not re.match(r"\d+\s*(?:%|percent)", m.group(0)):
            word = m.group(1)
            f.count = int(word) if word.isdigit() else NUMBER_WORDS[word]
            f.count_noun = _singular(m.group(2) or m.group(3))
        else:
            m = re.search(r"\b(refund|pay|cancel|charge|process|issue|void)\b\s+(?:the\s+|my\s+|our\s+|this\s+|that\s+)?"
                          r"(order|invoice|charge|bill|payment)\s+#?\w*\d", low)
            m2 = re.search(r"\b(?:the|this|that)\s+(?:[\w-]+\s+){0,2}?(refund|payment|transfer|charge|order|invoice|payout)\b(?!s)", low)
            if m and not re.search(r"\b(orders|invoices|charges|bills|payments)\b", low):
                f.count, f.count_noun = 1, m.group(2)            # "refund order #44819"
            elif m2 and not re.search(r"\b(refunds|payments|transfers|charges|orders|invoices|payouts)\b", low):
                f.count, f.count_noun = 1, m2.group(1)           # "refund the duplicate charge"

    money = _money(text)
    caps = []
    for m in re.finditer(r"\b(?:max(?:imum)?|up to|no more than|not more than|at most|cap(?:ped)? at|limit(?:ed)? to|"
                         r"limit of|under|below|less than)\s+", low):
        rest = text[m.end():m.end() + 25]
        mm = _money(rest)
        if mm:
            f.cap_amount = mm[0][0]
            caps.append(mm[0])
    plain = list(money)
    for c in caps:
        if c in plain:
            plain.remove(c)
    if plain:
        f.amount = max(v for v, _ in plain)
        f.currency = next((c for v, c in plain if v == f.amount and c), None)
    elif caps:
        f.currency = next((c for _, c in caps if c), None)
    f.percent = _percent(text)

    if re.search(PUBLIC_REQUEST, low):
        f.audience = "public"
    elif re.search(INTERNAL_REQUEST, low):
        f.audience = "internal"

    for m in EMAIL_RE.finditer(text):
        f.emails.add(m.group(0).lower())
        f.domains.add(m.group(1).lower())
    for m in DOMAIN_RE.finditer(text):
        if "@" not in text[max(0, m.start() - 1):m.start()]:
            f.domains.add(m.group(1).lower())
    f.entities = {m.group(1).lower() for m in re.finditer(r"(?<![.!?:]\s)(?<!^)\b([A-Z][A-Za-z0-9]{3,})\b", text.strip())
                  if not re.fullmatch(WRITE_VERBS + "|" + READ_VERBS, m.group(1).lower())}
    f.entities |= {d.split(".")[0] for d in f.domains}
    f.ids = _ids(text)

    writes = re.search(WRITE_VERBS, low)
    f.read_only = bool(READ_START.match(low.strip())) and not writes
    f.draft_only = bool(re.search(DRAFT_VERBS, low)) and not re.search(SEND_VERBS, low)
    f.pause_only = bool(re.search(PAUSE_VERBS, low)) and not re.search(DELETE_VERBS, low)
    f.wants_limit = bool(re.search(r"\blimit\s+(order|price|buy|sell)\b|\b(?:at|with|using|as|via|place)\s+(?:a\s+)?limit\b|"
                                   r"\blimit\s+(?:at|@)\s*[$€£₹]?\s?\d|\blimit\b.{0,20}[$€£₹]|[$€£₹]\s?[\d.,]+\s+limit\b", low)) \
        and not re.search(r"\bat\s+market\b|\bmarket\s+order\b", low)
    m = re.search(r"\b(?:in|into|as|across|over|split\s+(?:it\s+|this\s+|that\s+)?into)\s+(\d+|" + "|".join(NUMBER_WORDS)
                  + r")\s+(?:separate\s+|equal\s+|identical\s+)?(orders|tranches|parts|batches|chunks|clips|slices|lots)\b", low) \
        or re.search(r"\b(\d+|" + "|".join(NUMBER_WORDS) + r")\s+(?:separate\s+|equal\s+|identical\s+)?"
                     r"(orders|tranches|batches|clips|slices)\s+of\s+[\d.,]+", low)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1)]
        f.splits = n if n >= 2 else None
    return f


# ---------------------------------------------------------------- reading the call

def _call_text(tool_name, tool_input, action_text):
    if action_text is not None:
        return action_text
    args = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)
    return f"{tool_name} {args}"


def _items(obj, prefix=""):
    """(key, value) for scalar args, one level into nested objects."""
    if not isinstance(obj, dict):
        return
    for k, v in obj.items():
        if isinstance(v, dict) and not prefix:
            yield from _items(v, k)
        else:
            yield str(k), v


def _tokens(name):
    name = re.sub(r"([a-z])([A-Z])", r"\1_\2", name or "")
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


def _action_class(tool_name, tool_input, text):
    if not tool_name and text:
        m = re.match(r"\s*Call `([^`]+)`", text)
        if m:
            tool_name = m.group(1)
    tokens = _tokens(tool_name)
    if isinstance(tool_input, dict):
        for k in ("method", "action", "operation", "op", "verb"):
            if isinstance(tool_input.get(k), str):
                tokens |= _tokens(tool_input[k])
    if (tool_name or "").lower() in ("bash", "shell", "terminal", "powershell"):
        cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input or "")
        if re.search(r"(^|[;&|]\s*)(rm|del|rmdir)\s", cmd) or re.search(r"\b(drop\s+table|delete\s+from)\b", cmd, re.I):
            return "delete"
        return None
    if tokens & DELETE_TOKENS:
        return "delete"
    if tokens & SEND_TOKENS:
        return "send"
    if tokens & WRITE_TOKENS:
        return "write"
    if tokens & READ_TOKENS:
        return "read"
    if tool_name == "" and text:                 # plain-text action from a benchmark or a log
        low = text.lower()
        if re.search(DELETE_VERBS, low):
            return "delete"
    return None


def action_facts(tool_name, tool_input, action_text=None, org="", dedupe_keys=(), qty_keys=()):
    dedupe_keys = {_keyname(k) for k in dedupe_keys or ()}
    qty_keys = {_keyname(k) for k in qty_keys or ()}
    text = _call_text(tool_name, tool_input, action_text)
    low = text.lower()
    f = Facts(envs=_envs(text))
    structured = isinstance(tool_input, dict) and action_text is None
    items = list(_items(tool_input)) if structured else []

    # a structured call's kind comes from its tool name, never from words inside its arguments
    # (an email with the subject "Your refund" is not a refund)
    kind_text = (tool_name or "").lower() if structured else low
    refunds = len(re.findall(REFUND_CALL, kind_text))
    payments = len(re.findall(PAYMENT_CALL, kind_text))
    orders = len(re.findall(ORDER_CALL, (tool_name or "").lower())) if tool_name else 0
    if not orders and structured and any(str(v).lower() in ("buy", "sell", "bid", "ask") for k, v in items
                                         if _keyname(k) in ("side", "action", "direction")):
        orders = 1
    if not orders and structured and any(_keyname(k) in ("isbuy", "buy") for k, _ in items):
        orders = 1                                       # Hyperliquid: is_buy
    if not orders and structured and _tokens(tool_name) & {"order", "orders"} \
            and not _tokens(tool_name) & READ_TOKENS and any(_is_qty(k, qty_keys) for k, _ in items):
        orders = 1                                       # exchange.order with a size
    if refunds:
        f.money_kind = "refund"
        f.count = ALL if re.search(ALL_WORDS, low) else refunds
    elif payments:
        f.money_kind = "payment"
        f.count = ALL if re.search(ALL_WORDS, low) else payments
    elif orders:
        f.money_kind = "order"
        f.count = 1
    elif structured:
        tokens = _tokens(tool_name)
        if tokens & {"refund", "refunds"}:
            f.money_kind, f.count = "refund", 1
        elif tokens & {"pay", "payment", "payments", "transfer", "transfers", "wire", "payout", "payouts",
                       "charge", "charges"} and not tokens & READ_TOKENS:
            f.money_kind, f.count = "payment", 1
    elif re.search(r"\brefund(s|ing)?\b", low):
        f.money_kind = "refund"
        f.count = ALL if re.search(ALL_WORDS, low) else 1
    elif re.search(r"\b(pay|transfer|wire)\b", low):
        f.money_kind = "payment"
        f.count = ALL if re.search(ALL_WORDS, low) else 1

    stripe = "stripe" in (tool_name or "").lower() or "stripe" in low
    amounts = []
    for k, v in items:
        kl = k.lower()
        if isinstance(v, bool):
            continue
        if _is_dedupe(k, dedupe_keys):
            f.dedupe_id = str(v)
        elif _is_qty(k, qty_keys) and isinstance(v, (int, float, str)):
            x = _number(str(v))
            if x is not None:
                f.qty = x
        elif PERCENT_KEYS.match(k) and isinstance(v, (int, float, str)):
            f.percent = _number(str(v))
        elif (AMOUNT_KEYS.match(k) or CENTS_KEYS.search(k)) and isinstance(v, (int, float, str)):
            if isinstance(v, str) and v.strip().lower() in ("full", "all", "max", "entire"):
                f.percent = 100.0
                continue
            x = _number(str(v)) if isinstance(v, str) else float(v)
            if x is None:
                continue
            if CENTS_KEYS.search(k) or (stripe and isinstance(v, int)):
                x = x / 100
            amounts.append(x)
        elif kl == "currency" and isinstance(v, str):
            f.currency = v.strip().upper()[:3]
        elif kl in ("type", "order_type", "ordertype") and isinstance(v, str) and v.lower() in ("market", "limit", "stop", "stop_limit"):
            f.order_type = v.lower()
        if isinstance(v, list) and LIST_KEYS.search(k) and len(v) > 0:
            f.list_count = len(v)
        if isinstance(v, str) and WILDCARD_KEYS.match(k) and (v.strip() in ("*", "%") or re.fullmatch(r"all\b.*", v.strip().lower())):
            f.count = ALL
        if isinstance(v, (str, int)) and not isinstance(v, bool) and TARGET_KEYS.search(k) and not _is_dedupe(k, dedupe_keys):
            digits = re.sub(r"\D", "", str(v))
            if len(digits) >= 3:
                f.target_ids.append((k, str(v), digits))
        if isinstance(v, str) and NAME_KEYS.search(k):
            f.names.append(v)
        if not SENDER_KEYS.match(k):
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                if isinstance(x, str):
                    f.recipients += [(m.group(0).lower(), m.group(1).lower()) for m in EMAIL_RE.finditer(x)]
    if structured and f.list_count and f.money_kind and f.count != ALL:
        f.count = f.list_count
    if f.money_kind:
        if not structured:
            amounts = []
            for m in re.finditer(r"amount\W{1,4}([\d][\d,]*(?:\.\d+)?)", text, re.I):
                raw = m.group(1).replace(",", "")
                x = float(raw)
                if stripe and "." not in raw:
                    x = x / 100
                amounts.append(x)
        text_money = _money(text) if not structured else []
        if text_money:
            amounts += [v for v, _ in text_money]
            f.currency = f.currency or next((c for _, c in text_money if c), None)
        if amounts:
            f.amount = sum(amounts) if refunds > 1 or payments > 1 else max(amounts)
        f.new_payee = bool(re.search(NEW_PAYEE, low))
    if not structured:
        f.recipients = [(m.group(0).lower(), m.group(1).lower()) for m in EMAIL_RE.finditer(text)]

    if any(re.search(p, low) for p in PUBLIC_ACTION):
        f.audience = "public"
    f.action_class = _action_class(tool_name, tool_input, text)
    if structured:
        econ = {k: v for k, v in items if not _is_dedupe(k, dedupe_keys) and not VOLATILE_KEYS.match(k)}
        f.econ = redact((tool_name or "") + " " + json.dumps(econ, sort_keys=True, default=str))
    return f


# ---------------------------------------------------------------- comparisons

def _noun(kind, n):
    if kind == "box":
        return "box" if n == 1 else "boxes"
    return kind + ("s" if n != 1 else "")


def _edit(a, b):
    if abs(len(a) - len(b)) > 3:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _lookalike(a, b):
    """Two names that are easy to confuse: clientco / clientcorp, acmee / acme."""
    if a == b or min(len(a), len(b)) < 4 or a[:3] != b[:3]:
        return False
    if a.startswith(b) or b.startswith(a):
        return abs(len(a) - len(b)) <= 4
    return _edit(a, b) <= 2


def _near_id(a, b):
    """Two record numbers that differ by swapped or mistyped digits: 1043 / 1034."""
    if a == b or len(a) != len(b) or len(a) < 3:
        return False
    return sorted(a) == sorted(b) or _edit(a, b) == 1


def _base(domain):
    return domain.split(".")[0]


def _check_recipients(req, act, org):
    """Returns (blocks, asks, confirmed)."""
    blocks, asks = [], []
    org = (org or "").lower()
    named = req.domains | req.entities
    outside = [(e, d) for e, d in act.recipients if d != org]
    confirmed = bool(outside)
    for email, dom in outside:
        base = _base(dom)
        if email in req.emails or dom in req.domains:
            continue
        swapped = sorted(d for d in req.domains if _base(d) == base)
        twin = next((n for n in sorted(named) if _lookalike(base, _base(n))), None)
        if swapped:
            blocks.append(("recipient", f"You asked for {swapped[0]}. The agent sends to {dom}, which only looks similar."))
        elif org and base == _base(org):
            asks.append(("recipient", f"{dom} looks like your own domain {org}, but it is not."))
        elif base in req.entities:
            continue
        elif twin:
            blocks.append(("recipient", f"You asked for {twin}. The agent sends to {dom}, which only looks similar."))
        elif req.emails or req.domains:
            asks.append(("recipient", f"You asked for {', '.join(sorted(req.emails or req.domains))}. The agent sends to {email}."))
        else:
            confirmed = False
    for key, raw, digits in act.target_ids:
        if digits in req.ids:
            continue
        twin = next((i for i in sorted(req.ids) if _near_id(digits, i)), None)
        if twin:
            blocks.append(("customer", f"You asked for {twin}. The agent uses {raw} ({key}), which only looks similar."))
    for name in act.names:
        words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", name)}
        if words & req.entities:
            continue
        twin = next((e for w in sorted(words) for e in sorted(req.entities) if _edit(w, e) in (1, 2) and w[0] == e[0]), None)
        if twin:
            asks.append(("customer", f"You asked about {twin.title()}. The agent uses the name {name}."))
    return blocks, asks, confirmed


def _identical_allowed(req, kind):
    """How many identical money actions the request allows: an explicit split ("in two orders of 5"),
    or a count of that kind of action ("two refunds"). Otherwise one: a second identical one is a duplicate."""
    if req.splits:
        return req.splits
    if isinstance(req.count, int) and req.count_noun and kind and req.count_noun.startswith(kind[:5]):
        return req.count
    return 1


def _so_far(asked, this, total, unit=None):
    """Gap wording that keeps the call's own size apart from the running total in this task:
    "You asked for 5 shares. This call is 5 shares, which would make 10 shares in total in this task." """
    return f"You asked for {asked}. This call is {this}, which would make {total} in total in this task."


def _check_action(req, act):
    if act.action_class is None or act.action_class == "read":
        return None
    if req.draft_only and act.action_class == "send":
        return "You asked for a draft. The agent wants to send it."
    if req.pause_only and act.action_class == "delete":
        return "You asked to pause or disable this. The agent wants to delete it."
    if req.read_only:
        what = {"send": "send something", "delete": "delete something"}.get(act.action_class, "make a change")
        if act.money_kind == "order":
            what = "place an order"
        elif act.money_kind:
            what = f"make a {act.money_kind}"
        return f"You only asked a question. The agent wants to {what}."
    return None


# ---------------------------------------------------------------- session memory

CONTINUE_RE = re.compile(r"^\s*(?:(?:yes|yeah|yep|y|ok|okay|sure|please|pls|great|thanks|thank you|good|fine|cool|right|"
                         r"alright|perfect|correct|confirmed?|approved?|agreed|lgtm|go ahead|go on|go for it|continue|"
                         r"proceed|carry on|keep going|do it|do that|try again|retry|again|sounds good|that works|"
                         r"looks good|ship it|yes please|ok do it)[\s,.!]*){1,4}$", re.I)


def is_continuation(text):
    return bool(text and len(text) <= 60 and CONTINUE_RE.match(text))


class Ledger:
    """Session memory across calls: the current task, the user's limits, and money actions.

    The state is a plain dict, so the Claude Code hook can save it between calls.
    """

    def __init__(self, state=None):
        self.state = state if state is not None else {}

    def _session(self, session):
        sessions = self.state.setdefault("sessions", {})
        return sessions.setdefault(session or "default", {"task": None, "forbid": [], "only": [], "cap": None,
                                                          "tasks": {}, "last": None})

    def resolve(self, session, request):
        """The request this call should be judged against, and the session's limits.

        A short reply ("yes", "go ahead") continues the previous request. Limits from earlier
        requests stay, unless the user names that environment again.
        """
        s = self._session(session)
        task = request or ""
        if is_continuation(request) and s["task"]:
            task = s["task"]
        elif task != s["task"]:
            f = request_facts(task)
            forbid = (set(s["forbid"]) - f.envs) | f.forbid_envs
            only = set(s["only"]) | f.only_envs if not (f.envs - f.only_envs) else f.only_envs
            s["forbid"], s["only"] = sorted(forbid), sorted(only)
            if f.cap_amount is not None:
                s["cap"] = f.cap_amount if s["cap"] is None else min(s["cap"], f.cap_amount)
            s["task"] = task
        return task, {"forbid": set(s["forbid"]), "only": set(s["only"]), "cap": s["cap"]}

    def task(self, session, task):
        s = self._session(session)
        return s["tasks"].setdefault(task[:500], {"entries": [], "calls": {}, "blocks": 0})

    def totals(self, session, task, kind, unknown=False):
        """Count, amount and quantity of the non-blocked, non-failed money actions of this kind."""
        n = amount = qty = 0
        ok = ("pending", "ok", "unknown") if unknown else ("pending", "ok")
        for e in self.task(session, task)["entries"]:
            if e["kind"] == kind and e["status"] in ok:
                n += e["n"]
                amount += e.get("amount") or 0
                qty += e.get("qty") or 0
        return n, amount, qty

    def record(self, session, pending, decision):
        """Store the call once the final decision is known. Blocked calls never ran, so they
        never use up the budget; they still count toward the repeated-call and repeated-block checks."""
        if not pending:
            return
        t = self.task(session, pending["task"])
        sig = pending["sig"]
        t["calls"][sig] = t["calls"].get(sig, 0) + 1
        s = self._session(session)
        if decision == "block":
            t["blocks"] += 1
            s["last"] = None
            return
        entry = pending.get("entry")
        if entry and not pending.get("retry_of_same_id"):
            t["entries"].append(dict(entry))
            s["last"] = [pending["task"][:500], len(t["entries"]) - 1]
        else:
            s["last"] = None

    def settle(self, session, outcome):
        """Record what happened to the last allowed money action: ok, failed or unknown.
        A failed call is released, so a corrected retry is not counted twice."""
        s = self._session(session)
        if not s.get("last") or outcome not in ("ok", "failed", "unknown"):
            return
        key, idx = s["last"]
        entries = s["tasks"].get(key, {}).get("entries", [])
        if 0 <= idx < len(entries):
            entries[idx]["status"] = outcome


def check_rules(request, tool_name, tool_input, action_text=None, org="", policy=None,
                ledger=None, session=None, commit=True):
    """Compare request facts with call facts. Returns a RuleResult (allow means no rule fired).

    commit=True stores the call in the ledger right away, using the rule decision. The Gate passes
    commit=False and records the call itself once the model has spoken, so a call the model blocks
    does not use up the budget either."""
    policy = policy or {}
    sensitive = set(policy.get("sensitive_envs", ["production"]))
    loop_limit = int(policy.get("loop_threshold", 3))
    max_amount = policy.get("max_amount")
    max_count = policy.get("max_count")
    max_blocks = int(policy.get("max_blocks", 3))
    always_ask = [t.lower() for t in policy.get("always_ask_tools", [])]
    confirm_public = policy.get("confirm_public", True)

    ledger = ledger if ledger is not None else Ledger()
    session = session or "default"
    task, limits = ledger.resolve(session, request)
    req = request_facts(task)
    act = action_facts(tool_name, tool_input, action_text, org,
                       policy.get("dedupe_keys") or (), policy.get("qty_keys") or ())
    text = _call_text(tool_name, tool_input, action_text)
    t = ledger.task(session, task)

    blocks, asks = [], []
    sig = re.sub(r"\s+", " ", text.strip().lower())[:400]
    if t["calls"].get(sig, 0) + 1 >= loop_limit:
        blocks.append(("loop", f"The agent has repeated the same action {t['calls'][sig] + 1} times."))

    # Environment
    hit = (act.envs & sensitive) - req.envs
    forbidden = act.envs & limits["forbid"]
    outside_only = (act.envs - limits["only"]) if limits["only"] else set()
    if forbidden:
        blocks.append(("environment", f"You said not to touch {' and '.join(sorted(forbidden))}. "
                                      f"The agent targets {' and '.join(sorted(forbidden))}."))
    elif outside_only and act.envs:
        blocks.append(("environment", f"You said {' and '.join(sorted(limits['only']))} only. "
                                      f"The agent targets {' and '.join(sorted(outside_only))}."))
    elif hit and req.envs:
        asked = " and ".join(sorted(req.envs))
        blocks.append(("environment", f"You asked for {asked}. The agent targets {' and '.join(sorted(hit))}."))
    elif hit:
        asks.append(("environment", f"You didn't say which environment. The agent picked {' and '.join(sorted(hit))}."))

    # Duplicates: the same money action again, by its economic fields
    kind = act.money_kind
    entry = None
    retry_same_id = False
    unverified = False
    if kind:
        n_new = act.count if isinstance(act.count, int) else 0
        entry = {"kind": kind, "n": n_new, "amount": act.amount or 0, "qty": act.qty or 0,
                 "econ": act.econ, "id": act.dedupe_id, "status": "pending"}
        if act.econ:
            same = [e for e in t["entries"] if e.get("econ") == act.econ and e["status"] != "failed"]
            if same and act.dedupe_id and any(e.get("id") == act.dedupe_id for e in same):
                retry_same_id = True                     # the provider dedupes this ID: the same order retried
            elif same and len(same) < _identical_allowed(req, kind):
                pass                                     # the user asked for this many identical orders
            elif any(e["status"] == "unknown" for e in same):
                unverified = True
                asks.append(("duplicate_unverified", f"An earlier identical {kind} in this task has no confirmed result. "
                                                     f"This could be a second {kind}."))
            elif same:
                asks.append(("duplicate", f"The agent already made the same {kind} in this task."))

    # Count
    prior_n, prior_amount, prior_qty = ledger.totals(session, task, kind) if kind and not retry_same_id else (0, 0, 0)
    if kind and isinstance(act.count, int):
        total_count = prior_n + act.count
        total_amount = prior_amount + (act.amount or 0)
    else:
        total_count, total_amount = act.count, act.amount
    total_qty = prior_qty + (act.qty or 0) if act.qty is not None else None
    rc, noun = req.count, req.count_noun
    qty_noun = bool(noun and (re.fullmatch(QTY_NOUNS, noun) or re.fullmatch(QTY_NOUNS, noun + "s")))
    if isinstance(rc, int) and qty_noun and total_qty is not None and not retry_same_id:
        if total_qty > rc and not unverified:
            blocks.append(("count", _so_far(f"{rc} {_noun(noun, rc)}", f"{act.qty:g} {_noun(noun, act.qty)}",
                                            f"{total_qty:g} {_noun(noun, total_qty)}", noun)
                           if prior_qty else f"You asked for {rc} {_noun(noun, rc)}. "
                                              f"The agent wants {total_qty:g} {_noun(noun, total_qty)}."))
    elif kind and not retry_same_id:
        if isinstance(rc, int):
            label = kind
            if act.count == ALL:
                blocks.append(("count", f"You asked for {rc} {_noun(label, rc)}. The agent wants to {kind} all of them."))
            elif isinstance(total_count, int) and total_count > rc and not unverified:
                money = f", {_fmt_money(total_amount, act.currency or req.currency)} total" if total_amount else ""
                if prior_n:
                    blocks.append(("count", f"You asked for {rc} {_noun(label, rc)}. This call is {act.count} more "
                                            f"{_noun(label, act.count)}, which would make {total_count} "
                                            f"{_noun(label, total_count)} in this task ({money[2:] or 'no amount given'})."))
                else:
                    blocks.append(("count", f"You asked for {rc} {_noun(label, rc)}. "
                                            f"The agent wants {total_count} {_noun(label, total_count)}{money}."))
        elif rc is None and act.count == ALL:
            asks.append(("count", f"You didn't say how many. The agent wants to {kind} all of them."))
    if kind and not retry_same_id:
        if max_count is not None and isinstance(total_count, int) and total_count > int(max_count):
            asks.append(("count_cap", f"{total_count} {_noun(kind, total_count)} is above your limit of {int(max_count)}."))

    # Amount
    price_only = kind == "order" and req.count_noun is not None     # "buy 10 shares at $150": a price, not a total
    if kind and not retry_same_id and not price_only:
        if req.currency and act.currency and req.currency != act.currency:
            blocks.append(("amount", f"You asked for {req.currency}. The agent uses {act.currency}."))
        elif total_amount and req.amount is not None and total_amount > req.amount * 1.005 + 0.01 and not unverified:
            cur = act.currency or req.currency
            blocks.append(("amount", _so_far(_fmt_money(req.amount, req.currency), _fmt_money(act.amount or 0, cur),
                                             _fmt_money(total_amount, cur), None)
                           if prior_amount else f"You asked for {_fmt_money(req.amount, req.currency)}. "
                                                f"The agent wants {_fmt_money(total_amount, cur)}."))
        if req.percent is not None and act.percent is not None and act.percent > req.percent + 0.01:
            blocks.append(("amount", f"You asked for {req.percent:g}%. The agent wants {act.percent:g}%."))
        if total_amount and limits["cap"] is not None and total_amount > limits["cap"] + 0.01:
            cur = act.currency or req.currency
            if prior_amount:
                blocks.append(("amount", f"You set a limit of {_fmt_money(limits['cap'], req.currency)}. This call is "
                                         f"{_fmt_money(act.amount or 0, cur)}, which would make "
                                         f"{_fmt_money(total_amount, cur)} in total in this task."))
            else:
                blocks.append(("amount", f"You set a limit of {_fmt_money(limits['cap'], req.currency)}. "
                                         f"The agent wants {_fmt_money(total_amount, cur)}."))
        if total_amount and max_amount is not None and total_amount > float(max_amount):
            asks.append(("amount_cap", f"{_fmt_money(total_amount, act.currency)} is above your limit of "
                                       f"{_fmt_money(float(max_amount))}."))

    # New payee
    if kind == "payment" and act.new_payee:
        asks.append(("new_payee", "The agent is paying a newly added recipient."))

    # Recipients and records
    r_blocks, r_asks, confirmed = _check_recipients(req, act, org)
    blocks += r_blocks
    asks += r_asks

    # Action strength
    stronger = _check_action(req, act)
    if not stronger and req.wants_limit and act.order_type == "market":
        stronger = "You asked for a limit order. The agent wants a market order."
    if stronger:
        blocks.append(("action", stronger))

    # Audience
    if act.audience == "public":
        if req.audience == "internal":
            blocks.append(("audience", "You asked to share with your team. The agent makes it public."))
        elif req.audience != "public":
            asks.append(("audience", "You didn't ask for this to be public. The agent makes it public."))
        elif confirm_public:
            asks.append(("public_confirm", "Making this public can't be undone. Please confirm."))

    # Always-ask tools
    tname = (tool_name or "").lower()
    if tname and any(t_ == tname or (t_.endswith("*") and tname.startswith(t_[:-1])) for t_ in always_ask):
        asks.append(("always_ask", f"`{tool_name}` always needs your approval."))

    # Repeated blocks: stop guessing and get a person
    if t["blocks"] >= max_blocks and not blocks:
        asks.append(("repeated_blocks", f"The agent was blocked {t['blocks']} times on this task. "
                                        f"A person should look before it continues."))

    if blocks:
        result = RuleResult("block", [f"rule:{n}" for n, _ in blocks], " ".join(g for _, g in blocks))
    elif asks:
        result = RuleResult("ask", [f"rule:{n}" for n, _ in asks], " ".join(g for _, g in asks))
    else:
        result = RuleResult("allow")
    result.confirmed_recipient = confirmed and not r_blocks and not r_asks
    result.pending = {"task": task, "sig": sig, "entry": entry, "retry_of_same_id": retry_same_id}
    if commit:
        ledger.record(session, result.pending, result.decision)
    return result
