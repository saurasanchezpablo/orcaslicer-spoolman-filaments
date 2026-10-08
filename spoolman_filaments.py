# /// script
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "Spoolman Filaments"
# description = "One filament profile per Spoolman filament, kept in sync with Spoolman both ways"
# author = "Pablo Saura"
# version = "0.1.0"
# ///

"""Spoolman Filaments: one OrcaSlicer filament profile per Spoolman filament.

Every Spoolman filament with an active spool gets a standalone filament profile
named "<filament> [Spoolman <id>]" with filament_id "SPOOLMAN_<id>". Its
settings come from the best-matching OrcaSlicer system profile, then
Spoolman's own fields, then Spoolman's "orca_<setting>" extra fields. Saving
one of these profiles in OrcaSlicer writes the edited settings back to those
extra fields, so Spoolman stays the source of truth.

OrcaSlicer builds that select a lane's profile from its Spoolman spool (e.g.
AFC lanes reported through Moonraker) pick these profiles by filament_id.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any, Callable

try:
    import orca
except ImportError:  # unit tests and tooling import the sync logic without OrcaSlicer
    orca = None

PLUGIN_NAME = "Spoolman Filaments"
FILAMENT_ID_PREFIX = "SPOOLMAN_"
EXTRA_PREFIX = "orca_"
DEFAULT_SETTINGS = {"spoolman_url": "", "sync_on_startup": True}

# Settings that get a Spoolman extra field ("orca_<key>") so they sync both ways.
# Keys match PipSpool's, so Spoolman servers it already set up keep working.
SYNCED_SETTINGS: dict[str, tuple[str, str, str | None]] = {
    "filament_flow_ratio": ("Flow ratio", "float", None),
    "filament_max_volumetric_speed": ("Max volumetric speed", "float", "mm³/s"),
    "enable_pressure_advance": ("Enable pressure advance", "boolean", None),
    "pressure_advance": ("Pressure advance", "float", None),
    "nozzle_temperature": ("Nozzle temperature", "integer", "°C"),
    "nozzle_temperature_initial_layer": ("Nozzle temperature, first layer", "integer", "°C"),
    "hot_plate_temp": ("Smooth PEI plate temperature", "integer", "°C"),
    "hot_plate_temp_initial_layer": ("Smooth PEI plate temperature, first layer", "integer", "°C"),
    "textured_plate_temp": ("Textured PEI plate temperature", "integer", "°C"),
    "textured_plate_temp_initial_layer": ("Textured PEI plate temperature, first layer", "integer", "°C"),
    "cool_plate_temp": ("Cool plate temperature", "integer", "°C"),
    "cool_plate_temp_initial_layer": ("Cool plate temperature, first layer", "integer", "°C"),
    "textured_cool_plate_temp": ("Textured cool plate temperature", "integer", "°C"),
    "textured_cool_plate_temp_initial_layer": ("Textured cool plate temperature, first layer", "integer", "°C"),
    "eng_plate_temp": ("Engineering plate temperature", "integer", "°C"),
    "eng_plate_temp_initial_layer": ("Engineering plate temperature, first layer", "integer", "°C"),
    "supertack_plate_temp": ("SuperTack plate temperature", "integer", "°C"),
    "supertack_plate_temp_initial_layer": ("SuperTack plate temperature, first layer", "integer", "°C"),
    "filament_retraction_length": ("Retraction length", "float", "mm"),
    "filament_retraction_speed": ("Retraction speed", "float", "mm/s"),
    "fan_min_speed": ("Fan min speed", "integer", "%"),
    "fan_max_speed": ("Fan max speed", "integer", "%"),
    "filament_density": ("Density", "float", "g/cm³"),
    "filament_diameter": ("Diameter", "float", "mm"),
    "filament_cost": ("Cost", "float", "per kg"),
    "filament_notes": ("Notes", "text", None),
}

PLATE_TEMP_KEYS = [k for k in SYNCED_SETTINGS if k.endswith("plate_temp") or k.endswith("plate_temp_initial_layer")]

# Keys copied from a system profile that describe that profile rather than the filament.
PARENT_SKIP_KEYS = {
    "inherits", "compatible_printers", "compatible_printers_condition", "compatible_prints",
    "compatible_prints_condition", "filament_settings_id", "filament_id", "setting_id", "name", "from",
    "instantiation", "description", "version", "renamed_from",
}
# Keys the plugin always owns; never treated as edits made in OrcaSlicer.
OWNED_KEYS = {"name", "inherits", "from", "version", "filament_id", "filament_settings_id",
              "compatible_printers", "compatible_printers_condition"}


def log(message: str) -> None:
    print(f"[{PLUGIN_NAME}] {message}", flush=True)


# --- Spoolman ------------------------------------------------------------------------------------

class SpoolmanError(RuntimeError):
    pass


class Spoolman:
    def __init__(self, base_url: str, timeout: float = 10):
        self.base_url = normalize_url(base_url)
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> Any:
        parsed = urllib.parse.urlsplit(self.base_url + path)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SpoolmanError(f"Not an http(s) URL: {self.base_url}")
        conn_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_type(parsed.hostname, parsed.port, timeout=self.timeout)
        target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, target, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        except OSError as exc:
            raise SpoolmanError(f"Cannot reach Spoolman at {self.base_url}: {exc}") from exc
        finally:
            conn.close()
        if resp.status >= 400:
            raise SpoolmanError(f"Spoolman {method} {path}: HTTP {resp.status} {payload[:200]!r}")
        return json.loads(payload) if payload else None

    def active_filaments(self) -> dict[int, dict]:
        """Filaments with at least one non-archived spool, keyed by filament id."""
        filaments: dict[int, dict] = {}
        for spool in self.request("GET", "/api/v1/spool?allow_archived=false") or []:
            filament = spool.get("filament") or {}
            if not spool.get("archived") and "id" in filament:
                filaments.setdefault(int(filament["id"]), filament)
        return filaments

    def filament_fields(self) -> dict[str, dict]:
        return {f["key"]: f for f in self.request("GET", "/api/v1/field/filament") or []}

    def ensure_fields(self, existing: dict[str, dict]) -> list[str]:
        """Create the missing orca_<setting> extra fields. Existing fields are left alone."""
        created = []
        order = 100 + len(existing)
        for key, (label, kind, unit) in SYNCED_SETTINGS.items():
            field_key = EXTRA_PREFIX + key
            if field_key in existing:
                continue
            definition = {"name": f"Orca: {label}", "field_type": kind, "order": order}
            if unit and kind in ("integer", "float"):
                definition["unit"] = unit
            self.request("POST", f"/api/v1/field/filament/{field_key}", definition)
            created.append(field_key)
            order += 1
        return created

    def update_filament_extra(self, filament_id: int, extra: dict[str, str]) -> None:
        self.request("PATCH", f"/api/v1/filament/{int(filament_id)}", {"extra": extra})


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def decode_extra(value: Any) -> Any:
    """Spoolman extra values are JSON-encoded, sometimes more than once."""
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return value


def encode_extra(field_def: dict, value: Any) -> str | None:
    """Encode an OrcaSlicer profile value for a Spoolman extra field."""
    if isinstance(value, list) and field_def.get("field_type") == "text" and len(value) != 1:
        return json.dumps(json.dumps(value, separators=(",", ":")))
    value = canonical(value)
    if value is None or value == "" or value == "nil":
        return None
    kind = field_def.get("field_type")
    try:
        if kind == "integer":
            value = int(float(value))
        elif kind == "float":
            value = float(value)
        elif kind == "boolean":
            value = str(value).strip().lower() in ("1", "true", "yes", "on", "1.0")
        elif kind == "choice":
            if str(value) not in (field_def.get("choices") or []):
                return None
            value = str(value)
        else:
            value = str(value)
    except (TypeError, ValueError):
        return None
    return json.dumps(value)


def normalize_material(material: str) -> str:
    """'Flexible (TPU)' -> 'TPU': Spoolman names often carry the base type in parentheses."""
    match = re.search(r"\(([^)]+)\)", material or "")
    return (match.group(1) if match else material or "").strip()


# --- Values --------------------------------------------------------------------------------------

def fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def first_serialized(value: str | None) -> str:
    """First element of an OrcaSlicer serialized value ('"PLA";"PETG"', 'PLA', '1.2,1.3')."""
    if not value:
        return ""
    value = value.strip()
    if value.startswith('"'):
        match = re.match(r'"((?:[^"\\]|\\.)*)"', value)
        if match:
            return match.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return re.split(r"[;,]", value, maxsplit=1)[0].strip()


def canonical(value: Any) -> Any:
    """Compare OrcaSlicer values independent of their JSON form (["250"], "250", '"text"')."""
    if isinstance(value, list):
        if len(value) != 1:
            return [canonical(v) for v in value]
        value = value[0]
    if isinstance(value, str):
        text = value
        quoted = value.strip()
        if len(quoted) >= 2 and quoted[0] == quoted[-1] == '"':
            try:
                text = json.loads(quoted)
            except ValueError:
                text = quoted[1:-1]
        try:
            number = float(text.strip())
            return int(number) if number.is_integer() else number
        except (TypeError, ValueError):
            return text
    return value


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+]+", (text or "").lower())


# --- Profiles ------------------------------------------------------------------------------------

@dataclass
class SystemFilament:
    name: str
    vendor: str
    material: str
    compatible: bool


class Catalog:
    """System filament profiles to build from. The plugin reads them from OrcaSlicer."""

    def __init__(self, filaments: list[SystemFilament], config_of: Callable[[str], dict[str, str]]):
        self.filaments = filaments
        self.config_of = config_of


def host_catalog() -> Catalog:
    collection = orca.host.preset_bundle().filaments
    filaments = []
    for name in collection.preset_names():
        preset = collection.find_preset(name)
        if preset is None or not preset.is_system or not preset.is_visible:
            continue
        filaments.append(SystemFilament(
            name=name,
            vendor=first_serialized(preset.config_value("filament_vendor")),
            material=first_serialized(preset.config_value("filament_type")),
            compatible=bool(preset.is_compatible),
        ))

    def config_of(name: str) -> dict[str, str]:
        preset = collection.find_preset(name)
        return {key: preset.config_value(key) for key in preset.config_keys()}

    return Catalog(filaments, config_of)


def choose_parent(catalog: Catalog, filament: dict, material: str) -> str | None:
    """The system profile closest to a Spoolman filament: same material, same vendor if possible,
    sharing the name's modifiers ("Matte", "PRO", "Silk"...), compatible with the current printer."""
    vendor = ((filament.get("vendor") or {}).get("name") or "").lower()
    name_words = set(words(filament.get("name", ""))) - set(words(vendor)) - set(words(material))
    best, best_key = None, None
    for candidate in catalog.filaments:
        if candidate.material.upper() != material.upper():
            continue
        candidate_vendor = candidate.vendor.lower()
        if candidate_vendor == vendor and vendor:
            score = 10
        elif candidate_vendor == "generic":
            score = 0
        else:
            continue
        base = candidate.name.split(" @")[0]
        modifiers = set(words(base)) - set(words(candidate_vendor)) - set(words(material))
        score += 3 * len(modifiers & name_words) - 3 * len(modifiers - name_words)
        score += 2 if candidate.compatible else -5
        # On a tie the printer-specific variant ("@Elegoo Centauri") beats the portable "@System" one.
        key = (score, not candidate.name.endswith("@System"), candidate.name)
        if best_key is None or key > best_key:
            best, best_key = candidate.name, key
    return best


