import json
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH = ROOT / "containers/seerr/patch-availability.cjs"


class SeerrAvailabilityTests(unittest.TestCase):
    def evaluate(self, expression):
        result = subprocess.run(
            ["node", "-e", f"const p=require({json.dumps(str(PATCH))}); console.log(JSON.stringify({expression}));"],
            check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def test_only_authoritative_positive_partial_counts_override_availability(self):
        self.assertEqual(self.evaluate("""[
          p.confirmedPartial({seasonNumber:1,episodes:10,totalEpisodes:30},false,true),
          p.confirmedPartial({seasonNumber:1,episodes:0,totalEpisodes:30},false,true),
          p.confirmedPartial({seasonNumber:1,episodes:30,totalEpisodes:30},false,true),
          p.confirmedPartial({seasonNumber:1,episodes:10,totalEpisodes:0},false,true),
          p.confirmedPartial({seasonNumber:1,episodes:10,totalEpisodes:30},false,false),
          p.confirmedPartial({seasonNumber:0,episodes:10,totalEpisodes:30},false,true),
          p.confirmedPartial({seasonNumber:1,episodes:10,totalEpisodes:30,is4kOverride:true},false,true),
          p.confirmedPartial({seasonNumber:1,episodes4k:10,totalEpisodes:30,is4kOverride:true},true,true)
        ]"""), [True, False, False, False, False, False, False, True])

    def test_unknown_upstream_source_fails_closed(self):
        result = subprocess.run(
            ["node", "-e", f"require({json.dumps(str(PATCH))}).patch('changed upstream');"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires review", result.stderr)


if __name__ == "__main__":
    unittest.main()
