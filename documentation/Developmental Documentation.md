#### **Things to Confirm Before Going Live**

- [ ] Connect to live AWP-SQL-PROD and verify manifests pull correctly
- [ ] Upload a real ALG invoice CSV and confirm matching works
- [ ] Verify calculated cost against a known invoice (pick one and walk through the math)
- [ ] Confirm VisualMail SELECT permission granted on AWD-SQL-WH4 (need Megha)
- [ ] Confirm destination ZIP field in VisualMail (`Locations.AccountNumber` vs `DestinationID`) — need Marge
- [ ] SMTP credentials configured so the accounting email actually sends
- [ ] Confirm SID file format still matches current Prophecy import expectations