def profile_name(filament: dict) -> str:
    vendor = (filament.get("vendor") or {}).get("name") or ""
    name = filament.get("name") or f"Filament {filament['id']}"
    if vendor and not name.lower().startswith(vendor.lower()):
        name = f"{vendor} {name}"
    # '@' would make OrcaSlicer restrict the profile to the printer named after it.
    name = name.replace("@", " ").replace("/", "-")
    return f"{' '.join(name.split())} [Spoolman {filament['id']}]"


def generate(catalog: Catalog, filament: dict, version: str) -> tuple[dict, str]:
    material = normalize_material(filament.get("material") or "")
    parent = choose_parent(catalog, filament, material)
    if parent is None:
        raise SpoolmanError(f"No system profile for material {material!r} ({filament.get('name')})")
    profile: dict[str, Any] = {k: v for k, v in catalog.config_of(parent).items()
                               if k not in PARENT_SKIP_KEYS and v is not None}

    def put(key: str, value: Any) -> None:
        if value is not None and value != "":
            profile[key] = [fmt(value)]

    put("filament_type", material)
    put("filament_vendor", (filament.get("vendor") or {}).get("name"))
    if filament.get("color_hex"):
        put("default_filament_colour", "#" + filament["color_hex"][:6].upper())
    put("filament_density", filament.get("density"))
    put("filament_diameter", filament.get("diameter"))
    if filament.get("price") and filament.get("weight"):
        put("filament_cost", round(filament["price"] / filament["weight"] * 1000, 2))
    if filament.get("settings_extruder_temp"):
        put("nozzle_temperature", filament["settings_extruder_temp"])
        put("nozzle_temperature_initial_layer", filament["settings_extruder_temp"])
    if filament.get("settings_bed_temp"):
        for key in PLATE_TEMP_KEYS:
            put(key, filament["settings_bed_temp"])

    for key, raw in (filament.get("extra") or {}).items():
        if not key.startswith(EXTRA_PREFIX):
            continue
        value = decode_extra(raw)
        if isinstance(value, list):
            if any(str(v).strip('"') for v in value):
                profile[key[len(EXTRA_PREFIX):]] = [str(v) for v in value]
            continue
        if isinstance(value, str) and value.strip('"') == "":
            continue
        put(key[len(EXTRA_PREFIX):], value)

    name = profile_name(filament)
    profile.update({
        "name": name,
        "inherits": "",
        "from": "User",
        "version": version,
        "filament_id": f"{FILAMENT_ID_PREFIX}{filament['id']}",
        "filament_settings_id": [name],
        "compatible_printers": [],
        "compatible_printers_condition": "",
    })
    return profile, parent


