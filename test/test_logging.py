import io
import json
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'worker'))
from server import JsonLogHandler


class DetailedLoggingTests(unittest.TestCase):
    def test_error_reason_and_trace_reach_iobroker_within_each_line_limit(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            try:
                raise RuntimeError('Previous audio stream is not responding')
            except RuntimeError:
                record = logging.LogRecord('airplay-mm', logging.ERROR, __file__, 1,
                    'Announcement failed: %s', ('Audio stream is not responding',), sys.exc_info())
                JsonLogHandler().emit(record)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertIn('Audio stream is not responding', rows[0]['message'])
        self.assertTrue(any('RuntimeError' in row['message'] for row in rows[1:]))
        self.assertTrue(all(len(row['message']) < 2000 for row in rows))

    def test_credentials_are_redacted_before_trace_is_split(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            try:
                raise RuntimeError('x' * 1750 + ' CREDENTIALS: ' + 'ab' * 96 + ' 123-45-678')
            except RuntimeError:
                record = logging.LogRecord('airplay-mm', logging.ERROR, __file__, 1,
                    'Test', (), sys.exc_info())
                JsonLogHandler().emit(record)
        self.assertNotIn('ab' * 96, output.getvalue())
        self.assertNotIn('123-45-678', output.getvalue())
        self.assertIn('pairing key', output.getvalue())


if __name__ == '__main__':
    unittest.main()
