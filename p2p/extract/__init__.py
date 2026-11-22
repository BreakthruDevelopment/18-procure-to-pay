"""Invoice extraction: few-shot, forced schema, validate and repair (slice 18.4).

The `invoice-extract` task, the `InvoiceDraft` schema sent as a forced tool, three
committed few-shot examples as files, a one-retry repair loop, per-field confidence,
the `extraction` audit row, and `p2p ingest`. This package reads nothing it does not
own and stages nothing.
"""
