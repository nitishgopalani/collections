"""Unit tests for the incremental sentence-boundary splitter."""

from __future__ import annotations

from app.engine.stream_sentences import SentenceStreamSplitter


def _feed(tokens: list[str]) -> list[str]:
    splitter = SentenceStreamSplitter()
    out: list[str] = []
    for tok in tokens:
        out.extend(splitter.push(tok))
    out.extend(splitter.flush())
    return out


def test_basic_sentences_split_as_tokens_arrive():
    splitter = SentenceStreamSplitter()
    assert splitter.push("Namaste! ") == ["Namaste!"]
    assert splitter.push("Booking ID ") == []
    assert splitter.push("bataiye? Theek") == ["Booking ID bataiye?"]
    assert splitter.flush() == ["Theek"]


def test_terminator_at_buffer_end_waits_for_next_token():
    """A trailing '.' may be a decimal about to continue — it must not split
    until the following token proves it's a real boundary."""
    splitter = SentenceStreamSplitter()
    assert splitter.push("Amount 3.") == []
    # "3.5" glued back together; the trailing ". " then confirms the boundary.
    assert splitter.push("5 percent hai. ") == ["Amount 3.5 percent hai."]
    assert splitter.push("Theek hai?") == []
    assert splitter.flush() == ["Theek hai?"]


def test_hindi_danda_boundaries():
    out = _feed(["आपकी बुकिंग कन्फर्म है। ", "धन्यवाद॥ ", "फिर मिलेंगे"])
    assert out == ["आपकी बुकिंग कन्फर्म है।", "धन्यवाद॥", "फिर मिलेंगे"]


def test_abbreviations_and_initials_do_not_split():
    out = _feed(["Rs. 500 due hai. ", "Dr. R. Sharma se baat hui. ", "done"])
    assert out == ["Rs. 500 due hai.", "Dr. R. Sharma se baat hui.", "done"]


def test_decimals_and_ids_do_not_split():
    out = _feed(["Booking v2.1 hai aur rate 4.5 hai. ", "ok"])
    assert out == ["Booking v2.1 hai aur rate 4.5 hai.", "ok"]


def test_newline_is_a_boundary():
    out = _feed(["pehli line\ndoosri", " line"])
    assert out == ["pehli line", "doosri line"]


def test_marker_split_across_tokens_stays_whole():
    """<consult ...> arriving in pieces must never be cut mid-marker, even when
    its attributes contain sentence terminators."""
    out = _feed(
        [
            "Line par bane rahiye. ",
            "<consult booking_id=BK1",
            '23 hotel="Hotel Sun',
            'rise. Deluxe" guest=Rahul ',
            "phone=9990001111>",
        ]
    )
    assert out == [
        "Line par bane rahiye.",
        '<consult booking_id=BK123 hotel="Hotel Sunrise. Deluxe" guest=Rahul phone=9990001111>',
    ]


def test_marker_followed_by_text_still_splits_after_it():
    splitter = SentenceStreamSplitter()
    got = splitter.push("<consult_result booking_id=BK9 confirmed=yes note=ok> Shukriya. Bye")
    got.extend(splitter.flush())
    assert got == ["<consult_result booking_id=BK9 confirmed=yes note=ok> Shukriya.", "Bye"]


def test_exclamation_and_question_with_quotes():
    out = _feed(['Usne kaha "haan!" ', "phir kya? ", "bas"])
    assert out == ['Usne kaha "haan!"', "phir kya?", "bas"]


def test_flush_empty_and_whitespace():
    splitter = SentenceStreamSplitter()
    assert splitter.push("   ") == []
    assert splitter.flush() == []
    assert splitter.flush() == []
