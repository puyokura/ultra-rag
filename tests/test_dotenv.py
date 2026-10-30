from __future__ import annotations

import unittest

from secret_core.dotenv import DotenvError, parse_dotenv


class DotenvParserTests(unittest.TestCase):
    def test_parses_assignments_export_quotes_and_comments(self) -> None:
        entries = parse_dotenv(
            "\ufeff# owner keys\n"
            "KEY_NAME=key-~~~\n"
            "export TOKEN=\"a\\nb\"\n"
            "SINGLE='literal # value'\n"
            "URL=https://example.test/path#fragment\n"
            "PASSWORD=abc # trailing comment\n"
        )
        values = {entry.key: entry.value for entry in entries}
        self.assertEqual(values["KEY_NAME"], "key-~~~")
        self.assertEqual(values["TOKEN"], "a\nb")
        self.assertEqual(values["SINGLE"], "literal # value")
        self.assertEqual(values["URL"], "https://example.test/path#fragment")
        self.assertEqual(values["PASSWORD"], "abc")

    def test_duplicate_key_is_rejected(self) -> None:
        with self.assertRaises(DotenvError):
            parse_dotenv("A=1\nA=2\n")

    def test_empty_value_is_rejected(self) -> None:
        with self.assertRaises(DotenvError):
            parse_dotenv("A=\n")

    def test_lowercase_key_is_rejected(self) -> None:
        with self.assertRaises(DotenvError):
            parse_dotenv("lower=value\n")

    def test_unterminated_quote_is_rejected(self) -> None:
        with self.assertRaises(DotenvError):
            parse_dotenv('A="unterminated\n')

    def test_empty_file_is_rejected(self) -> None:
        with self.assertRaises(DotenvError):
            parse_dotenv("# only comments\n\n")


if __name__ == "__main__":
    unittest.main()
