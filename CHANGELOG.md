# Changelog

## 2.0.0

The triage now runs on n8n and learns from your corrections. It replaces the skill of version 1.

- Classifier workflow: Gmail Trigger plus a 15-minute sweep. Jev (TypeSafe) decides when its confidence is 0.6 or more, Mistral otherwise.
- Doubts: when Jev was unsure and Mistral disagreed, the email gets **🤖 To Check** and stays unread.
- Hourly corrections workflow: each logged decision becomes `corrected`, `removed`, `confirmed` or `expired`, from the labels now on the email.
- A sender's past corrections are given to both models as context for that sender's next emails.
- Sender rules: a fixed label per address or `@domain`, or `llm` to always judge with the models.
- **🤖 Force AI** label: its sender gets an `llm` rule, then the label is removed.
- Portable: Gmail labels, Data Tables and credentials are found by name. Ready-to-import workflows are in `n8n/workflows/`.
- `n8n/n8n.py`: deploy and inspect through the n8n public API.
- `tools/guard.py`: pre-commit check against secrets and account identifiers.
