"""The lane must be able to read its own outcomes (2026-08-03).

Three kinds of evidence sat on disk and never reached the planner:

  - proposal.json stores the sandbox {passed, summary}, the reviewer's
    {verdict, reasons, revision_instructions, error} and the retire_reason,
    and the PAST PROPOSALS section — titled "learn from outcomes" — stripped
    every one of them. Six consecutive proposals died on 2026-08-01/02 and
    each following planner re-specced blind against a wall it could not see.
  - RESULTS.jsonl 'error' strings were dropped by _research_ledger, so an
    ERROR verdict arrived without its cause (the engineer-side twin of the
    same defect on the director side).
  - _MAX_FILE_CHARS cut the planner's two hottest files — runtime/live.py and
    research/engineer.py — out of its own view of the tree.

These fail by construction on the old code: the extra keys did not exist, and
whole-file inclusion of live.py cannot hold at a 60k cap.

Offline and deterministic: no network, no writes outside tmp_path.
"""
import json

import research.engineer as eng


def _section(text: str, header: str) -> str:
    """The body of one '=== HEADER ... ===' block, up to the next block."""
    i = text.index(header)
    body = text[text.index("\n", i) + 1:]
    cut = body.find("\n\n=== ")
    return body if cut == -1 else body[:cut]


# ------------------------------------------------------- _past_proposals --
def test_red_sandbox_carries_the_TAIL_of_the_summary():
    """The end of a pytest summary names the failing tests; the head is noise."""
    summary = "STARTMARK" + "x" * 984 + "ENDMARK"
    assert len(summary) == 1000
    row = eng._past_proposals([{"id": "P0029", "title": "t", "status": "retired",
                                "sandbox": {"passed": False,
                                            "summary": summary}}])[0]

    assert len(row["sandbox_failed"]) <= 400
    assert "ENDMARK" in row["sandbox_failed"]
    assert "STARTMARK" not in row["sandbox_failed"]


def test_a_green_record_keeps_exactly_the_five_base_keys():
    row = eng._past_proposals([{"id": "P0030", "title": "t", "status": "applied",
                                "reject_reason": None,
                                "apply_note": "applied @x",
                                "sandbox": {"passed": True}}])[0]

    assert list(row) == ["id", "title", "status", "reject_reason", "apply_note"]


def test_a_plain_record_serializes_exactly_as_before():
    p = {"id": "P0031", "title": "t", "status": "applied",
         "reject_reason": None, "apply_note": "applied @x"}
    old = [{"id": p["id"], "title": p.get("title"), "status": p.get("status"),
            "reject_reason": p.get("reject_reason"),
            "apply_note": p.get("apply_note")}]

    assert (json.dumps(eng._past_proposals([p]), indent=1)
            == json.dumps(old, indent=1))


def test_review_verdict_is_carried_with_only_its_truthy_keys():
    row = eng._past_proposals([{
        "id": "P0032", "title": "t", "status": "pending",
        "review": {"verdict": "revise", "reasons": "close",
                   "revision_instructions": "tighten", "error": None}}])[0]

    assert row["review"] == {"verdict": "revise", "reasons": "close",
                            "revision_instructions": "tighten"}


def test_an_empty_review_adds_no_key():
    row = eng._past_proposals([{"id": "P0033", "title": "t",
                                "status": "pending", "review": {}}])[0]
    assert "review" not in row


def test_retire_reason_is_carried():
    row = eng._past_proposals([{
        "id": "P0034", "title": "t", "status": "retired",
        "retire_reason": "unreviewed for more than 3 days"}])[0]

    assert row["retire_reason"] == "unreviewed for more than 3 days"


# ------------------------------------------------------ _research_ledger --
def test_an_ERROR_row_carries_its_cause(tmp_path, monkeypatch):
    """An ERROR verdict without its cause is unactionable — the reason is in
    the row and was being stripped. Healthy rows are untouched.

    _research_ledger also embeds research_liveness/forward_oos, which read the
    REAL research/ files read-only inside try blocks; nothing here asserts on
    those sections. The tmp repo has no registry.json, so FAMILIES is absent.
    """
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    (tmp_path / "research").mkdir()
    rows = [
        {"id": "R0001", "spec": {"name": "rv_instrument"}, "family": "vol",
         "phase": "explore", "verdict": "ERROR", "metrics": None,
         "error": "RuntimeError: SPY missing"},
        {"id": "R0002", "spec": {"name": "mom_63"}, "family": "momentum",
         "phase": "explore", "verdict": "FAIL", "metrics": {"sharpe": 0.1}},
    ]
    (tmp_path / "research" / "RESULTS.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")

    parsed = json.loads(_section(eng._research_ledger(),
                                 "=== RESEARCH RESULTS"))

    assert [r["id"] for r in parsed] == ["R0001", "R0002"]
    assert "SPY missing" in parsed[0]["error"]
    assert "error" not in parsed[1], "a healthy row must serialize as before"


# ---------------------------------------------------- the planner's view --
def test_the_hot_files_arrive_whole_in_the_planner_context():
    """The E42 whole-file standard, applied to the planner's own view: it is
    the nightly reviewer of these two files and cannot spec edits to code it
    reads with its middle cut out."""
    ctx = eng.build_context()

    for rel in ("runtime/live.py", "research/engineer.py"):
        text = (eng.ROOT / rel).read_text()
        assert len(text) <= eng._MAX_FILE_CHARS, (
            f"{rel}: the file outgrew the planner cap — raise _MAX_FILE_CHARS")
        assert text in ctx, f"{rel} reaches the planner truncated"


def test_a_red_sandbox_tail_reaches_the_past_proposals_section(monkeypatch):
    """End to end: a corpse's pytest tail is readable where the planner looks.

    The token is checked INSIDE the section, because this test file is itself
    part of the SOURCE TREE the same context carries.
    """
    token = "SBXTAIL" + "9174"
    rec = {"id": "P9999", "title": "wire the realized-vol instrument",
           "status": "retired",
           "sandbox": {"passed": False,
                       "summary": "=" * 500 + "\nFAILED tests/test_x.py :: "
                                  + token},
           "retire_reason": "sandbox red and nothing in the lane can apply it"}
    monkeypatch.setattr(eng, "list_proposals", lambda: [rec])

    section = _section(eng.build_context(), "=== YOUR PAST PROPOSALS")

    assert token in section
    assert "sandbox red and nothing in the lane" in section
