import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import mod_doctor as doctor


class LegacyRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_game = doctor.GAME
        self.old_gamev = doctor.GAMEV
        doctor.GAME = self.root / "Timberborn_Data" / "Managed"
        doctor.GAME.mkdir(parents=True)
        doctor.GAMEV = (1, 0, 13, 1)
        modding = doctor.GAME.parent / "StreamingAssets" / "Modding"
        modding.mkdir(parents=True)
        with zipfile.ZipFile(modding / "Blueprints.zip", "w") as archive:
            archive.writestr(
                "Buildings/Food/Bakery/Bakery.Folktails.blueprint.json", "{}"
            )

    def tearDown(self):
        doctor.GAME = self.old_game
        doctor.GAMEV = self.old_gamev
        self.temp.cleanup()

    def make_mod(self, name="legacy", unique_id="Legacy.Mod"):
        folder = self.root / name
        specs = folder / "Specifications"
        specs.mkdir(parents=True)
        (folder / "mod.json").write_text(
            json.dumps(
                {
                    "Name": name,
                    "Version": "1.0.0",
                    "UniqueId": unique_id,
                    "MinimumApiVersion": "0.6.0",
                }
            ),
            encoding="utf-8",
        )
        mod = {
            "folder": folder,
            "name": name,
            "is_loaded": False,
            "steam": False,
            "modio": False,
        }
        return folder, specs, mod

    def test_read_json_accepts_jsonc_comments_and_trailing_comma(self):
        path = self.root / "mod.json"
        path.write_text(
            '{\n  "Name": "http://example.invalid", // comment\n  "Version": "1",\n}\n',
            encoding="utf-8",
        )
        self.assertEqual(doctor.read_json(path)["Name"], "http://example.invalid")

    def test_data_only_package_becomes_native_blueprints(self):
        folder, specs, mod = self.make_mod("berries", "BerriesAreNutritious")
        (specs / "NeedSpecification.Beaver.Berries.original.json").write_text(
            json.dumps(
                {
                    "Id": "Berries",
                    "NeedGroupId": "Nutrition",
                    "CharacterType": "Beaver",
                    "FavorableWellbeing": 1,
                }
            ),
            encoding="utf-8",
        )
        (specs / "GoodSpecification.Berries.json").write_text(
            json.dumps(
                {
                    "Id": "Berries",
                    "VisibleContainer": {"Value": "Box"},
                    "ContainerColor": "#336699",
                    "ConsumptionEffects": [{"NeedId": "Berries", "Points": 0.2}],
                }
            ),
            encoding="utf-8",
        )
        (specs / "FactionSpecification.Folktails.json").write_text(
            json.dumps({"Needs": ["Berries"]}), encoding="utf-8"
        )

        profile = doctor._legacy_profile(mod)
        self.assertTrue(profile["repairable"])
        destination = self.root / "converted"
        version_root = doctor._convert_legacy_package(profile, destination)

        manifest = json.loads((version_root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["MinimumGameVersion"], "1.0.13.1")
        self.assertTrue((version_root / "Goods/Good.Berries.blueprint.json").exists())
        self.assertTrue((version_root / "Needs/Need.Beaver.Berries.blueprint.json").exists())
        collection = json.loads(
            (version_root / "NeedCollection/NeedCollection.Folktails.blueprint.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(collection["NeedCollectionSpec"]["Needs#append"], ["Berries"])

    def test_compiled_timberapi_mod_is_never_manifest_wrapped(self):
        folder, _, mod = self.make_mod("compiled")
        metadata = json.loads((folder / "mod.json").read_text(encoding="utf-8"))
        metadata["EntryDll"] = "Legacy.dll"
        (folder / "mod.json").write_text(json.dumps(metadata), encoding="utf-8")
        (folder / "Legacy.dll").write_bytes(b"not a managed assembly")

        profile = doctor._legacy_profile(mod)
        self.assertFalse(profile["repairable"])
        self.assertIn("source rebuild", profile["reason"])

    def test_duplicate_legacy_definition_has_one_owner(self):
        _, first_specs, first_mod = self.make_mod(
            "berries", "BerriesAreNutritious"
        )
        _, second_specs, second_mod = self.make_mod("snacks", "MoreSnacks")
        definition = json.dumps(
            {"Id": "Berries", "NeedGroupId": "Nutrition", "CharacterType": "Beaver"}
        )
        filename = "NeedSpecification.Beaver.Berries.original.json"
        (first_specs / filename).write_text(definition, encoding="utf-8")
        (second_specs / filename).write_text(definition, encoding="utf-8")

        profiles = doctor._coordinate_legacy_profiles(
            [doctor._legacy_profile(first_mod), doctor._legacy_profile(second_mod)]
        )

        self.assertNotIn(filename, profiles[0]["omit_specs"])
        self.assertIn(filename, profiles[1]["omit_specs"])
        self.assertIn("Berries", profiles[1]["omit_needs"])


if __name__ == "__main__":
    unittest.main()
