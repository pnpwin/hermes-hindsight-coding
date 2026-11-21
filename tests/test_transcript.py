import importlib.util
import sys
import unittest

from test_settings import ROOT


class TranscriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('_coding_transcript_test', ROOT / 'transcript.py')
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def snapshot(self, rows, user='ask', answer='done', **kw):
        return self.module.normalize(rows, user, answer, anchor='2026-04-05T02:00:00Z', **kw)

    def test_only_supported_text_and_tool_names_cross_boundary(self):
        rows = [
            {'role': 'system', 'content': 'SECRET_SYSTEM'},
            {'role': 'developer', 'content': 'SECRET_DEVELOPER'},
            {'role': 'user', 'content': 'hidden bootstrap', 'display_kind': 'bootstrap'},
            {'role': 'user', 'content': 'blocked', 'display_metadata': {'withheld_prompt': 'SECRET_WITHHELD'}},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'ask'}, {'type': 'image_url', 'image_url': 'SECRET_IMAGE'}],
             'api_content': 'SECRET_INJECTION', 'timestamp': '2026-04-05T05:00:01+03:00'},
            {'role': 'assistant', 'content': None, 'reasoning_content': 'SECRET_THINKING', 'tool_calls': [
                {'function': {'name': 'terminal', 'arguments': 'SECRET_ARGS'}},
                {'function': {'name': 'terminal /private/key', 'arguments': 'SECRET_PATH'}},
            ]},
            {'role': 'tool', 'content': 'SECRET_OUTPUT'},
            {'role': 'assistant', 'content': 'done\n<hindsight_memory>SECRET_RECALL</hindsight_memory>'},
        ]
        result = self.snapshot(rows)
        self.assertEqual([(r['role'], r['content']) for r in result], [('user', 'ask'), ('action', 'terminal'), ('assistant', 'done')])
        self.assertEqual(result[0]['timestamp'], '2026-04-05T02:00:01.000Z')
        self.assertNotIn('SECRET', repr(result))

    def test_final_reply_is_added_once_and_duplicates_across_turns_are_preserved(self):
        rows = [{'role': 'user', 'content': 'ask'}, {'role': 'assistant', 'content': 'done'},
                {'role': 'user', 'content': 'ask'}]
        before = self.snapshot(rows)
        after = self.snapshot(rows + [{'role': 'assistant', 'content': 'done'}])
        self.assertEqual(before, after)
        self.assertEqual([r['content'] for r in after], ['ask', 'done', 'ask', 'done'])
        self.assertEqual(self.snapshot(rows[:2]), after[:2])
        self.assertEqual(len({r['timestamp'] for r in after}), 4)

    def test_preserves_old_timestamp_when_host_later_stamps_a_saved_reply(self):
        rows = [{'role': 'user', 'content': 'ask'}]
        previous = self.snapshot(rows)
        updated = rows + [{'role': 'assistant', 'content': 'done', 'timestamp': '2026-04-05T02:00:02Z'}]
        self.assertEqual(self.snapshot(updated, previous=previous), previous)

    def test_image_only_and_withheld_placeholders_are_not_human_turns(self):
        from session_rules.turns import COMPRESSION_CONTINUATION_USER_CONTENT

        for content in (COMPRESSION_CONTINUATION_USER_CONTENT, [{'type': 'image_url', 'image_url': 'data:fixture'}]):
            with self.subTest(content=content), self.assertRaises(self.module.TranscriptError):
                self.snapshot([{'role': 'user', 'content': content}], user=content if isinstance(content, str) else '[1 image]')

    def test_data_uri_text_is_never_forwarded(self):
        secret = 'data: private text that the host redactor skipped'
        for content in (secret, [{'type': 'text', 'text': secret}]):
            with self.subTest(content=content), self.assertRaises(self.module.TranscriptError):
                self.snapshot([{'role': 'user', 'content': content}], user=secret)

    def test_multi_turn_rewind_keeps_only_verified_prefix_and_new_tail(self):
        rows = [{'role': role, 'content': f'{role} {i}'} for i in range(3) for role in ('user', 'assistant')]
        previous = self.snapshot(rows, user='user 2', answer='assistant 2')
        correction = rows[:2] + [{'role': 'user', 'content': 'new question', 'timestamp': '2026-04-05T02:00:05Z'}]
        actual = self.snapshot(correction, user='new question', previous=previous, rewound=True,
                               rewind_after='2026-04-05T02:00:03Z')
        self.assertEqual(actual[:2], previous[:2])
        self.assertEqual([r['content'] for r in actual[2:]], ['new question', 'done'])

    def test_rejects_missing_provenance_summaries_or_unbalanced_fences(self):
        for rows in (None, [], [{'role': 'assistant', 'content': 'done'}],
                     [{'role': 'user', 'content': 'different user'}],
                     [{'role': 'user', 'content': 'ask', '_is_context_summary': True}],
                     [{'role': 'user', 'content': 'ask\n<memory-context>SECRET_INCOMPLETE'}]):
            with self.subTest(rows=rows), self.assertRaises(self.module.TranscriptError):
                self.snapshot(rows)

    def test_declines_oversized_or_partial_snapshots_instead_of_replacing(self):
        with self.assertRaises(self.module.TranscriptError):
            self.snapshot([{'role': 'user', 'content': 'ask'}, {'role': 'assistant', 'content': 'x' * 600000}], answer='x' * 600000)
        previous = self.snapshot([{'role': 'user', 'content': 'older'}, {'role': 'assistant', 'content': 'first'}],
                                 user='older', answer='first')
        current = [{'role': 'user', 'content': 'ask', 'timestamp': '2026-04-05T02:00:05Z'}]
        with self.assertRaises(self.module.TranscriptError):
            self.snapshot(current, previous=previous)
        self.assertEqual(self.snapshot(current, previous=previous, rewound=True,
                                       rewind_after='2026-04-05T02:00:04Z')[0]['content'], 'ask')
