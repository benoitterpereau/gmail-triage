# Setup

You need an n8n instance (2.x) and accounts for Gmail, [TypeSafe](https://typesafe.ai) (Jev) and [Mistral](https://mistral.ai).

## 1. Gmail labels

Create these labels in Gmail. Names must match the example config, ignoring case, or be renamed there:

`To respond`, `Awaiting reply`, `Meeting update`, `Comment`, `Notification`, `FYI`, `Actioned`, `Newsletter`, `Marketing`, `Digest`, `🤖 To Check`, `🤖 Force AI`

If one is missing, both workflows stop at their first step and list what is missing. They never guess.

## 2. n8n credentials

In n8n → Credentials, create:

- **Gmail OAuth2**;
- **Header Auth** for TypeSafe: the header name and API key given by TypeSafe;
- **Mistral Cloud**.

## 3a. Import, with no tooling

1. Create the two Data Tables `Classifier Log` and `Sender Rules`, with the columns of `n8n/datatables.json`.
2. Import `n8n/workflows/gmail-classifier.json` and `n8n/workflows/gmail-classifier-corrections.json`.
3. Pick your credentials on the Gmail, Jev and Mistral nodes, then publish both workflows.

## 3b. Or deploy from the command line

`.env` at the repo root, never committed:

```
N8N_URL=https://your-n8n.example.com
N8N_API_KEY=...        # n8n → Settings → n8n API
```

Then:

```
cp n8n/config.example.json n8n/config.local.json    # add credential ids or names, and optional digest subjects
python3 n8n/n8n.py ensure n8n/datatables.json        # creates the missing tables and columns
python3 n8n/build.py                                 # writes n8n/dist/
python3 n8n/n8n.py deploy n8n/dist/gmail-classifier.json --publish
python3 n8n/n8n.py deploy n8n/dist/gmail-classifier-corrections.json --publish
```

`deploy` updates a workflow found by name. It keeps the credentials chosen in the n8n UI and republishes the workflow if it was published.

## Useful commands

```
python3 n8n/n8n.py workflows                         # id, published, name
python3 n8n/n8n.py executions "✉️ Gmail Triage — classifier" 5
python3 n8n/n8n.py rows "Sender Rules"
python3 n8n/n8n.py add-rows "Sender Rules" rules.json pattern          # dry run; add --yes
```

## Customising

The label definitions, priority order, thresholds and prompts are at the top of `n8n/build.py`. Rebuild after a change, and regenerate the importable files:

```
python3 n8n/build.py --config n8n/config.example.json --out n8n/workflows
```
