# Unseen-bid test

The assignment says the system will be tested on an unseen bid folder without code changes. This folder is that
test: a bid the system was never tuned on (different agency, product, ID format and file naming), with an
addendum that changes both the due date and the quantity.

- `Riverton-Network-Switches/`: portal page (HTML), 3-page RFQ (PDF), Addendum No. 1 (PDF)
- `make_unseen_bid.py`: regenerates those files
- `Riverton-Network-Switches.json`: the system's output, produced with no code or config changes

Run it yourself:

```powershell
python main.py --bid .\examples\unseen_bid\Riverton-Network-Switches
```

Result: **20 passed, 0 failed, 0 not found**; all 20 fields match the documents.

| Check | Expected (from the documents) | Output |
|---|---|---|
| Due Date | Addendum 1 extends it from 2025-03-14 to 2025-03-21 10:00 MST | 2025-03-21 10:00 MST (cites the addendum) |
| Product | Addendum 1 raises the quantity from 12 to 16 | 16 x Cisco Catalyst 9300 48-port PoE+ switches; 16 x power supplies |
| Bid Bond | 5% of total quoted price | Bid bond in the amount of five percent (5%) of the total quoted price |
| Part_no | C9300-48P-E; PWR-C1-1100WAC-P | C9300-48P-E; PWR-C1-1100WAC-P |
| Contact | Priya Raman, email, phone | Priya Raman \| praman@riverton-utilities.gov \| (555) 201-3344 |

Caveat: these generated PDFs are cleaner than real bid documents, so real unseen bids will be harder. The test shows that
nothing in the pipeline depends on Bid1 or Bid2.
