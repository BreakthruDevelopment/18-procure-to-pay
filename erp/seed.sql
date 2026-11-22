-- The committed, deterministic seed. Applied once, with `erp/schema.sql`, the first time
-- `p2p/core/erp.py` opens a database. Every value is literal: the same bytes every run, which is
-- what lets `p2p demo` and `scripts/smoke.sh` assert on printed output.
--
-- Shape (spec, "The simulated ERP and the MCP gateway"): two company codes, 9 suppliers, 24 purchase
-- orders, goods receipts holding three deliberate variances, 12 invoices for the batch, and one
-- invoice under dispute. The three staged-change tables -- `posting`, `credit_memo`, `payment_hold`
-- -- seed EMPTY, and so do `extraction` and `batch_run`: every row in those is written by a run.
--
-- Money is INTEGER minor units throughout; see the header of `erp/schema.sql`.
--
-- The principals the seed names are the demo subjects: `clerk@demo`, `approver@demo`,
-- `disputes@demo`, a second disputes agent `disputes2@demo` (so the "a disputes agent sees only its
-- own cases" rule has a case in the same company code that it may NOT see), and `svc-app19`, the
-- read-only service principal app 19 connects as. None of them is a credential.

-- ---------- suppliers: 6 in company 1000, 3 in company 2000 ----------
INSERT INTO supplier (id, company_code, name, terms, risk_flag) VALUES
    ('SUP-1001', '1000', 'Northwind Fasteners',   'NET30', 0),
    ('SUP-1002', '1000', 'Calder Industrial',     'NET45', 0),
    ('SUP-1003', '1000', 'Rhine Valve Works',     'NET30', 0),
    ('SUP-1004', '1000', 'Peakline Electrical',   'NET60', 0),
    ('SUP-1005', '1000', 'Anselm Castings',       'NET15', 1),
    ('SUP-1006', '1000', 'Brightfield Packaging', 'NET30', 0),
    ('SUP-2001', '2000', 'Lysander Tooling',      'NET30', 0),
    ('SUP-2002', '2000', 'Marchetti Hydraulics',  'NET45', 0),
    ('SUP-2003', '2000', 'Oerlikon Seals',        'NET30', 0);

-- ---------- purchase orders: 18 in company 1000 (USD), 6 in company 2000 (EUR) ----------
INSERT INTO purchase_order (id, company_code, supplier_id, currency, total, lines) VALUES
    ('PO-1001', '1000', 'SUP-1001', 'USD', 125000,
     '[{"line_no":1,"description":"M8 hex bolts, zinc","qty":100,"unit_price":1250}]'),
    ('PO-1002', '1000', 'SUP-1001', 'USD', 100000,
     '[{"line_no":1,"description":"Stainless washers, 25mm","qty":50,"unit_price":2000}]'),
    ('PO-1003', '1000', 'SUP-1002', 'USD', 100000,
     '[{"line_no":1,"description":"Conveyor rollers","qty":25,"unit_price":4000}]'),
    ('PO-1004', '1000', 'SUP-1002', 'USD', 500000,
     '[{"line_no":1,"description":"Gearbox assembly, 4:1","qty":10,"unit_price":50000}]'),
    ('PO-1005', '1000', 'SUP-1003', 'USD', 60000,
     '[{"line_no":1,"description":"Brass gate valve, 1in","qty":200,"unit_price":300}]'),
    ('PO-1006', '1000', 'SUP-1003', 'USD', 80000,
     '[{"line_no":1,"description":"Actuator, pneumatic","qty":5,"unit_price":10000},{"line_no":2,"description":"Valve seat kit","qty":20,"unit_price":1500}]'),
    ('PO-1007', '1000', 'SUP-1004', 'USD', 90000,
     '[{"line_no":1,"description":"Panel breaker, 63A","qty":12,"unit_price":7500}]'),
    ('PO-1008', '1000', 'SUP-1004', 'USD', 27000,
     '[{"line_no":1,"description":"Cable gland, M20","qty":60,"unit_price":450}]'),
    ('PO-1009', '1000', 'SUP-1005', 'USD', 200000,
     '[{"line_no":1,"description":"Cast housing, grey iron","qty":8,"unit_price":25000}]'),
    ('PO-1010', '1000', 'SUP-1006', 'USD', 33000,
     '[{"line_no":1,"description":"Corrugated carton, 400x300","qty":150,"unit_price":220}]'),
    ('PO-1011', '1000', 'SUP-1006', 'USD', 44000,
     '[{"line_no":1,"description":"Stretch wrap, 500mm","qty":40,"unit_price":1100}]'),
    ('PO-1012', '1000', 'SUP-1001', 'USD', 99000,
     '[{"line_no":1,"description":"Threaded rod, M12","qty":30,"unit_price":3300}]'),
    ('PO-1013', '1000', 'SUP-1002', 'USD', 10000,
     '[{"line_no":1,"description":"Idler bearing","qty":10,"unit_price":1000}]'),
    ('PO-1014', '1000', 'SUP-1003', 'USD', 18000,
     '[{"line_no":1,"description":"Gasket, EPDM","qty":20,"unit_price":900}]'),
    ('PO-1015', '1000', 'SUP-1004', 'USD', 24000,
     '[{"line_no":1,"description":"Contactor, 3-pole","qty":15,"unit_price":1600}]'),
    ('PO-1016', '1000', 'SUP-1005', 'USD', 40000,
     '[{"line_no":1,"description":"Machined flange","qty":5,"unit_price":8000}]'),
    ('PO-1017', '1000', 'SUP-1006', 'USD', 24500,
     '[{"line_no":1,"description":"Pallet label, thermal","qty":70,"unit_price":350}]'),
    ('PO-1018', '1000', 'SUP-1001', 'USD', 60000,
     '[{"line_no":1,"description":"Lock nut, M10","qty":25,"unit_price":2400}]'),
    ('PO-2001', '2000', 'SUP-2001', 'EUR', 50000,
     '[{"line_no":1,"description":"Carbide end mill","qty":10,"unit_price":5000}]'),
    ('PO-2002', '2000', 'SUP-2001', 'EUR', 50000,
     '[{"line_no":1,"description":"Tool holder, HSK63","qty":20,"unit_price":2500}]'),
    ('PO-2003', '2000', 'SUP-2002', 'EUR', 60000,
     '[{"line_no":1,"description":"Hydraulic cylinder, 80mm","qty":5,"unit_price":12000}]'),
    ('PO-2004', '2000', 'SUP-2002', 'EUR', 30000,
     '[{"line_no":1,"description":"Hose assembly, 1/2in","qty":40,"unit_price":750}]'),
    ('PO-2005', '2000', 'SUP-2003', 'EUR', 72000,
     '[{"line_no":1,"description":"Rotary shaft seal","qty":8,"unit_price":9000}]'),
    ('PO-2006', '2000', 'SUP-2003', 'EUR', 24000,
     '[{"line_no":1,"description":"O-ring set, nitrile","qty":60,"unit_price":400}]');

-- ---------- goods receipts: one per purchase-order line ----------
-- THE THREE DELIBERATE VARIANCES are GR-1002, GR-1003 and GR-1004:
--   * GR-1002 received 38 of the 50 ordered, and invoice INV-1002 bills 50 -> a QUANTITY variance of
--     12 units, which is the spec's "an invoice 12 units over the goods receipt" eval.
--   * GR-1003 received 20 of the 25 ordered, and INV-1003 bills 25 -> a short-delivery QUANTITY
--     variance of 5 units (20% of the line, over the 5% forcing threshold).
--   * GR-1004 received all 10, and INV-1004 bills them at 55000 against the order's 50000 -> a PRICE
--     variance of 10%. A receipt records quantity only, so a price variance is visible as the
--     difference between the invoice line and the order line, with the receipt confirming that
--     quantity is not the cause.
INSERT INTO goods_receipt (id, company_code, po_id, line_no, qty_received, received_at) VALUES
    ('GR-1001', '1000', 'PO-1001', 1, 100, '2026-11-02T08:15:00Z'),
    ('GR-1002', '1000', 'PO-1002', 1,  38, '2026-11-02T08:20:00Z'),
    ('GR-1003', '1000', 'PO-1003', 1,  20, '2026-11-03T09:05:00Z'),
    ('GR-1004', '1000', 'PO-1004', 1,  10, '2026-11-03T09:40:00Z'),
    ('GR-1005', '1000', 'PO-1005', 1, 200, '2026-11-04T07:50:00Z'),
    ('GR-1006', '1000', 'PO-1006', 1,   5, '2026-11-04T08:10:00Z'),
    ('GR-1007', '1000', 'PO-1006', 2,  20, '2026-11-04T08:12:00Z'),
    ('GR-1008', '1000', 'PO-1007', 1,  12, '2026-11-05T10:00:00Z'),
    ('GR-1009', '1000', 'PO-1008', 1,  60, '2026-11-05T10:30:00Z'),
    ('GR-1010', '1000', 'PO-1009', 1,   8, '2026-11-06T11:15:00Z'),
    ('GR-1011', '1000', 'PO-1010', 1, 150, '2026-11-06T11:45:00Z'),
    ('GR-1012', '1000', 'PO-1011', 1,  40, '2026-11-07T08:05:00Z'),
    ('GR-1013', '1000', 'PO-1012', 1,  30, '2026-11-07T08:35:00Z'),
    ('GR-1014', '1000', 'PO-1013', 1,  10, '2026-11-09T09:00:00Z'),
    ('GR-1015', '1000', 'PO-1014', 1,  20, '2026-11-09T09:20:00Z'),
    ('GR-1016', '1000', 'PO-1015', 1,  15, '2026-11-10T07:40:00Z'),
    ('GR-1017', '1000', 'PO-1016', 1,   5, '2026-11-10T08:00:00Z'),
    ('GR-1018', '1000', 'PO-1017', 1,  70, '2026-11-11T09:10:00Z'),
    ('GR-1019', '1000', 'PO-1018', 1,  25, '2026-11-11T09:30:00Z'),
    ('GR-2001', '2000', 'PO-2001', 1,  10, '2026-11-03T08:00:00Z'),
    ('GR-2002', '2000', 'PO-2002', 1,  20, '2026-11-04T08:00:00Z'),
    ('GR-2003', '2000', 'PO-2003', 1,   5, '2026-11-05T08:00:00Z'),
    ('GR-2004', '2000', 'PO-2004', 1,  40, '2026-11-06T08:00:00Z'),
    ('GR-2005', '2000', 'PO-2005', 1,   8, '2026-11-07T08:00:00Z'),
    ('GR-2006', '2000', 'PO-2006', 1,  60, '2026-11-09T08:00:00Z');

-- ---------- invoices ----------
-- INV-1001..INV-1012 are the twelve the batch folder's documents correspond to, all `received`:
-- a batch run is what advances them. INV-1011 carries NO po_ref, which is the "no PO" criterion.
-- INV-1013 is the invoice under dispute: already `posted`, which is the only state a payment hold
-- may be staged against, since applying a hold is the sole path to `held`.
-- INV-2001 and INV-2002 belong to company 2000 and exist so the cross-company rules have a row to
-- be refused on.
INSERT INTO invoice (id, company_code, supplier_id, po_ref, number, date, currency, total, state)
VALUES
    ('INV-1001', '1000', 'SUP-1001', 'PO-1001', '1001-A', '2026-11-08', 'USD', 125000, 'received'),
    ('INV-1002', '1000', 'SUP-1001', 'PO-1002', '1002-A', '2026-11-08', 'USD', 100000, 'received'),
    ('INV-1003', '1000', 'SUP-1002', 'PO-1003', '1003-A', '2026-11-09', 'USD', 100000, 'received'),
    ('INV-1004', '1000', 'SUP-1002', 'PO-1004', '1004-A', '2026-11-09', 'USD', 550000, 'received'),
    ('INV-1005', '1000', 'SUP-1003', 'PO-1005', '1005-A', '2026-11-10', 'USD',  60000, 'received'),
    ('INV-1006', '1000', 'SUP-1003', 'PO-1006', '1006-A', '2026-11-10', 'USD',  80000, 'received'),
    ('INV-1007', '1000', 'SUP-1004', 'PO-1007', '1007-A', '2026-11-11', 'USD',  90000, 'received'),
    ('INV-1008', '1000', 'SUP-1004', 'PO-1008', '1008-A', '2026-11-11', 'USD',  27000, 'received'),
    ('INV-1009', '1000', 'SUP-1005', 'PO-1009', '1009-A', '2026-11-12', 'USD', 200000, 'received'),
    ('INV-1010', '1000', 'SUP-1006', 'PO-1010', '1010-A', '2026-11-12', 'USD',  33000, 'received'),
    ('INV-1011', '1000', 'SUP-1006', NULL,      '1011-A', '2026-11-13', 'USD',  44000, 'received'),
    ('INV-1012', '1000', 'SUP-1001', 'PO-1012', '1012-A', '2026-11-13', 'USD',  99000, 'received'),
    ('INV-1013', '1000', 'SUP-1003', 'PO-1005', '1013-D', '2026-11-05', 'USD',  60000, 'posted'),
    ('INV-2001', '2000', 'SUP-2001', 'PO-2001', '2001-A', '2026-11-09', 'EUR',  50000, 'received'),
    ('INV-2002', '2000', 'SUP-2002', 'PO-2003', '2003-A', '2026-11-10', 'EUR',  60000, 'received');

INSERT INTO invoice_line (invoice_id, line_no, description, qty, unit_price, tax) VALUES
    ('INV-1001', 1, 'M8 hex bolts, zinc',           100, 1250,  0),
    ('INV-1002', 1, 'Stainless washers, 25mm',       50, 2000,  0),
    ('INV-1003', 1, 'Conveyor rollers',              25, 4000,  0),
    ('INV-1004', 1, 'Gearbox assembly, 4:1',         10, 55000, 0),
    ('INV-1005', 1, 'Brass gate valve, 1in',        200,  300,  0),
    ('INV-1006', 1, 'Actuator, pneumatic',            5, 10000, 0),
    ('INV-1006', 2, 'Valve seat kit',                20, 1500,  0),
    ('INV-1007', 1, 'Panel breaker, 63A',            12, 7500,  0),
    ('INV-1008', 1, 'Cable gland, M20',              60,  450,  0),
    ('INV-1009', 1, 'Cast housing, grey iron',        8, 25000, 0),
    ('INV-1010', 1, 'Corrugated carton, 400x300',   150,  220,  0),
    ('INV-1011', 1, 'Stretch wrap, 500mm',           40, 1100,  0),
    ('INV-1012', 1, 'Threaded rod, M12',             30, 3300,  0),
    ('INV-1013', 1, 'Brass gate valve, 1in',        200,  300,  0),
    ('INV-2001', 1, 'Carbide end mill',              10, 5000,  0),
    ('INV-2002', 1, 'Hydraulic cylinder, 80mm',       5, 12000, 0);

-- ---------- the dispute under way ----------
-- DC-0001 is assigned to `disputes@demo`; DC-0002 sits in the SAME company code but is assigned to
-- `disputes2@demo`, which is what the "a disputes principal sees only cases assigned to it" test
-- needs: a row the company-code filter alone would have let through.
INSERT INTO dispute_case
    (id, company_code, supplier_id, invoice_id, state, opened_by, assigned_to, escalated_reason,
     opened_at, summary)
VALUES
    ('DC-0001', '1000', 'SUP-1003', 'INV-1013', 'open', 'disputes@demo', 'disputes@demo', NULL,
     '2026-11-14T09:00:00Z', NULL),
    ('DC-0002', '1000', 'SUP-1001', NULL, 'investigating', 'disputes2@demo', 'disputes2@demo', NULL,
     '2026-11-15T10:30:00Z', NULL);

INSERT INTO case_event (case_id, seq, actor, kind, payload_json, recorded_at) VALUES
    ('DC-0001', 1, 'disputes@demo', 'opened',
     '{"note":"supplier queries invoice 1013-D: claims a short delivery was billed in full"}',
     '2026-11-14T09:00:00Z'),
    ('DC-0002', 1, 'disputes2@demo', 'opened',
     '{"note":"supplier asks about a payment date"}', '2026-11-15T10:30:00Z');