def find_edits(existing: dict | None, last_generated: dict | None) -> list[str]:
    """Generated keys whose value was changed in OrcaSlicer since the plugin last wrote them."""
    if not existing or not last_generated:
        return []
    return [key for key, value in existing.items()
            if key not in OWNED_KEYS and key in last_generated
            and canonical(value) != canonical(last_generated[key])]


def merge(desired: dict, existing: dict | None, edits: list[str]) -> dict:
    """Desired profile plus the OrcaSlicer edits and the keys OrcaSlicer added when saving."""
    merged = dict(desired)
    for key, value in (existing or {}).items():
        if key in OWNED_KEYS:
            continue
        if key in edits or key not in merged:
            merged[key] = value
    return merged


# --- Files ---------------------------------------------------------------------------------------

def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_info(path: Path) -> dict[str, str]:
    info = {}
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key.strip()] = value.strip()
    except OSError:
        pass
    return info


def write_info(path: Path, info: dict[str, str]) -> None:
    keys = ["sync_info", "user_id", "setting_id", "base_id", "updated_time"]
    path.write_text("".join(f"{k} = {info.get(k, '')}\n" for k in keys))


def remove_profile(path: Path) -> None:
    """Delete a profile. One already in the Orca cloud keeps its .info marked for deletion: OrcaSlicer
    then deletes the cloud copy on its next start, instead of restoring it from the cloud."""
    path.unlink(missing_ok=True)
    info_path = path.with_suffix(".info")
    info = read_info(info_path)
    if info.get("setting_id"):
        write_info(info_path, {**info, "sync_info": "delete"})
    else:
        info_path.unlink(missing_ok=True)


