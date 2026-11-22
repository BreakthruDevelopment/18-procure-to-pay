"""Three-way match, staged posting and approval (slice 18.5).

The fixed four steps — extract, fetch the purchase order, fetch the goods receipt,
decide — run in code, in that order. The model is called inside a step and never
chooses the next one. A posting is staged through the gateway's permission gate;
applying it is a later command and a different principal.
"""
