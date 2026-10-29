#!/usr/bin/env python3
"""Generate the n8n Gmail Classifier workflows from a local config.

    python3 n8n/build.py [--config n8n/config.local.json] [--out n8n/dist] [--check]

The importable workflows in n8n/workflows/ come from the example config:
    python3 n8n/build.py --config n8n/config.example.json --out n8n/workflows

Writes:
  gmail-classifier.json       production workflow (Gmail Trigger → whitelist → cache → Jev → Mistral fallback → label)
  gmail-classifier-corrections.json  hourly: logged label vs current Gmail label → corrected / confirmed / removed;
                                     flagged mixed senders → llm Sender Rules

Nothing account-specific is baked in: Gmail labels are resolved by name at run time, Data Tables are referenced
by name, and credentials are optional (set them once in n8n; `n8n.py deploy` keeps them on later updates).
"""
import argparse
import json
import re
import sys
import uuid
from pathlib import Path

JEV_MODEL = "jev-1.13.0"          # pinned: confidence thresholds are tuned against one model version
MISTRAL_MODEL = "mistral-medium-latest"
CONFIDENCE_THRESHOLD = 0.6        # below → Mistral fallback
SWEEP_EVERY_MINUTES = 15          # catch-up for emails the Gmail Trigger skipped (late indexing)
SWEEP_WINDOW = "newer_than:2h"
SWEEP_SETTLE_MS = 5 * 60 * 1000   # leave fresh emails to the Gmail Trigger
BODY_MAX_CHARS = 6000
HISTORY_MAX = 5                   # past corrections of the same sender shown to Jev and Mistral

# Priority order = order of this dict. Descriptions are Jev's Choice criteria and Mistral's definitions.
# "Awaiting reply" is not offered: it describes a thread where the user sent the last message, never an incoming email.
CATEGORIES = {
    "To respond": "Requires a reply or an action from the recipient: a named person writes directly to the recipient by email (even from a company: personal invitation, follow-up, introduction, request, link to book a meeting slot, startup founder pitching their fundraising, question asked in a LinkedIn or platform conversation the recipient already takes part in), or an automated message requires action (failed payment, invoice or request to validate, document to sign, deadline, account action required). A verification or login code is not an action. Unsolicited sales or paid-study outreach is Marketing, even when written by a named person.",
    "Meeting update": "Calendar invitation or change for a meeting the recipient takes part in: invite, acceptance, decline, reschedule, cancellation, or video-call details. Not webinars or events promoted to a mailing list.",
    "Comment": "Mention or comment notification from a collaborative tool (Jira, Confluence, Notion, Google Docs, Figma, GitHub...).",
    "Notification": "Automated message from a system or service that requires no action: verification or login code, security alert, receipt, paid invoice, shipping, event registration confirmation, account, service or status update, emoji reaction to an email ('X reacted via Gmail'), or social network alert (connection request, new follower, job alert, 'you have a new message'), even when it quotes a personal note from someone, unless that note asks the recipient something in a conversation they already take part in.",
    "FYI": "Message written by a person to share information; no reply or action expected.",
    "Actioned": "Confirms that a request, booking or conversation is done, settled or acknowledged ('noted', 'done', 'see you later'); nothing left to do.",
    "Newsletter": "Editorial or recurring content from a publication or creator the recipient subscribed to (articles, news, curated tips), even when it features products, shops or deals.",
    "Marketing": "Mass email from a brand or service promoting its own products, offers, programmes or content (including welcome and onboarding emails after a sign-up), cold sales outreach or paid survey or research solicitation (even written and signed by a named person), or invitation to a webinar, live session or event sent to a mailing list (even signed by the organiser).",
}
JEV_INSTRUCTIONS = (
    "Pick the triage label for this email, from the point of view of its recipient. "
    "When several labels fit, pick the one that comes first in this priority order: "
    + ", ".join(CATEGORIES) + "."
)
MISTRAL_SYSTEM = (
    "You are an email triage engine. Assign exactly ONE category to the email.\n\n"
    "Categories are mutually exclusive. When several fit, choose the highest priority one.\n\n"
    "Category priority (highest to lowest):\n" + "\n".join(CATEGORIES) + "\n\n"
    "Category definitions:\n" + "\n".join(f"{k}: {v}" for k, v in CATEGORIES.items())
)


_GENERATED = set()


def nid():
    """Placeholder id for a node or a condition; Graph.workflow turns it into a stable one."""
    new = str(uuid.uuid4())
    _GENERATED.add(new)
    return new


