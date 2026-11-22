"""Supplier disputes desk (slice 18.6, exam scenario 1).

The `dispute_case` state machine, the five-criterion escalation chain, the
`dispute-triage` task, and the `p2p dispute open/step/show` commands. Writes go
through the gateway's staged-change path; this package never applies a credit or a
hold. A case reaching `resolved` does not release a payment hold: `held` is terminal.
"""
