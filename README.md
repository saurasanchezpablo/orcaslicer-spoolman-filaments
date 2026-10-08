<img src="assets/icon.png" alt="" width="96" align="right">

# Spoolman Filaments for OrcaSlicer

An OrcaSlicer plugin that keeps one filament profile per
[Spoolman](https://github.com/Donkie/Spoolman) filament, in sync both ways.

- **One profile per filament, not per spool.** Ten spools of the same PLA give
  you one profile, `SUNLU PLA Matte white [Spoolman 3]`.
- **Good defaults.** Each profile starts from the closest OrcaSlicer system
  profile: same vendor, material and modifiers ("Matte", "PRO", "Silk"…) when
  one exists, preferring variants tuned for your current printer, and
  `Generic <material>` otherwise. Material names like `Flexible (TPU)` map to
  `TPU`.
- **Spoolman is the source of truth.** Vendor, material, colour, density,
  diameter, price and temperatures come from Spoolman, plus any of the
  `orca_<setting>` extra fields the plugin adds to Spoolman filaments (flow
  ratio, max volumetric speed, pressure advance, plate temperatures,
  retraction, fan speeds, notes…).
- **Edits go back to Spoolman.** Saving one of these profiles in OrcaSlicer
  writes the changed settings to that filament's extra fields in Spoolman.
- **Printer lanes pick the right profile.** Each profile carries
  `filament_id = SPOOLMAN_<filament id>`. When OrcaSlicer syncs filaments from
  a Moonraker printer whose lanes report Spoolman spools (e.g. AFC), it selects
  the matching profile per lane. That part is done by OrcaSlicer itself and
  needs a build with Spoolman lane matching (see [Lane matching](#lane-matching)).
- **Orca cloud aware.** When you're logged in to an Orca account, new
  profiles are uploaded, updates are pushed, and profiles whose filament runs
  out are deleted from the cloud too.

## Requirements

- OrcaSlicer 2.5 or newer, with plugin support.
- A Spoolman server reachable from the computer running OrcaSlicer.

## Install

1. Download [`spoolman_filaments.py`](spoolman_filaments.py).
2. In OrcaSlicer, open **Plugins**, choose to install a plugin from a file,
   and pick `spoolman_filaments.py`. Enable it.
3. The settings window opens on first start. Enter your Spoolman address
   (e.g. `http://192.168.1.50:7912`), check **Test connection**, then
   **Save and sync**.
4. Allow network access to your Spoolman host when OrcaSlicer asks.
5. Restart OrcaSlicer when the sync report asks you to. Profiles written
   while OrcaSlicer runs are loaded at the next start.

## Use

- **Sync Spoolman Filaments** (plugin action) syncs on demand. With
  **Sync when OrcaSlicer starts** on, it also runs a few seconds after
  OrcaSlicer opens. It only shows a report when something changed or failed.
- Edit and save a `[Spoolman N]` profile as usual; the changed settings are
  written to Spoolman within a second. If Spoolman can't be reached, the next
  sync retries.
- **Spoolman Filaments Settings** reopens the settings window.

Settings that have no `orca_<setting>` field in Spoolman stay as edited in the
profile and are never overwritten by a sync.

## How a profile is built

Lowest to highest precedence:

1. The chosen OrcaSlicer system profile, copied in full. The profile is
   standalone (it doesn't inherit), because OrcaSlicer only keeps a
   profile's own `filament_id` when it has no parent.
2. Spoolman filament fields: vendor, material, colour, density, diameter,
   price ÷ weight → cost, extruder temperature, bed temperature (all plates).
3. Spoolman `orca_<setting>` extra fields.
4. Edits saved in OrcaSlicer (and then written to Spoolman).

Profiles are named `<vendor> <filament name> [Spoolman <id>]`. An `@` in a
name is replaced, because OrcaSlicer would otherwise limit the profile to the
printer named after it.

## Lane matching

OrcaSlicer's Moonraker filament sync only reads each lane's material, so on
its own it picks the generic profile for that material. Spoolman lane
matching makes it look up the lane's `spool_id` through Moonraker's Spoolman
proxy and use the compatible profile with `filament_id SPOOLMAN_<filament
id>`. Without it, the plugin still keeps your profiles in sync; you pick them
per lane yourself.

## Coming from PipSpool

The plugin uses the same `orca_<setting>` Spoolman field keys as
[PipSpool](https://github.com/Gadonk/pipspool-orcaslicer), so the settings
PipSpool stored in Spoolman carry over. Disable PipSpool first, and delete its
`(#N) … - PipSpool` profiles inside OrcaSlicer so the Orca cloud deletes them
too. The two would otherwise both write profiles from the same Spoolman data.

## Development

```sh
python3 -m unittest discover -s tests   # sync logic, no OrcaSlicer needed
python3 dev/install.py                  # copy the plugin into OrcaSlicer's plugin folder
```

`spoolman_filaments.py` is the whole plugin. Everything above the "OrcaSlicer
glue" section is plain Python and tested in `tests/`.

## License

[MIT](LICENSE)
