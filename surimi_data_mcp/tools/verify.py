"""The `verify` tool: checks the signature of a calculation record and, when asked, recomputes the number
for records from `aggregate` and `query_data`.
"""
from surimi_data_mcp.engine import cube as cube_module
from surimi_data_mcp.mcp_server import mcp
from surimi_data_mcp.tools.common import query_client

# ── Receipt verify ──
# This server holds tabular data only. SURIMI-model-mcp serves the gridded data.
# Both servers sign with `SURIMI_RECEIPT_SECRET`, so a record from either one
# passes the signature check here.


@mcp.tool(name="verify")
def verify(receipt: dict, recompute: bool = False) -> dict:
    """Verify a signed calculation record (the `receipt`); with recompute=true also
    re-execute the record's logical query fresh against the live data and report
    {claimed, recomputed, match}. The signature is checked FIRST, and a tampered
    record is never recomputed. Recompute exists only for records from `aggregate`
    and `query_data`. Records from the model server (model_*, and the older nc_*
    from before the rename) are verified by signature here and recomputed
    nowhere, since no recompute path exists for them on either server
    (recompute_supported=false). Report that as verified by signature, not as
    a re-derived number.

    Call this WHENEVER the user asks to verify, check, re-check or confirm a
    number, or to demonstrate tampering. INVOKE it and report {verified, reason};
    never just describe what verification would do. Tamper demo: pass the
    modified receipt and report verified:false, reason:signature_mismatch."""
    from surimi_data_mcp.provenance.verification import verify_and_recompute
    return verify_and_recompute(
        receipt, recompute=recompute,
        cube_factory=cube_module.get_cube,
        # The same client as `query_data` (`query_client`), so a record served
        # from the cube recomputes against the cube and not the primary database.
        query_client_factory=query_client,
    )