def write_profile(path: Path, profile: dict, old_path: Path) -> None:
    old_info = read_info(old_path.with_suffix(".info"))
    if old_path != path:
        old_path.unlink(missing_ok=True)
        old_path.with_suffix(".info").unlink(missing_ok=True)
    write_json(path, profile)
    if old_info.get("setting_id"):
        # Already in the Orca cloud: mark the newer local version for upload so the cloud copy
        # does not overwrite it.
        write_info(path.with_suffix(".info"),
                   {**old_info, "sync_info": "update", "updated_time": str(int(time.time()))})
    else:
        # No .info: OrcaSlicer uploads it as a new profile. A setting_id without sync_info would make
        # it look uploaded already, and the cloud sync would delete it as removed remotely.
        path.with_suffix(".info").unlink(missing_ok=True)


# --- Sync ----------------------------------------------------------------------------------------

@dataclass
class Report:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    fields_created: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def restart_needed(self) -> bool:
        return bool(self.created or self.updated or self.removed)

    def summary(self) -> str:
        lines = []
        for label, items in (("Created", self.created), ("Updated", self.updated), ("Removed", self.removed),
                             ("Saved to Spoolman", self.pushed), ("New Spoolman fields", self.fields_created),
                             ("Errors", self.errors)):
            if items:
                lines.append(f"{label}:\n  " + "\n  ".join(items))
        if not lines:
            return "All filament profiles are up to date."
        if self.restart_needed:
            lines.append("Restart OrcaSlicer to load the changed profiles.")
        return "\n\n".join(lines)


