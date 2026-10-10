"""Consistency checks for README-reported historical summaries."""
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from benchmarks.verify_readme_results import verify

class ReadmeResultTests(unittest.TestCase):
    def test_published_aggregates_match_root_readme(self):
        self.assertTrue(verify())

if __name__ == '__main__':
    unittest.main()
