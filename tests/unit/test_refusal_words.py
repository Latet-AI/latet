"""What an agent is told of a stopped request, word for word: Latet named once, the reason, one period, nothing sent."""

from latet.gateway.forward import stopped
from latet.gateway.image_coverage import NO_OCR


def test_a_reason_that_ends_its_sentence_gets_one_period_before_nothing_was_sent():
    """The same words on a computer and on a server (one code path): where text recognition is installed is said for
    both, never a computer's command alone to a server's user."""
    assert stopped(NO_OCR) == (
        "Latet cannot read this image's text: text recognition is not installed. On a computer, latet ocr install "
        "installs it; on a Latet server, its administrator installs it there. Then rewind to before the image or start "
        "a new conversation, because the image stays in its history. Nothing was sent; run `latet dashboard` to see "
        "where it was"
    )
    assert stopped(NO_OCR, tutorial=True).endswith("its history. Nothing was sent; see where it was in Latet's pane")


def test_a_reason_without_a_period_is_told_as_before():
    assert stopped("request envelope refused (envelope-route)") == (
        "Latet: request envelope refused (envelope-route). Nothing was sent; run `latet dashboard` to see where it was"
    )