def stable_ids(wf):
    """Replace the placeholder ids by ids derived from the workflow name and their order of appearance,
    so that rebuilding an unchanged workflow gives the same file."""
    text, seen = json.dumps(wf, ensure_ascii=False), {}
    for found in re.findall(r'"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"', text):
        if found in _GENERATED and found not in seen:
            seen[found] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{wf['name']}#{len(seen)}"))
    for old, new in seen.items():
        text = text.replace(old, new)
    return json.loads(text)


def js(value):
    """JSON literal safe inside an n8n {{ }} expression (no '}}' that would close it)."""
    s = json.dumps(value, ensure_ascii=False)
    while "}}" in s:
        s = s.replace("}}", "} }")
    return s


def node(name, type_, version, params, pos, **extra):
    creds = {k: v for k, v in (extra.pop("credentials", None) or {}).items() if v}
    if creds:
        extra["credentials"] = creds
    return {"id": nid(), "name": name, "type": type_, "typeVersion": version,
            "position": pos, "parameters": params, **extra}


def assignments(fields, include_other=False):
    """Edit Fields (Set) parameters from [(name, type, value)]."""
    p = {"assignments": {"assignments": [
        {"id": nid(), "name": n, "type": t, "value": v} for n, t, v in fields]},
        "options": {"ignoreConversionErrors": True}}
    if include_other:
        p["includeOtherFields"] = True
    return p


def table_ref(name):
    return {"__rl": True, "value": name, "mode": "name"}


LOG_TYPES = {"confidence": "number", "doubt": "boolean"}   # Classifier Log columns that are not strings


def table_columns(fields):
    """Data Table insert/update mapping for {column: value} (string columns unless listed in LOG_TYPES)."""
    return {"mappingMode": "defineBelow", "value": fields, "matchingColumns": [],
            "schema": [{"id": k, "displayName": k, "required": False, "defaultMatch": False, "display": True,
                        "type": LOG_TYPES.get(k, "string"), "readOnly": False, "removed": False} for k in fields],
            "attemptToConvertTypes": False, "convertFieldsToString": False}


def row_check(name, op, table, column, value, pos):
    """Data Table rowExists / rowNotExists: passes the input item through when (no) row has column = value."""
    return node(name, "n8n-nodes-base.dataTable", 1, {
        "operation": op, "dataTableId": table_ref(table), "matchType": "allConditions",
        "filters": {"conditions": [{"keyName": column, "keyValue": value}]}}, pos)


def string_condition(left, right, operation):
    return {"id": nid(), "leftValue": left, "rightValue": right,
            "operator": {"type": "string", "operation": operation}}


def conditions(conds, combinator="and"):
    return {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
            "conditions": conds, "combinator": combinator}


def gmail(name, op, message_id, pos, cred, **params):
    return node(name, "n8n-nodes-base.gmail", 2.1, {"operation": op, "messageId": message_id, **params},
                pos, credentials={"gmailOAuth2": cred})


def gmail_search(name, query, pos, cred):
    return node(name, "n8n-nodes-base.gmail", 2.1, {"operation": "getAll", "returnAll": True,
                                                     "filters": {"q": query}, "options": {}}, pos,
                credentials={"gmailOAuth2": cred})


def prepare(pos):
    """Normalise a full Gmail message into the fields everything downstream uses."""
    # The text part is skipped when it holds nothing once tags are removed: some senders ship blank lines
    # (Meetup) or an empty HTML skeleton (Envoy), with the real content only in the HTML part.
    # Tags are only stripped from HTML: in a text part, <...> is an address or a link worth keeping.
    strip = ("s => (s || '').replace(/<style[\\s\\S]*?<\\/style>|<script[\\s\\S]*?<\\/script>/gi, ' ')"
             ".replace(/<[^>]+>/g, ' ')")
    body = (f"((strip) => [strip($json.text).trim() && $json.text, strip($json.html), $json.snippet].find(s => s && s.trim()) || '')"
            f"({strip})"
            ".replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '\"')"
            ".replace(/&(#39|apos);/g, \"'\").replace(/&amp;/g, '&').replace(/\\s+/g, ' ').trim()"
            f".slice(0, {BODY_MAX_CHARS})")
    addr = "($json.from?.value?.[0]?.address || '').toLowerCase()"
    return node("Prepare", "n8n-nodes-base.set", 3.4, assignments([
        ("messageId", "string", "={{ $json.id }}"),
        ("from", "string", "={{ " + addr + " }}"),
        ("fromDomain", "string", "={{ '@' + (" + addr + ".split('@')[1] || '') }}"),
        ("fromName", "string", "={{ $json.from?.value?.[0]?.name || '' }}"),
        ("to", "string", "={{ $json.to?.text || '' }}"),
        ("subject", "string", "={{ $json.subject || '' }}"),
        ("date", "string", "={{ $json.date || '' }}"),
        ("hasUnsubscribeLink", "boolean", "={{ !!$json.headers?.['list-unsubscribe'] }}"),
        ("body", "string", "={{ " + body + " }}"),
    ]), pos)


