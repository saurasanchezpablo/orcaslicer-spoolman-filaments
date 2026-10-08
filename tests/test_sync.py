"""Sync logic tests. Run: python3 -m unittest discover -s tests"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spoolman_filaments as sf  # noqa: E402


def system(name, vendor, material, compatible=True, **config):
    base = {"filament_type": f'"{material}"', "filament_vendor": f'"{vendor}"', "nozzle_temperature": "220",
            "filament_flow_ratio": "0.98", "filament_max_volumetric_speed": "12",
            "filament_start_gcode": '"; Filament gcode\\n"', "compatible_printers": "",
            "filament_retraction_length": "nil",
            "inherits": "", "filament_settings_id": f'"{name}"'}
    base.update(config)
    return sf.SystemFilament(name, vendor, material, compatible), base


CATALOG_ENTRIES = [
    system("Generic PLA @System", "Generic", "PLA"),
    system("Generic PLA @Elegoo Centauri", "Generic", "PLA", filament_max_volumetric_speed="21"),
    system("Generic PLA Matte @System", "Generic", "PLA"),
    system("Generic PETG @System", "Generic", "PETG", nozzle_temperature="245"),
    system("Generic TPU @System", "Generic", "TPU", filament_max_volumetric_speed="3.2"),
    system("SUNLU PLA Matte @System", "SUNLU", "PLA"),
    system("SUNLU PLA+ @System", "SUNLU", "PLA"),
    system("Elegoo PLA PRO @System", "Elegoo", "PLA"),
    system("Elegoo PLA PRO @Elegoo Giga", "Elegoo", "PLA", compatible=False),
]


def catalog():
    configs = {f.name: c for f, c in CATALOG_ENTRIES}
    return sf.Catalog([f for f, _ in CATALOG_ENTRIES], lambda name: dict(configs[name]))


def filament(fid, vendor, name, material, **extra):
    return {"id": fid, "name": name, "material": material, "vendor": {"name": vendor},
            "color_hex": "FFFFFF", "density": 1.24, "diameter": 1.75, "price": 20.0, "weight": 1000.0,
            "settings_extruder_temp": 215, "settings_bed_temp": 60,
            "extra": {f"orca_{k}": v for k, v in extra.items()}}


class FakeSpoolman(sf.Spoolman):
    """In-memory Spoolman speaking the same API subset."""

    def __init__(self, filaments, fields=None):
        super().__init__("http://spoolman.test")
        self.spools = [{"id": i + 1, "archived": False, "filament": f} for i, f in enumerate(filaments)]
        self.fields = {k: {"key": k, "field_type": "float"} for k in (fields or [])}
        self.patches = []

    def request(self, method, path, body=None):
        if method == "GET" and path.startswith("/api/v1/spool"):
            return copy.deepcopy([s for s in self.spools if not s["archived"]])
        if method == "GET" and path == "/api/v1/field/filament":
            return list(copy.deepcopy(self.fields).values())
        if method == "POST" and path.startswith("/api/v1/field/filament/"):
            key = path.rsplit("/", 1)[1]
            self.fields[key] = {"key": key, **body}
            return self.fields[key]
        if method == "PATCH" and path.startswith("/api/v1/filament/"):
            fid = int(path.rsplit("/", 1)[1])
            self.patches.append((fid, body["extra"]))
            for spool in self.spools:
                if spool["filament"]["id"] == fid:
                    spool["filament"].setdefault("extra", {}).update(body["extra"])
            return {}
        raise AssertionError(f"unexpected {method} {path}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.profiles = root / "user" / "u1" / "filament"
        self.state = root / "plugin_data" / "state.json"
        self.spoolman = FakeSpoolman([
            filament(1, "SUNLU", "SUNLU pla matte white", "PLA", filament_flow_ratio='"1.0136"'),
            filament(2, "Geeetech", "Geeetech PLA Marble", "PLA"),
            filament(3, "Keleidi", "Keleidi TPU 95A white", "Flexible (TPU)"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def syncer(self):
        return sf.Syncer(self.spoolman, catalog(), self.profiles, self.state)

    def profile(self, name):
        return json.loads((self.profiles / f"{name}.json").read_text())


class ParentChoice(unittest.TestCase):
    def parent(self, vendor, name, material):
        return sf.choose_parent(catalog(), {"name": name, "vendor": {"name": vendor}}, material)

    def test_vendor_and_modifier(self):
        self.assertEqual(self.parent("SUNLU", "SUNLU pla matte white", "PLA"), "SUNLU PLA Matte @System")

    def test_compatible_printer_variant_preferred(self):
        self.assertEqual(self.parent("Elegoo", "ELEGOO PLA pro silver", "PLA"), "Elegoo PLA PRO @System")

    def test_generic_fallback_prefers_printer_tuned(self):
        self.assertEqual(self.parent("Geeetech", "Geeetech PLA Marble", "PLA"), "Generic PLA @Elegoo Centauri")

    def test_material_in_parentheses(self):
        self.assertEqual(sf.normalize_material("Flexible (TPU)"), "TPU")
        self.assertEqual(self.parent("Keleidi", "Keleidi TPU 95A", "TPU"), "Generic TPU @System")

    def test_unknown_material(self):
        self.assertIsNone(self.parent("X", "X Unobtainium", "UNOBTAINIUM"))


class Generate(Base):
    def test_profile_contents(self):
        report = self.syncer().sync()
        self.assertEqual(len(report.created), 3)
        p = self.profile("SUNLU pla matte white [Spoolman 1]")
        self.assertEqual(p["filament_id"], "SPOOLMAN_1")
        self.assertEqual(p["inherits"], "")
        self.assertEqual(p["compatible_printers"], [])
        self.assertEqual(p["filament_flow_ratio"], ["1.0136"])        # orca_ extra wins
        self.assertEqual(p["nozzle_temperature"], ["215"])            # Spoolman field beats parent
        self.assertEqual(p["hot_plate_temp"], ["60"])
        self.assertEqual(p["filament_cost"], ["20"])
        self.assertEqual(p["filament_start_gcode"], '"; Filament gcode\\n"')  # parent value, serialized
        tpu = self.profile("Keleidi TPU 95A white [Spoolman 3]")
        self.assertEqual(tpu["filament_type"], ["TPU"])
        self.assertEqual(tpu["filament_max_volumetric_speed"], "3.2")

    def test_name_never_contains_at(self):
        self.assertEqual(sf.profile_name({"id": 9, "name": "PLA @ home", "vendor": {"name": "Acme"}}),
                         "Acme PLA home [Spoolman 9]")

    def test_new_profiles_have_no_info(self):
        self.syncer().sync()
        self.assertEqual(list(self.profiles.glob("*.info")), [])

    def test_second_sync_changes_nothing(self):
        self.syncer().sync()
        report = self.syncer().sync()
        self.assertFalse(report.restart_needed)
        self.assertEqual(report.pushed, [])

    def test_creates_missing_spoolman_fields_once(self):
        report = self.syncer().sync()
        self.assertIn("orca_filament_flow_ratio", report.fields_created)
        self.assertEqual(self.syncer().sync().fields_created, [])


class OrcaEdits(Base):
    def orca_saves(self, name, **changes):
        """What OrcaSlicer does on save: every value as a list, plus defaults it adds."""
        path = self.profiles / f"{name}.json"
        p = json.loads(path.read_text())
        for key, value in list(p.items()):
            if isinstance(value, str) and key not in sf.OWNED_KEYS:
                p[key] = [sf.canonical(value) if not isinstance(sf.canonical(value), (int, float))
                          else value]
        p.update({k: [v] for k, v in changes.items()})
        p["slow_down_min_speed"] = ["10"]
        path.write_text(json.dumps(p))

    def test_resave_without_edits_is_not_an_edit(self):
        self.syncer().sync()
        self.orca_saves("Geeetech PLA Marble [Spoolman 2]")
        self.assertEqual(self.syncer().push_saved("Geeetech PLA Marble [Spoolman 2]").pushed, [])
        self.assertEqual(self.spoolman.patches, [])

    def test_saved_edit_goes_to_spoolman(self):
        self.syncer().sync()
        self.orca_saves("Geeetech PLA Marble [Spoolman 2]", filament_flow_ratio="0.95", nozzle_temperature="225")
        report = self.syncer().push_saved("Geeetech PLA Marble [Spoolman 2]")
        self.assertEqual(len(report.pushed), 1)
        fid, extra = self.spoolman.patches[0]
        self.assertEqual(fid, 2)
        self.assertEqual(extra, {"orca_filament_flow_ratio": "0.95", "orca_nozzle_temperature": "225"})
        # Already in Spoolman: the next sync neither pushes again nor reverts the profile.
        report = self.syncer().sync()
        self.assertEqual(report.pushed, [])
        self.assertEqual(self.profile("Geeetech PLA Marble [Spoolman 2]")["filament_flow_ratio"], ["0.95"])

    def test_edit_made_while_plugin_was_off_is_pushed_on_sync(self):
        self.syncer().sync()
        self.orca_saves("Geeetech PLA Marble [Spoolman 2]", filament_retraction_length="0.6")
        report = self.syncer().sync()
        self.assertEqual(self.spoolman.patches, [(2, {"orca_filament_retraction_length": "0.6"})])
        self.assertEqual(report.pushed[0], "Geeetech PLA Marble [Spoolman 2]: filament_retraction_length")

    def test_spoolman_change_reaches_profile(self):
        self.syncer().sync()
        self.spoolman.spools[1]["filament"]["extra"]["orca_filament_flow_ratio"] = '"0.9"'
        report = self.syncer().sync()
        self.assertEqual(report.updated, ["Geeetech PLA Marble [Spoolman 2]"])
        self.assertEqual(self.profile("Geeetech PLA Marble [Spoolman 2]")["filament_flow_ratio"], ["0.9"])


class OrcaCloud(Base):
    NAME = "Geeetech PLA Marble [Spoolman 2]"

    def uploaded(self):
        (self.profiles / f"{self.NAME}.info").write_text(
            "sync_info = \nuser_id = u1\nsetting_id = PCLOUD\nbase_id = \nupdated_time = 100\n")

    def test_update_of_uploaded_profile_is_marked_for_upload(self):
        self.syncer().sync()
        self.uploaded()
        self.spoolman.spools[1]["filament"]["density"] = 1.3
        self.syncer().sync()
        info = sf.read_info(self.profiles / f"{self.NAME}.info")
        self.assertEqual(info["sync_info"], "update")
        self.assertEqual(info["setting_id"], "PCLOUD")
        self.assertGreater(int(info["updated_time"]), 100)

    def test_removed_uploaded_profile_is_deleted_from_cloud(self):
        self.syncer().sync()
        self.uploaded()
        self.spoolman.spools[1]["archived"] = True
        report = self.syncer().sync()
        self.assertEqual(report.removed, [self.NAME])
        self.assertFalse((self.profiles / f"{self.NAME}.json").exists())
        self.assertEqual(sf.read_info(self.profiles / f"{self.NAME}.info")["sync_info"], "delete")

    def test_removed_profile_written_back_by_orca_is_removed_again(self):
        self.syncer().sync()
        self.spoolman.spools[1]["archived"] = True
        self.syncer().sync()
        (self.profiles / f"{self.NAME}.json").write_text('{"filament_id": "SPOOLMAN_2"}')
        self.assertEqual(self.syncer().sync().removed, [self.NAME])
        self.assertEqual(self.syncer().sync().removed, [])

    def test_renamed_filament_keeps_cloud_identity(self):
        self.syncer().sync()
        self.uploaded()
        self.spoolman.spools[1]["filament"]["name"] = "Geeetech PLA Marble Grey"
        self.syncer().sync()
        new = self.profiles / "Geeetech PLA Marble Grey [Spoolman 2]"
        self.assertFalse((self.profiles / f"{self.NAME}.json").exists())
        self.assertEqual(sf.read_info(new.with_suffix(".info"))["setting_id"], "PCLOUD")


class Security(Base):
    def test_only_known_settings_are_taken_from_spoolman(self):
        self.spoolman.spools[1]["filament"]["extra"].update({
            "orca_filament_start_gcode": '"M104 S300"', "orca_filament_id": '"X"', "orca_inherits": '"Y"'})
        self.syncer().sync()
        p = self.profile("Geeetech PLA Marble [Spoolman 2]")
        self.assertEqual(p["filament_start_gcode"], '"; Filament gcode\\n"')
        self.assertEqual(p["filament_id"], "SPOOLMAN_2")
        self.assertEqual(p["inherits"], "")

    def test_hostile_names_stay_in_the_profile_folder(self):
        for hostile in ("..\\..\\evil", "../../evil", "a:b*c?<d>|e", "x\x00y\nz", "...", ""):
            name = sf.profile_name({"id": 5, "name": hostile, "vendor": {"name": ""}})
            self.assertNotRegex(name, r'[\\/:*?"<>|\x00-\x1f]')
            path = sf.profile_path(self.profiles, name)
            self.assertEqual(path.parent, self.profiles)
        with self.assertRaises(sf.SpoolmanError):
            sf.profile_path(self.profiles, "../escape")

    def test_users_own_profile_with_the_same_name_is_left_alone(self):
        self.profiles.mkdir(parents=True)
        mine = self.profiles / "Geeetech PLA Marble [Spoolman 2].json"
        mine.write_text('{"name": "mine", "inherits": "Generic PLA @System"}')
        report = self.syncer().sync()
        self.assertEqual(json.loads(mine.read_text())["name"], "mine")
        self.assertTrue(any("not a Spoolman Filaments profile" in e for e in report.errors))

    def test_credentials_are_sent_but_never_shown(self):
        client = sf.Spoolman("https://me:s3cret@spoolman.test:7912")
        self.assertEqual(client.display_url, "https://spoolman.test:7912")
        with self.assertRaises(sf.SpoolmanError) as ctx:
            sf.Spoolman("http://me:s3cret@127.0.0.1:9", timeout=1).request("GET", "/api/v1/info")
        self.assertNotIn("s3cret", str(ctx.exception))


class Values(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(sf.canonical(["250"]), sf.canonical("250"))
        self.assertEqual(sf.canonical('"; gcode\\n"'), sf.canonical(["; gcode\n"]))
        self.assertEqual(sf.canonical("0.950"), sf.canonical(["0.95"]))

    def test_encode_extra(self):
        self.assertEqual(sf.encode_extra({"field_type": "integer"}, ["225"]), "225")
        self.assertEqual(sf.encode_extra({"field_type": "float"}, ["0.95"]), "0.95")
        self.assertEqual(sf.encode_extra({"field_type": "text"}, ["hello"]), '"hello"')
        self.assertEqual(sf.encode_extra({"field_type": "boolean"}, ["1"]), "true")
        self.assertIsNone(sf.encode_extra({"field_type": "float"}, ["nil"]))

    def test_first_serialized(self):
        self.assertEqual(sf.first_serialized('"PLA";"PETG"'), "PLA")
        self.assertEqual(sf.first_serialized("PLA"), "PLA")
        self.assertEqual(sf.first_serialized('"Generic"'), "Generic")


if __name__ == "__main__":
    unittest.main()
