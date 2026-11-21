from pathlib import Path
import asyncio
from array import array
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'worker'))
from native import NativeSender, append_silence, prepend_silence, playback_wait, start_timing


class NativeTimingTests(unittest.TestCase):
    def test_future_anchor_is_fully_waited(self):
        self.assertAlmostEqual(playback_wait(105000, 4.81, 100), 10.56)

    def test_past_anchor_does_not_shorten_message(self):
        self.assertAlmostEqual(playback_wait(95000, 4.81, 100), 5.56)

    def test_silence_keeps_all_speech_samples(self):
        source = b'\x01\x02\x03\x04' * 100
        padded = append_silence(source)
        self.assertEqual(padded[:len(source)], source)
        self.assertEqual(len(padded) - len(source), 3 * 44100 * 4)
        self.assertEqual(set(padded[len(source):]), {0})

    def test_invalid_pcm_and_padding_are_rejected(self):
        for data, seconds in [(b'x', 3), (b'', -1), (b'', 6)]:
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                append_silence(data, seconds)

    def test_pre_roll_keeps_every_speech_sample_and_full_tail(self):
        source = b'\x01\x02\x03\x04' * 100
        result = append_silence(prepend_silence(source))
        lead = round(44100 * .7) * 4
        self.assertEqual(result[:lead], bytes(lead))
        self.assertEqual(result[lead:lead + len(source)], source)
        self.assertEqual(len(result), lead + len(source) + 44100 * 4 * 3)

    def test_invalid_pre_roll_is_rejected(self):
        for data, seconds in [(b'x', .7), (b'', -1), (b'', 2.1), (b'', float('nan'))]:
            with self.assertRaises(ValueError):
                prepend_silence(data, seconds)

    def test_start_timing_separates_receiver_buffer_from_optional_pre_roll(self):
        self.assertEqual(start_timing(102000, .7, 100), {'receiverBufferMs':2000,'preRollMs':700})
        self.assertEqual(start_timing(102000, 0, 100), {'receiverBufferMs':2000,'preRollMs':0})
        self.assertEqual(start_timing(99000, .3, 100), {'receiverBufferMs':0,'preRollMs':300})

    def test_configured_pre_roll_is_applied_and_disabled_value_is_retained(self):
        from airplay import AirPlayBackend
        base = {'network':'10.0.0.0/24','host':'10.0.0.210'}
        self.assertEqual(AirPlayBackend(Path('/test'),base).native.lead_seconds,.7)
        self.assertEqual(AirPlayBackend(Path('/test'),{**base,'airplayPreRollMs':350}).native.lead_seconds,.35)
        self.assertEqual(AirPlayBackend(Path('/test'),{**base,'airplayPreRollMs':350,'airplayPreRollEnabled':False}).native.lead_seconds,0)
        for patch_value in [{'airplayPreRollMs':True},{'airplayPreRollMs':2001},{'airplayPreRollEnabled':'false'}]:
            with self.assertRaises(ValueError):
                AirPlayBackend(Path('/test'),{**base,**patch_value})