def jev(cred, pos):
    question = {"category": {"type": "choice", "instructions": JEV_INSTRUCTIONS, "criteria": CATEGORIES}}
    body = ("={{ JSON.stringify({ model: " + js(JEV_MODEL) + ", state: { "
            "from: $json.fromName ? $json.fromName + ' <' + $json.from + '>' : $json.from, "
            "to: $json.to, subject: $json.subject, date: $json.date, "
            "has_unsubscribe_link: $json.hasUnsubscribeLink, body: $json.body, "
            "...($json.senderHistory ? { sender_history: $json.senderHistory } : {}) }, "
            "questions: " + js(question) + " }) }}")
    return node("Jev", "n8n-nodes-base.httpRequest", 4.2, {
        "method": "POST", "url": "https://api.typesafe.ai/v1/systemone",
        "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
        "sendBody": True, "specifyBody": "json", "jsonBody": body,
        "options": {"timeout": 20000}}, pos,
        credentials={"httpHeaderAuth": cred},
        retryOnFail=True, maxTries=3, waitBetweenTries=2000, onError="continueErrorOutput")


def mistral_nodes(cred, x, y, history=None):
    """AI Agent + Mistral model + output parser restricted to the category list. Reads email fields from Prepare,
    and the sender's past corrections from the `history` expression when given."""
    p = "$('Prepare').item.json"
    after = f"{{{{ {history} ? '\\n\\n' + {history} : '' }}}}" if history else ""
    agent = node("Mistral fallback", "@n8n/n8n-nodes-langchain.agent", 3, {
        "promptType": "define",
        "text": (f"=Classify the following email.\n\nFrom: {{{{ {p}.fromName }}}} <{{{{ {p}.from }}}}>\n"
                 f"Subject: {{{{ {p}.subject }}}}\n\n{{{{ {p}.body }}}}{after}"),
        "hasOutputParser": True, "options": {"systemMessage": MISTRAL_SYSTEM}}, [x, y])
    model = node("Mistral Cloud Chat Model", "@n8n/n8n-nodes-langchain.lmChatMistralCloud", 1,
                 {"model": MISTRAL_MODEL, "options": {"temperature": 0}}, [x - 60, y + 200],
                 credentials={"mistralCloudApi": cred})
    schema = {"type": "object", "required": ["category"],
              "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}}}
    parser = node("Category parser", "@n8n/n8n-nodes-langchain.outputParserStructured", 1.3,
                  {"schemaType": "manual", "inputSchema": json.dumps(schema, indent=2)}, [x + 120, y + 200])
    conns = {model["name"]: {"ai_languageModel": [[{"node": agent["name"], "type": "ai_languageModel", "index": 0}]]},
             parser["name"]: {"ai_outputParser": [[{"node": agent["name"], "type": "ai_outputParser", "index": 0}]]}}
    return [agent, model, parser], conns


def confident(pos):
    return node("Confident?", "n8n-nodes-base.if", 2.2, {"conditions": conditions([{
        "id": nid(), "leftValue": "={{ $json.answers?.category?.confidence ?? 0 }}",
        "rightValue": CONFIDENCE_THRESHOLD, "operator": {"type": "number", "operation": "gte"}}]),
        "options": {}}, pos)


JEV_ANSWER = "$('Jev').item.json.answers?.category"

# Runs once for all items; input = Prepare items, rules read from "Load sender rules", past corrections
# from "Load corrections". History = the recipient's corrections for the exact address, latest first: context
# for Jev and Mistral, not a rule (a mixed sender is still judged email by email).
MATCH_RULE_JS = """const rules = $('Load sender rules').all().map(i => i.json).filter(r => r.pattern);
const corrections = $('Load corrections').all().map(i => i.json).filter(r => r.from && r.finalCategory)
  .sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));
const history = (from) => {
  const past = corrections.filter(r => r.from === from).slice(0, __HISTORY_MAX__);
  return past.length ? 'The recipient corrected the label of earlier emails from this sender:\\n'
    + past.map(r => `- "${r.subject}": ${r.category} → ${r.finalCategory}`).join('\\n') : '';
};
const best = (key) => rules
  .filter(r => String(r.pattern).trim().toLowerCase() === key)
  .sort((a, b) => (a.origin === 'manual' ? 0 : 1) - (b.origin === 'manual' ? 0 : 1))[0];
return $input.all().map((item, i) => {
  const rule = best(item.json.from) || best(item.json.fromDomain);
  return {
    json: { ...item.json, ruleMode: rule?.mode || 'none', ruleCategory: rule?.category || '', rulePattern: rule?.pattern || '',
            senderHistory: history(item.json.from) },
    pairedItem: { item: i },
  };
});"""


# Gmail label name → id, resolved at run time so the workflow holds no account-specific id.
LABEL_IDS_JS = """const WANT = __WANT__;   // {triage: {role: label name}, doubt: name, forceAi: name}
const byName = new Map($input.all().map(i => [String(i.json.name).toLowerCase(), i.json.id]));
const id = (name) => byName.get(name.toLowerCase());
const missing = [...Object.values(WANT.triage), WANT.doubt, WANT.forceAi].filter(n => !id(n));
if (missing.length) throw new Error('Missing Gmail labels: ' + missing.join(', '));
return [{ json: {
  triage: Object.fromEntries(Object.entries(WANT.triage).map(([role, name]) => [role, id(name)])),
  doubt: id(WANT.doubt), forceAi: id(WANT.forceAi) } }];"""
IDS = "$('Label ids').first().json"


def label_nodes(g, cfg, x, y):
    """Gmail labels (once per execution) → Label ids. Returns (first, last)."""
    want = {"triage": cfg["labels"], "doubt": cfg["doubtLabel"], "forceAi": cfg["forceAiLabel"]}
    fetch = g.add(node("Gmail labels", "n8n-nodes-base.gmail", 2.1, {
        "resource": "label", "operation": "getAll", "returnAll": True}, [x, y],
        credentials={"gmailOAuth2": cfg.get("credentials", {}).get("gmail")}, executeOnce=True))
    ids = g.add(node("Label ids", "n8n-nodes-base.code", 2, {"jsCode": LABEL_IDS_JS.replace("__WANT__", js(want))},
                     [x + 220, y]))
    g.link(fetch, ids)
    return fetch, ids


class Graph:
    def __init__(self):
        self.nodes, self.connections = [], {}

    def add(self, *nodes):
        self.nodes.extend(nodes)
        return nodes[0] if len(nodes) == 1 else nodes

    def link(self, src, dst, output=0):
        outs = self.connections.setdefault(src["name"], {}).setdefault("main", [])
        while len(outs) <= output:
            outs.append([])
        outs[output].append({"node": dst["name"], "type": "main", "index": 0})

    def merge(self, conns):
        self.connections.update(conns)

    def workflow(self, wf_id, name):
        wf = {"name": name, "active": False, "nodes": self.nodes,
              "connections": self.connections, "settings": {"executionOrder": "v1"}, "pinData": {}}
        return stable_ids({"id": wf_id, **wf} if wf_id else wf)


def build_production(cfg):
    c, t = cfg.get("credentials", {}), cfg["dataTables"]
    p = "$('Prepare').item.json"
    g = Graph()

    trigger = g.add(node("Gmail Trigger", "n8n-nodes-base.gmailTrigger", 1.3,
                         {"pollTimes": {"item": [{"mode": "everyMinute"}]}, "filters": {}}, [0, 300],
                         credentials={"gmailOAuth2": c.get("gmail")}))
    # Catch-up: the Gmail Trigger only returns emails dated after its previous poll, so an email Gmail
    # indexes a few seconds late is skipped for good. Every few minutes, re-feed recent emails that still
    # have no triage label (older than a few minutes, to leave fresh ones to the trigger).
    sweep = g.add(node("Every 15 minutes", "n8n-nodes-base.scheduleTrigger", 1.2, {"rule": {"interval": [
        {"field": "minutes", "minutesInterval": SWEEP_EVERY_MINUTES}]}}, [0, 600]))
    no_label = " ".join("-label:" + name.lower().replace(" ", "-") for name in cfg["labels"].values())
    unlabelled = g.add(gmail_search("Unlabelled emails", f"{SWEEP_WINDOW} -in:sent -in:chats -in:drafts {no_label}",
                                    [220, 600], c.get("gmail")))
    settled = g.add(node("Settled", "n8n-nodes-base.filter", 2.2, {"conditions": conditions([{
        "id": nid(), "leftValue": "={{ Number($json.internalDate) }}", "rightValue": f"={{{{ Date.now() - {SWEEP_SETTLE_MS} }}}}",
        "operator": {"type": "number", "operation": "lt"}}]), "options": {}}, [440, 600]))
    subj = "={{ $json.Subject }}"
    # Digest emails (recaps sent by other workflows, matched by exact subject) only get the Digest label
    digests = cfg.get("digestSubjects", [])
    is_digest = g.add(node("Digest?", "n8n-nodes-base.filter", 2.2, {"conditions": conditions(
        [string_condition(subj, s, "equals") for s in digests], "or"), "options": {}}, [220, 100]))
    not_digest = g.add(node("Not a digest", "n8n-nodes-base.filter", 2.2, {"conditions": conditions(
        [string_condition(subj, s, "notEquals") for s in digests]), "options": {}}, [220, 400]))
    label_digest = g.add(gmail("Label Digest", "addLabels", "={{ $json.id }}", [440, 100], c.get("gmail"),
                               labelIds=f"={{{{ {IDS}.triage.Digest }}}}"))
    get = g.add(gmail("Get a message", "get", "={{ $json.id }}", [440, 400], c.get("gmail"), simple=False, options={}))
    # Idempotence: an email that already has a triage label is never classified again (trigger state reset on
    # republish, trigger and sweep picking the same email, manual re-runs).
    unlabelled_only = g.add(node("Not labelled yet", "n8n-nodes-base.filter", 2.2, {"conditions": conditions([{
        "id": nid(), "leftValue": f"={{{{ !($json.labelIds || []).some(id => Object.values({IDS}.triage).includes(id)) }}}}",
        "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}]),
        "options": {}}, [550, 400]))
    prep = g.add(prepare([660, 400]))

    # Sender Rules: loaded once per execution (above "Get a message" → runs first in v1 execution order),
    # then matched in code: exact address beats domain, a manual rule beats the others.
    rules = g.add(node("Load sender rules", "n8n-nodes-base.dataTable", 1, {
        "operation": "get", "dataTableId": table_ref(t["senderRules"]), "returnAll": True}, [440, 220],
        executeOnce=True, alwaysOutputData=True))
    corrections = g.add(node("Load corrections", "n8n-nodes-base.dataTable", 1, {
        "operation": "get", "dataTableId": table_ref(t["classifierLog"]), "matchType": "allConditions",
        "filters": {"conditions": [{"keyName": "status", "keyValue": "corrected"}]}, "returnAll": True}, [660, 220],
        executeOnce=True, alwaysOutputData=True))
    match = g.add(node("Match sender rule", "n8n-nodes-base.code", 2, {
        "jsCode": MATCH_RULE_JS.replace("__HISTORY_MAX__", str(HISTORY_MAX))}, [880, 400]))
    has_rule = g.add(node("Fixed rule?", "n8n-nodes-base.if", 2.2, {"conditions": conditions([
        string_condition("={{ $json.ruleMode }}", "fixed", "equals"),
        {"id": nid(), "leftValue": "={{ " + js(list(CATEGORIES)) + ".includes($json.ruleCategory) }}",
         "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}]),
        "options": {}}, [1100, 400]))
    rule_result = g.add(node("Rule result", "n8n-nodes-base.set", 3.4, assignments([
        ("category", "string", "={{ $json.ruleCategory }}"), ("source", "string", "rule"),
        ("model", "string", "={{ 'rule:' + $json.rulePattern }}")]), [1540, 550]))

    jev_node = g.add(jev(c.get("typesafe"), [1320, 300]))
    conf = g.add(confident([1540, 250]))
    jev_result = g.add(node("Jev result", "n8n-nodes-base.set", 3.4, assignments([
        ("category", "string", "={{ $json.answers.category.choice }}"),
        ("confidence", "number", "={{ $json.answers.category.confidence }}"),
        ("jevChoice", "string", "={{ $json.answers.category.choice }}"),
        ("source", "string", "jev"), ("model", "string", "={{ $json.model }}")]), [1760, 150]))
    ai_nodes, ai_conns = mistral_nodes(c.get("mistral"), 1760, 400, history="$('Match sender rule').item.json.senderHistory")
    g.add(*ai_nodes)
    g.merge(ai_conns)
    agent = ai_nodes[0]
    mistral_result = g.add(node("Mistral result", "n8n-nodes-base.set", 3.4, assignments([
        ("category", "string", "={{ $json.output.category }}"),
        ("confidence", "number", f"={{{{ {JEV_ANSWER}?.confidence ?? null }}}}"),
        ("jevChoice", "string", f"={{{{ {JEV_ANSWER}?.choice ?? 'error' }}}}"),
        ("source", "string", "mistral"), ("model", "string", MISTRAL_MODEL)]), [2080, 400]))

    # Doubt: Jev under the threshold and Mistral picked another label → flagged for review in Gmail
    # (keep or fix the label, then remove the flag), and left unread so it stays visible.
    label = g.add(node("Label", "n8n-nodes-base.set", 3.4, assignments([
        ("labelId", "string", f"={{{{ {IDS}.triage[$json.category] ?? {IDS}.triage.FYI }}}}"),
        ("doubt", "boolean", "={{ $json.source === 'mistral' && !!$json.jevChoice && $json.jevChoice !== 'error' "
                             "&& $json.jevChoice !== $json.category }}")],
        include_other=True), [2300, 300]))
    add_label = g.add(gmail("Add label", "addLabels", f"={{{{ {p}.messageId }}}}", [2520, 300], c.get("gmail"),
                            labelIds="={{ $json.labelId }}"))

    L = "$('Label').item.json"
    log_fields = {
        "messageId": f"={{{{ {p}.messageId }}}}", "from": f"={{{{ {p}.from }}}}",
        "subject": f"={{{{ {p}.subject.slice(0, 200) }}}}", "category": f"={{{{ {L}.category }}}}",
        "confidence": f"={{{{ {L}.confidence ?? null }}}}", "jevChoice": f"={{{{ {L}.jevChoice ?? '' }}}}",
        "source": f"={{{{ {L}.source }}}}", "model": f"={{{{ {L}.model ?? '' }}}}",
        "status": "open", "doubt": f"={{{{ !!{L}.doubt }}}}"}
    log = g.add(node("Log", "n8n-nodes-base.dataTable", 1, {
        "dataTableId": table_ref(t["classifierLog"]), "columns": table_columns(log_fields),
        "options": {}}, [2740, 150]))

    not_doubt = {"id": nid(), "leftValue": f"={{{{ !{L}.doubt }}}}", "rightValue": "",
                 "operator": {"type": "boolean", "operation": "true", "singleValue": True}}
    read_cats = ["Marketing", "Meeting update", "Newsletter"]
    switch = g.add(node("Mark as read?", "n8n-nodes-base.switch", 3.3, {"rules": {"values": [
        {"conditions": conditions([string_condition(f"={{{{ {L}.category }}}}", cat, "equals"), not_doubt]),
         "renameOutput": True, "outputKey": cat} for cat in read_cats]}, "options": {}}, [2740, 400]))
    mark_read = g.add(gmail("Mark as read", "markAsRead", f"={{{{ {p}.messageId }}}}", [2960, 400], c.get("gmail")))
    is_doubt = g.add(node("Doubt?", "n8n-nodes-base.filter", 2.2, {"conditions": conditions([{
        **not_doubt, "id": nid(), "leftValue": f"={{{{ {L}.doubt }}}}"}]), "options": {}}, [2740, 650]))
    flag_doubt = g.add(gmail("Flag for review", "addLabels", f"={{{{ {p}.messageId }}}}", [2960, 650], c.get("gmail"),
                             labelIds=f"={{{{ {IDS}.doubt }}}}"))

    # Label ids: topmost child of both entry points, so v1 execution order resolves labels before anything else
    fetch_labels, _ = label_nodes(g, cfg, 660, -100)
    g.link(trigger, fetch_labels); g.link(trigger, is_digest); g.link(trigger, not_digest)
    g.link(sweep, unlabelled); g.link(unlabelled, settled)
    g.link(settled, fetch_labels); g.link(settled, is_digest); g.link(settled, not_digest)
    g.link(is_digest, label_digest)
    g.link(not_digest, rules); g.link(rules, corrections); g.link(not_digest, get); g.link(get, unlabelled_only); g.link(unlabelled_only, prep)
    g.link(prep, match); g.link(match, has_rule)
    g.link(has_rule, rule_result, 0); g.link(has_rule, jev_node, 1)
    g.link(jev_node, conf, 0); g.link(jev_node, agent, 1)      # Jev error output → Mistral
    g.link(conf, jev_result, 0); g.link(conf, agent, 1)        # low confidence → Mistral
    g.link(agent, mistral_result)
    for src in (jev_result, mistral_result, rule_result):
        g.link(src, label)
    g.link(label, add_label)
    g.link(add_label, log); g.link(add_label, switch); g.link(add_label, is_doubt); g.link(is_doubt, flag_doubt)
    for i, cat in enumerate(read_cats):
        g.link(switch, mark_read, i)
    return g.workflow(cfg.get("workflowIds", {}).get("production"), "✉️ Gmail Triage — classifier")


# Runs once for all items. Compares each open Classifier Log row with the email's current Gmail labels.
CORRECTIONS_JS = """const L = $('Label ids').first().json;
const TRIAGE = Object.fromEntries(Object.entries(L.triage).map(([role, id]) => [id, role]));   // label id → role
const DOUBT = L.doubt;
const DAY = 24 * 3600 * 1000, now = Date.now();
// The simplified Gmail output may carry labels as [{id, name}] instead of labelIds
const emails = $('Recent emails').all().map(i => i.json).filter(m => m.id)
  .map(m => ({ ...m, labelIds: m.labelIds ?? m.labels?.map(l => l.id) }));
// Never guess: without labels, every row would look 'removed'
if (emails.some(m => !Array.isArray(m.labelIds))) throw new Error('Gmail search returned emails without labels');
const byId = new Map(emails.map(m => [m.id, m]));
const out = [];
for (const row of $('Open log rows').all().map(i => i.json).filter(r => r.id && r.messageId)) {
  const age = (now - new Date(row.createdAt).getTime()) / DAY;
  const email = byId.get(row.messageId);
  let status = null, finalCategory = row.category;
  if (!email) {
    // Out of the search window, trashed or spam: give up once it can no longer show up
    if (age > __EXPIRE_DAYS__) { status = 'expired'; finalCategory = ''; }
  } else {
    const labels = email.labelIds.filter(id => id in TRIAGE).map(id => TRIAGE[id]);
    const others = labels.filter(l => l !== row.category);
    if (!labels.length) { status = 'removed'; finalCategory = ''; }
    else if (others.length) { status = 'corrected'; finalCategory = others.join(', '); }
    else if (row.doubt && !email.labelIds.includes(DOUBT)) status = 'confirmed';   // flag removed, label kept
    else if (age >= __CONFIRM_DAYS__) status = 'confirmed';                          // untouched for a week
  }
  if (status) out.push({ json: { id: row.id, status, finalCategory } });
}
return out;"""
# Runs once for all items: one item per distinct sender among the emails labelled Force AI.
FORCE_AI_SENDERS_JS = """const seen = new Set();
return $input.all().map(i => (i.json.from?.value?.[0]?.address || '').trim().toLowerCase())
  .filter(a => a && !seen.has(a) && seen.add(a))
  .map(pattern => ({ json: { pattern } }));"""
CONFIRM_DAYS = 7          # an unchanged label counts as confirmed after this many days
CORRECTIONS_WINDOW_DAYS = CONFIRM_DAYS + 1


def build_corrections(cfg):
    """Every hour: open Classifier Log rows vs the labels now on the emails in Gmail → corrected, removed,
    confirmed or expired. One Gmail search for the whole window instead of one call per email.
    Same run: senders of emails labelled Force AI become `llm` Sender Rules."""
    c, t = cfg.get("credentials", {}), cfg["dataTables"]
    g = Graph()
    trigger = g.add(node("Every hour", "n8n-nodes-base.scheduleTrigger", 1.2,
                         {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}}, [0, 300]))
    emails = g.add(node("Recent emails", "n8n-nodes-base.gmail", 2.1, {
        "operation": "getAll", "returnAll": True,
        "filters": {"q": f"newer_than:{CORRECTIONS_WINDOW_DAYS}d -in:sent -in:chats -in:drafts"}, "options": {}},
        [220, 300], credentials={"gmailOAuth2": c.get("gmail")}, executeOnce=True, alwaysOutputData=True))
    rows = g.add(node("Open log rows", "n8n-nodes-base.dataTable", 1, {
        "operation": "get", "dataTableId": table_ref(t["classifierLog"]), "matchType": "allConditions",
        "filters": {"conditions": [{"keyName": "status", "keyValue": "open"}]}, "returnAll": True}, [440, 300],
        executeOnce=True, alwaysOutputData=True))
    code = (CORRECTIONS_JS.replace("__CONFIRM_DAYS__", str(CONFIRM_DAYS))
            .replace("__EXPIRE_DAYS__", str(CORRECTIONS_WINDOW_DAYS + 1)))
    compare = g.add(node("Compare labels", "n8n-nodes-base.code", 2, {"jsCode": code}, [660, 300]))
    update = g.add(node("Update log", "n8n-nodes-base.dataTable", 1, {
        "operation": "update", "dataTableId": table_ref(t["classifierLog"]), "matchType": "allConditions",
        "filters": {"conditions": [{"keyName": "id", "keyValue": "={{ $json.id }}"}]},
        "columns": table_columns({"status": "={{ $json.status }}", "finalCategory": "={{ $json.finalCategory }}"}),
        "options": {}}, [880, 300]))
    fetch_labels, _ = label_nodes(g, cfg, 220, 100)     # topmost: runs first (v1 execution order)
    g.link(trigger, fetch_labels)
    g.link(trigger, emails); g.link(emails, rows); g.link(rows, compare); g.link(compare, update)

    # Force AI: the user puts the Force AI label on any email of a mixed sender → that sender becomes an `llm`
    # rule (always judged email by email, never a fixed label) → the label is removed once the rule is written.
    # Table writes sit above the unflag node, so an error stops the run before the flag is removed.
    flagged = g.add(node("Flagged Force AI", "n8n-nodes-base.gmail", 2.1, {
        "operation": "getAll", "returnAll": True, "simple": False,
        "filters": {"labelIds": f"={{{{ [{IDS}.forceAi] }}}}"}, "options": {}}, [220, 600],
        credentials={"gmailOAuth2": c.get("gmail")}))
    senders = g.add(node("One per sender", "n8n-nodes-base.code", 2, {"jsCode": FORCE_AI_SENDERS_JS}, [440, 600]))
    rule = {"mode": "llm", "category": "", "origin": "manual", "note": "Force AI label in Gmail"}
    known = g.add(row_check("Has a rule", "rowExists", t["senderRules"], "pattern", "={{ $json.pattern }}", [660, 520]))
    to_llm = g.add(node("Switch rule to llm", "n8n-nodes-base.dataTable", 1, {
        "operation": "update", "dataTableId": table_ref(t["senderRules"]), "matchType": "allConditions",
        "filters": {"conditions": [{"keyName": "pattern", "keyValue": "={{ $json.pattern }}"}]},
        "columns": table_columns(rule), "options": {}}, [880, 520]))
    unknown = g.add(row_check("No rule yet", "rowNotExists", t["senderRules"], "pattern", "={{ $json.pattern }}", [660, 680]))
    add_rule = g.add(node("Add llm rule", "n8n-nodes-base.dataTable", 1, {
        "dataTableId": table_ref(t["senderRules"]),
        "columns": table_columns({"pattern": "={{ $json.pattern }}", **rule}), "options": {}}, [880, 680]))
    unflag = g.add(gmail("Remove flag", "removeLabels", "={{ $json.id }}", [440, 860], c.get("gmail"),
                         labelIds=f"={{{{ [{IDS}.forceAi] }}}}"))
    g.link(trigger, flagged); g.link(flagged, senders); g.link(flagged, unflag)
    g.link(senders, known); g.link(known, to_llm); g.link(senders, unknown); g.link(unknown, add_rule)
    return g.workflow(cfg.get("workflowIds", {}).get("corrections"),
                      "✏️ Gmail Triage — corrections")


def main():
    here = Path(__file__).parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=here / "config.local.json")
    ap.add_argument("--out", type=Path, default=here / "dist")
    ap.add_argument("--check", action="store_true", help="fail if the files in --out are not up to date")
    args = ap.parse_args()
    cfg = json.loads(args.config.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    stale = []
    for fname, wf in (("gmail-classifier.json", build_production(cfg)),
                      ("gmail-classifier-corrections.json", build_corrections(cfg))):
        path, text = args.out / fname, json.dumps(wf, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(str(path))
            continue
        path.write_text(text)
        print(f"{path}: {len(wf['nodes'])} nodes")
    if stale:
        sys.exit("✗ not up to date, rebuild: " + ", ".join(stale))
    if args.check:
        print(f"✓ {args.out} is up to date")


if __name__ == "__main__":
    main()
