"""Build a realistic UNSEEN bid folder (different agency, domain, ids, file naming) to test generalisation.
Ground truth is printed at the end so extraction output can be compared."""
from pathlib import Path
import fitz

OUT = Path(__file__).resolve().parent / "Riverton-Network-Switches"
OUT.mkdir(parents=True, exist_ok=True)

(OUT / "City of Riverton - Bid Opportunity Details.html").write_text("""<!doctype html><html><head>
<title>RFQ-2025-0418 Network Switch Replacement | Riverton eBid</title></head><body>
<nav>Home | Open Bids | Vendor Login | Help</nav>
<div class="cookie">This site uses cookies. Accept</div>
<h1>Network Switch Replacement - Water Treatment Plant</h1>
<table class="bid-details">
<tr><th>Solicitation Number</th><td>RFQ-2025-0418</td></tr>
<tr><th>Status</th><td>OPEN</td></tr>
<tr><th>Issuing Agency</th><td>City of Riverton Public Utilities Department</td></tr>
<tr><th>Publication Date</th><td>02/10/2025</td></tr>
<tr><th>Closing Date</th><td>03/14/2025 10:00 AM MST</td></tr>
<tr><th>Contact</th><td>Priya Raman, Senior Buyer</td></tr>
<tr><th>Email</th><td>praman@riverton-utilities.gov</td></tr>
<tr><th>Phone</th><td>(555) 201-3344</td></tr>
</table>
<p>Documents: RFQ-2025-0418 Network Switches.pdf; Addendum No. 1.</p>
<footer>Copyright 2025 Riverton eBid. All rights reserved.</footer>
</body></html>""", encoding="utf-8")

RFQ = [
    """CITY OF RIVERTON PUBLIC UTILITIES DEPARTMENT
REQUEST FOR QUOTATION RFQ-2025-0418
NETWORK SWITCH REPLACEMENT - WATER TREATMENT PLANT

SECTION 1 - GENERAL INFORMATION
1.1 Purpose. The City of Riverton Public Utilities Department is requesting quotations for the purchase and
installation of network switches for the North Water Treatment Plant control network.
1.2 Quotation Due Date. Quotations must be received no later than March 14, 2025 at 10:00 AM Mountain
Standard Time (MST). Late quotations will not be considered.
1.3 Submission. Quotations shall be submitted electronically through the Riverton eBid vendor portal only.
Quotations submitted by email, fax or hand delivery will not be accepted.
1.4 Pre-Bid Meeting. A non-mandatory pre-bid meeting will be held virtually via Zoom on February 25, 2025 at
2:00 PM MST. Attendance is optional but encouraged.
1.5 Questions. All questions must be emailed to the Senior Buyer, Priya Raman, at praman@riverton-utilities.gov
no later than February 28, 2025.""",
    """SECTION 2 - SCOPE OF WORK AND SPECIFICATIONS
2.1 Quantity and Products. The City requires the following equipment:
Item 1: Twelve (12) Cisco Catalyst 9300 48-port PoE+ switches, manufacturer part number C9300-48P-E.
Item 2: Twelve (12) Cisco 1100W AC power supplies, part number PWR-C1-1100WAC-P.
2.2 Minimum Specifications. Each switch shall provide 48 x 1 Gigabit Ethernet PoE+ ports, 4 x 10G SFP+ uplink
ports, StackWise-480 stacking and a minimum PoE budget of 740 W.
2.3 Installation. The vendor shall rack-mount, cable and configure all switches on site at the North Water
Treatment Plant. Configuration must be performed by Cisco-certified technicians (CCNA or higher).
2.4 Delivery. All equipment shall be delivered within thirty (30) calendar days after receipt of the City's
purchase order, F.O.B. destination, to 4100 River Road, Riverton.
2.5 Warranty. A five (5) year manufacturer hardware warranty with next-business-day replacement is required.""",
    """SECTION 3 - COMMERCIAL TERMS
3.1 Bid Security. Each quotation must be accompanied by a bid bond in the amount of five percent (5%) of the
total quoted price.
3.2 Term. This is a one-time purchase. Quoted prices shall remain firm for one hundred twenty (120) days after
the quotation due date.
3.3 Payment Terms. Payment will be made Net 30 days after acceptance of the equipment and receipt of a correct
invoice. Invoices shall be sent to ap@riverton-utilities.gov.
3.4 Authorized Reseller. The vendor must be a Cisco Authorized Partner and shall provide a manufacturer letter of
authorization with the quotation.
3.5 Cooperative Contract. Vendors may quote pricing under the NASPO ValuePoint Data Communications cooperative
contract, if applicable.
3.6 Required Documents. The following must be submitted with the quotation: completed W-9 form; certificate of
insurance; signed Non-Collusion Affidavit; bid bond; signed acknowledgment of all addenda.""",
]
ADDENDUM = """CITY OF RIVERTON PUBLIC UTILITIES DEPARTMENT
ADDENDUM NO. 1 TO RFQ-2025-0418
Date of Addendum: March 3, 2025

This addendum modifies the Request for Quotation as follows:
1. The quotation due date is extended from March 14, 2025 at 10:00 AM MST to March 21, 2025 at 10:00 AM MST.
2. Section 2.1, Item 1 is revised: the quantity of Cisco Catalyst 9300 48-port PoE+ switches (C9300-48P-E)
is increased from twelve (12) to sixteen (16). Item 2 power supplies are increased from twelve (12) to sixteen (16).
3. All other terms and conditions remain unchanged.

Vendors must sign and return this addendum with their quotation."""


def pdf(path: Path, pages: list[str]):
    doc = fitz.open()
    for i, text in enumerate(pages, 1):
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(54, 54, 558, 760), text, fontsize=10, fontname="helv")
        page.insert_text((54, 790), f"RFQ-2025-0418    Page {i} of {len(pages)}", fontsize=8)
    doc.save(path)


pdf(OUT / "RFQ-2025-0418 Network Switches.pdf", RFQ)
pdf(OUT / "Addendum No. 1 - RFQ-2025-0418.pdf", [ADDENDUM])
print("created", OUT)
for f in sorted(OUT.iterdir()):
    print("  ", f.name)
