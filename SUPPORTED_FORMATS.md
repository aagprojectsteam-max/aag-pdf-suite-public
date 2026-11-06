# Supported input and truthful outcomes

The app reads files with the `.pdf` extension only. Anything else is refused before any work starts. The extension alone never authorizes a repair: the decision comes from what is proven inside the file.

| What is found in the file | Outcome |
| --- | --- |
| A PDF that passes the strict checks | PASS_EXACT: byte-for-byte copy |
| A complete PDF after unexplained leading bytes | PASS_DECODED: the embedded PDF is extracted unchanged |
| Complete gzip, zlib, bzip2 or XZ encoding of one PDF, or a single-member ZIP holding one PDF | PASS_DECODED after all pages render |
| Surviving catalog and page graph, damaged or missing cross-reference table, header or trailer | PASS_REPAIRED: new structure around the original object and stream bytes |
| Missing catalog, one complete unambiguous page root | PASS_REPAIRED, with the rebuilt structure recorded |
| Wrong page count or parent entries, one complete order of pages | PASS_REPAIRED: metadata only |
| One JPEG, or a complete ZIP sequence `1.jpg` to `N.jpg` | PASS_REPAIRED: original JPEG streams copied, with the order and size assumptions recorded |
| Recoverable structure when `--recovery exact` forbids rebuilding | NEW_VARIANT_DETECTED: nothing published |
| Nothing safe to recover, or the analysis budget ran out | UNSUPPORTED_AFTER_ANALYSIS: nothing published |

A passing result means the file is structurally valid and renders. It does not prove the result equals a lost original, which is why reports mark fidelity to the original as not proven.

Refused on purpose: missing page order, an absent content stream, ambiguous roots or revisions, a surviving security dictionary, unsupported filters. Image recovery is limited to explicit framing and order in the source. Orphan images in an incomplete PDF are not turned into pages.
