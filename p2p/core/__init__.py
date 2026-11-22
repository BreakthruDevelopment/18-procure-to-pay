"""Foundation package: the simulated ERP, the access rules and the tool adapter (slice 18.2).

Wave 1 owns this path. A later slice adds files here only if a security-sensitive `claude` slice
names it as the owner; a slice that needs a change here stops and comments on its issue instead.

* `erp.py` -- the only module that opens the database, and every accessor over it.
* `access.py` -- the four roles and `assert_can`, the one authorization in the app.
* `adapter.py` -- an ERP call as an app-17 `Tool`, so the gateway and the loop share one.
* `errors.py` -- the failures this app reports, each carrying its app-17 `error_category`.
"""