class Syncer:
    """Spoolman <-> profile files. Independent of OrcaSlicer so it can be tested on its own."""

    def __init__(self, spoolman: Spoolman, catalog: Catalog, profile_dir: Path, state_path: Path,
                 version: str = "2.5.0.0"):
        self.spoolman = spoolman
        self.catalog = catalog
        self.profile_dir = profile_dir
        self.state_path = state_path
        self.version = version

    def load_state(self) -> dict:
        return read_json(self.state_path) or {}

    def save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.state_path, state)

    def push_edits(self, fid: int, existing: dict, edits: list[str], fields: dict[str, dict],
                   report: Report) -> dict[str, str]:
        extra = {}
        for key in edits:
            field_def = fields.get(EXTRA_PREFIX + key)
            encoded = encode_extra(field_def, existing[key]) if field_def else None
            if encoded is not None:
                extra[EXTRA_PREFIX + key] = encoded
        if extra:
            self.spoolman.update_filament_extra(fid, extra)
            report.pushed.append(f"{existing.get('name', fid)}: "
                                 + ", ".join(k[len(EXTRA_PREFIX):] for k in extra))
        return extra

    def sync(self) -> Report:
        report = Report()
        state = self.load_state()
        filaments = self.spoolman.active_filaments()
        fields = self.spoolman.filament_fields()
        report.fields_created = self.spoolman.ensure_fields(fields)
        if report.fields_created:
            fields = self.spoolman.filament_fields()
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        new_state: dict[str, dict] = {}
        for fid, filament in sorted(filaments.items()):
            name = profile_name(filament)
            path = self.profile_dir / f"{name}.json"
            previous = state.get(str(fid), {})
            old_path = self.profile_dir / f"{previous['name']}.json" if previous.get("name") else path
            existing = read_json(old_path)
            try:
                desired, parent = generate(self.catalog, filament, self.version)
                edits = find_edits(existing, previous.get("generated"))
                pushed = self.push_edits(fid, existing, edits, fields, report) if edits else {}
                if pushed:
                    filament = dict(filament, extra={**(filament.get("extra") or {}), **pushed})
                    desired, parent = generate(self.catalog, filament, self.version)
                profile = merge(desired, existing, edits)
            except SpoolmanError as exc:
                report.errors.append(str(exc))
                if previous:
                    new_state[str(fid)] = previous
                continue
            new_state[str(fid)] = {"name": name, "parent": parent, "generated": desired}
            if existing == profile and old_path == path:
                continue
            write_profile(path, profile, old_path)
            (report.updated if existing else report.created).append(name)

        for fid, previous in state.items():
            if fid in new_state:
                continue
            path = self.profile_dir / f"{previous['name']}.json"
            if path.exists():
                remove_profile(path)
                report.removed.append(previous["name"])
                # Keep watching it: OrcaSlicer may write it back once from memory before restarting.
                new_state[fid] = {"name": previous["name"], "removed": True}

        self.save_state(new_state)
        return report

    def push_saved(self, preset_name: str) -> Report:
        """Write the settings of a just-saved profile back to Spoolman."""
        report = Report()
        state = self.load_state()
        for fid, entry in state.items():
            if entry.get("name") != preset_name or entry.get("removed"):
                continue
            existing = read_json(self.profile_dir / f"{preset_name}.json")
            edits = find_edits(existing, entry.get("generated"))
            if not edits:
                return report
            pushed = self.push_edits(int(fid), existing, edits, self.spoolman.filament_fields(), report)
            generated = dict(entry["generated"])
            for key in edits:
                if EXTRA_PREFIX + key in pushed:
                    generated[key] = existing[key]
            entry["generated"] = generated
            self.save_state(state)
        return report


