"""Result parser parity without request-model changes."""
import unittest
from hermes_hostd.models import CommandResult


class ResultValidationTests(unittest.TestCase):
    def test_strict_result_fields(self):
        base = {'id': 'fixture', 'approved': False}
        invalid = {'approved': ['false', 'true', 0, 1, None, [], {}],
                   'id': [None, 1, True, [], {}],
                   'exit_code': [True, False, 0.0, '0', [], {}],
                   'stdout': [None, 1, [], {}], 'stderr': [None, False, []],
                   'error': [1, False, [], {}], 'log_id': [1, False, []],
                   'output_truncated': [0, None, 'false', []],
                   'persistent_intent': [0, None, 'false', []]}
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, kind=type(value).__name__):
                    with self.assertRaises((ValueError, TypeError)):
                        CommandResult.from_dict(dict(base, **{field: value}))
        for value in (None, [], 1, True, 'text', {}, {'id': 'fixture'}, {'approved': True}):
            with self.assertRaises((ValueError, TypeError)):
                CommandResult.from_dict(value)

    def test_metadata_and_ordinary_outcomes_roundtrip(self):
        for approved, code, error in ((False, None, 'denied'), (True, 0, None),
                (True, None, 'timeout'), (True, 0, 'capture error'), (True, 2, None)):
            wire = dict(id='fixture', approved=approved, exit_code=code, stdout='tail', stderr='',
                        error=error, log_id='opaque', output_truncated=True, persistent_intent=True,
                        future={'version': [1, 'compatible']})
            self.assertEqual(CommandResult.from_dict(wire).to_dict(), wire)


if __name__ == '__main__':
    unittest.main()
