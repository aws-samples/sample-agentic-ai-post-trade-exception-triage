# Contributing

Thank you for your interest in improving this sample. This repository is intended
to demonstrate a governed, synthetic pattern for post-trade exception triage on
AWS. Contributions should keep the sample reproducible, synthetic, and safe to
deploy in a demo account.

## Ground Rules

- Do not add real customer, counterparty, account, trade, settlement, or
  operational data.
- Do not add live AWS account IDs, ARNs, public endpoints, secrets, access keys,
  or organization-internal hostnames.
- Keep Gateway tools read-only unless the sample is explicitly redesigned and
  reviewed as a write-capable workflow.
- Keep agent recommendations advisory. The sample must not auto-resolve
  exceptions or modify systems of record.
- Include tests for behavioral changes, especially policy, routing, evaluation,
  and schema-validation behavior.

## Local Validation

Run these checks before opening a pull request:

```bash
python3 -m pip install -r requirements.txt
DISABLE_STRANDS_MODEL_CALL=1 python3 -m pytest -q
npm --prefix frontend install
npm --prefix frontend run build
npx --yes aws-cdk@2.1123.0 synth -c account=111122223333 -c region=us-east-1
```

For dependency checks, run:

```bash
pip-audit -r requirements.txt
npm --prefix frontend audit --audit-level=moderate
```

## Pull Requests

Please describe:

- What changed and why.
- Which AWS resources or permissions are affected.
- Which tests and local checks were run.
- Any security, cost, latency, or operational trade-offs.

By submitting a pull request, you represent that you have the right to license
your contribution under this repository's license.