# --- OrcaSlicer glue -----------------------------------------------------------------------------

class Runtime:
    """Process-wide plugin state: storage paths, settings, and one sync at a time."""

    lock = threading.Lock()
    storage: Path | None = None

    @classmethod
    def init_storage(cls) -> None:
        if cls.storage is None:
            cls.storage = Path(orca.host.plugin.storage())

    @classmethod
    def settings(cls) -> dict:
        data = read_json(cls.storage / "settings.json") if cls.storage else None
        return {**DEFAULT_SETTINGS, **(data or {})}

    @classmethod
    def save_settings(cls, settings: dict) -> None:
        write_json(cls.storage / "settings.json", {**DEFAULT_SETTINGS, **settings})

    @classmethod
    def profile_dir(cls) -> Path:
        """The user filament profile folder OrcaSlicer reads (per logged-in user, or "default")."""
        bundle = orca.host.preset_bundle()
        for collection, subdir in ((bundle.filaments, None), (bundle.printers, "filament"),
                                   (bundle.prints, "filament")):
            for name in collection.preset_names():
                preset = collection.find_preset(name)
                if preset is not None and preset.is_user() and preset.file:
                    folder = Path(preset.file).parent
                    return folder if subdir is None else folder.parent / subdir
        # storage is <data_dir>/orca_plugins/plugin_data/<plugin>
        return cls.storage.parents[2] / "user" / "default" / "filament"

    @classmethod
    def syncer(cls) -> Syncer:
        url = cls.settings()["spoolman_url"]
        if not url:
            raise SpoolmanError("Set the Spoolman address in Spoolman Filaments: Settings first.")
        return Syncer(Spoolman(url), host_catalog(), cls.profile_dir(), cls.storage / "state.json")


def show(text: str, icon: str = "info") -> None:
    try:
        orca.host.ui.message(text, title=PLUGIN_NAME, icon=icon)
    except Exception as exc:  # never let a message box failure break a sync
        log(f"message failed: {exc}: {text}")


def run_sync(automatic: bool) -> None:
    def work():
        if not Runtime.lock.acquire(blocking=False):
            return
        try:
            report = Runtime.syncer().sync()
            log(report.summary())
            if not automatic or report.restart_needed or report.errors:
                show(report.summary(), icon="warning" if report.errors else "info")
        except Exception as exc:
            log(f"sync failed: {exc}")
            if not automatic or not isinstance(exc, SpoolmanError):
                show(f"Sync failed:\n{exc}", icon="error")
        finally:
            Runtime.lock.release()

    threading.Thread(target=work, daemon=True).start()


def run_push(preset_name: str) -> None:
    with Runtime.lock:
        try:
            report = Runtime.syncer().push_saved(preset_name)
            if report.pushed:
                log("saved to Spoolman: " + "; ".join(report.pushed))
        except Exception as exc:
            log(f"saving {preset_name!r} to Spoolman failed: {exc}")
            show(f"Could not save {preset_name} to Spoolman:\n{exc}\n\n"
                 "It will be retried on the next sync.", icon="warning")


def event_name(event: Any) -> str:
    name = getattr(event, "name", None)
    return str(name) if name else str(event).rsplit(".", 1)[-1]


