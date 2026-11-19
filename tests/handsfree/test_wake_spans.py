"""One saying of a wake word is one span, even when Whisper splits it or puts hey in front."""

import importlib.util
import unittest
from pathlib import Path

from tests import sandbox  # noqa: F401

_path = Path(__file__).resolve().parents[2] / "handsfree/tools/wakeword_train.py"
_spec = importlib.util.spec_from_file_location("wakeword_train", _path)
train = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(train)


class Word:
    def __init__(self, word, start, end):
        self.word, self.start, self.end = word, start, end


class Segment:
    def __init__(self, words):
        self.words = words


class Model:
    def __init__(self, words):
        self.words = words

    def transcribe(self, path, **_kwargs):
        return [Segment(self.words)], None


class Spans(unittest.TestCase):
    def test_hey_plus_the_word_is_one_span(self):
        model = Model([Word("hey", 0.1, 0.3), Word("jarvis", 0.4, 0.9)])
        self.assertEqual(len(train.hey_word_spans(model, "take.wav", "jarvis")), 1)

    def test_a_split_name_is_one_span(self):
        model = Model([Word("hench", 0.1, 0.4), Word("line", 0.4, 0.8)])
        self.assertEqual(len(train.hey_word_spans(model, "take.wav", "henchline")), 1)

    def test_two_sayings_stay_two(self):
        model = Model([Word("jarvis", 0.1, 0.4), Word("jarvis", 1.0, 1.4)])
        self.assertEqual(len(train.hey_word_spans(model, "take.wav", "jarvis")), 2)


if __name__ == "__main__":
    unittest.main()
