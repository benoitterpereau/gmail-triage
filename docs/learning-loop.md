# The learning loop

The classifier learns from what you do in Gmail. There is no separate interface: you fix a label as you normally would, and the next emails from that sender are judged with your fix in mind.

## 1. You fix a label

Swap a wrong triage label for the right one in Gmail. Within the hour, the corrections workflow compares each `open` row of `Classifier Log` with the labels now on the email:

| What it sees | Status | `finalCategory` |
| --- | --- | --- |
| Another triage label than the logged one | `corrected` | the label you chose |
| No triage label left | `removed` | empty |
| 🤖 To Check removed, label kept | `confirmed` | the logged label |
| Label untouched for 7 days | `confirmed` | the logged label |
| Email gone (trashed, spam) after 9 days | `expired` | empty |

## 2. Your corrections come back as context

On the next email from the same address, Jev and Mistral get your last corrections for that sender (up to 5, latest first):

```
The recipient corrected the label of earlier emails from this sender:
- "Product director jobs this week": Newsletter → Notification
```

This is context, not a rule. Each email is still judged on its own content, which matters for senders who send several kinds of email.

## 3. Doubts ask for your eye

When Jev is unsure (below 0.6) and Mistral picks another label, the email gets **🤖 To Check** and stays unread:

- the label is right: remove 🤖 To Check, and the row becomes `confirmed`;
- it is wrong: change the label and remove 🤖 To Check, and the row becomes `corrected`.

## 4. Sender rules

`Sender Rules` holds one row per address or `@domain`:

- `fixed` + a category labels every email from that sender directly, with no model call. It suits senders that only ever send one kind of email.
- `llm` always lets the models judge, email by email, even if a broader rule (the domain) is `fixed`.

An exact address beats its domain, and a `manual` rule beats the others.

## 5. 🤖 Force AI

Some senders mix kinds of email. The same LinkedIn address sends messages, notifications and job digests. Put **🤖 Force AI** on any email from such a sender:

1. Within the hour, the sender gets an `llm` rule (`origin: manual`). An existing rule for that address is switched to `llm`.
2. The 🤖 Force AI label is removed once the rule is written.

## Measuring it

`Classifier Log` holds everything needed to measure the system:

- the error rate, from `corrected` and `removed` over all settled rows;
- which source errs (`rule`, `jev` or `mistral`);
- whether doubts catch the errors;
- which senders keep getting corrected, which are candidates for a rule.

```
python3 n8n/n8n.py rows "Classifier Log"
```
