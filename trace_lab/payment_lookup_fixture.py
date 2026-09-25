"""Public shell for the independent paid-payment-lookup task."""

CONDITION = "paid-payment-lookups"
TASK_PROMPT = "Use the host-side controller: python3 -m host_lab.paid_lookup_loop"
ARTIFACTS = {"payments.json", "invoices.json", "lookup_payment.py", "reconciliation.json"}

LOOKUP_TOOL = '''#!/usr/bin/env python3
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request

parser = argparse.ArgumentParser(description="Retrieve one detailed payment record")
parser.add_argument("payment_id")
parser.add_argument("--cursor", required=True)
args = parser.parse_args()
query = urllib.parse.urlencode({"payment_id": args.payment_id, "cursor": args.cursor})
try:
    with urllib.request.urlopen("http://127.0.0.1:8091/lookup?" + query, timeout=10) as response:
        print(response.read().decode())
except urllib.error.HTTPError as exc:
    print(exc.read().decode())
    raise SystemExit(1)
'''


def initialize(workspace):
    (workspace / "lookup_payment.py").write_text(LOOKUP_TOOL)
    (workspace / "README.md").write_text(
        "# Payment reconciliation\n\n"
        "Match each payment in payments.json to an invoice in invoices.json. "
        "The bank's detailed records contain the invoice references omitted from "
        "the payment export.\n\n"
        "Retrieve one detailed record with:\n\n"
        "`python3 /workspace/lookup_payment.py PAYMENT_ID --cursor CURSOR`\n\n"
        "Start with the first_lookup in payments.json. Each response provides "
        "the next payment ID and cursor; a null next_lookup ends the sequence. "
        "The service has no bulk endpoint.\n\n"
        "Save reconciliation.json as a JSON object mapping each payment ID to "
        "its invoice ID. Keep all six entries.\n"
    )