SETTINGS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
:root { color-scheme: light dark; --bg:#ffffff; --fg:#1f2328; --muted:#59636e; --line:#d0d7de;
  --accent:#009688; --ok:#1a7f37; --bad:#cf222e; }
@media (prefers-color-scheme: dark) { :root { --bg:#2d2d31; --fg:#e6e6e6; --muted:#a0a4ab;
  --line:#4a4d52; --ok:#3fb950; --bad:#f85149; } }
body { margin:0; padding:20px 24px; background:var(--bg); color:var(--fg);
  font:14px/1.45 system-ui, sans-serif; }
h1 { font-size:18px; margin:0 0 4px; } p { color:var(--muted); margin:0 0 16px; }
label { display:block; font-weight:600; margin:14px 0 6px; }
input[type=text] { width:100%; box-sizing:border-box; padding:8px 10px; border:1px solid var(--line);
  border-radius:6px; background:transparent; color:inherit; font:inherit; }
.check { display:flex; gap:8px; align-items:center; font-weight:400; }
.row { display:flex; gap:8px; margin-top:20px; }
button { padding:8px 14px; border-radius:6px; border:1px solid var(--line); background:transparent;
  color:inherit; font:inherit; cursor:pointer; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
#status { margin-top:14px; min-height:1.4em; } .ok { color:var(--ok); } .bad { color:var(--bad); }
</style></head><body>
<h1>Spoolman Filaments</h1>
<p>One filament profile per Spoolman filament, kept in sync both ways.</p>
<label for="url">Spoolman address</label>
<input id="url" type="text" placeholder="http://192.168.1.50:7912" value="__URL__">
<label class="check"><input id="startup" type="checkbox" __STARTUP__> Sync when OrcaSlicer starts</label>
<div class="row">
  <button class="primary" id="save">Save and sync</button>
  <button id="test">Test connection</button>
</div>
<div id="status"></div>
<script>
const $ = (id) => document.getElementById(id);
const send = (action) => window.orca.postMessage({action, url: $("url").value, sync_on_startup: $("startup").checked});
function status(text, ok) { const s = $("status"); s.textContent = text; s.className = ok ? "ok" : "bad"; }
$("test").onclick = () => { status("Connecting…", true); send("test"); };
$("save").onclick = () => send("save");
window.orca.onMessage((msg) => { if (msg && msg.action === "status") status(msg.text, msg.ok); });
</script></body></html>"""


def open_settings_window() -> None:
    settings = Runtime.settings()
    html = (SETTINGS_HTML.replace("__URL__", escape(settings["spoolman_url"], quote=True))
            .replace("__STARTUP__", "checked" if settings["sync_on_startup"] else ""))
    window = None

    def on_message(data):
        data = data if isinstance(data, dict) else {}
        url = normalize_url(str(data.get("url", "")))
        if data.get("action") == "test":
            def test():
                try:
                    count = len(Spoolman(url).active_filaments())
                    window.post({"action": "status", "ok": True,
                                 "text": f"Connected: {count} filament(s) with active spools."})
                except Exception as exc:
                    window.post({"action": "status", "ok": False, "text": str(exc)})
            threading.Thread(target=test, daemon=True).start()
        elif data.get("action") == "save":
            if not url:
                window.post({"action": "status", "ok": False, "text": "Enter the Spoolman address."})
                return
            Runtime.save_settings({"spoolman_url": url, "sync_on_startup": bool(data.get("sync_on_startup"))})
            window.close()
            run_sync(automatic=False)

    window = orca.host.ui.create_window(html=html, title=f"{PLUGIN_NAME} Settings", width=560, height=360,
                                        on_message=on_message)


if orca is not None:
    class SyncCapability(orca.script.ScriptPluginCapabilityBase):
        def get_name(self):
            return "Sync Spoolman Filaments"

        def on_load(self):
            Runtime.init_storage()
            settings = Runtime.settings()
            if settings["spoolman_url"] and settings["sync_on_startup"]:
                # Give OrcaSlicer time to finish loading its profiles first.
                timer = threading.Timer(5.0, run_sync, kwargs={"automatic": True})
                timer.daemon = True
                timer.start()

        def on_lifecycle_event(self, event, context):
            # May run on a worker thread: only schedule the work here.
            if event_name(event) != "PresetSaved":
                return
            name = str(getattr(context, "name", "") or "")
            if name.endswith("]") and "[Spoolman " in name:
                timer = threading.Timer(1.0, run_push, args=(name,))
                timer.daemon = True
                timer.start()

        def execute(self):
            Runtime.init_storage()
            run_sync(automatic=False)
            return orca.ExecutionResult.success("Spoolman sync started")

    class SettingsCapability(orca.script.ScriptPluginCapabilityBase):
        def get_name(self):
            return "Spoolman Filaments Settings"

        def on_load(self):
            Runtime.init_storage()
            if not Runtime.settings()["spoolman_url"]:
                open_settings_window()

        def execute(self):
            Runtime.init_storage()
            open_settings_window()
            return orca.ExecutionResult.success("Settings opened")

    @orca.plugin
    class SpoolmanFilamentsPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(SyncCapability)
            orca.register_capability(SettingsCapability)
