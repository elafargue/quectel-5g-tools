# Overview

Tools for monitoring and configuring Quectel 5G modems on OpenWRT, originally developed for the GL.INET X-3000 with Quectel RM520N-GL modem and Poynting XPOL-24 directional antenna.

The goal is to extract signal information from the modem to aid accurate pointing of directional antennas, with audio feedback based on 5G SINR values.

## Project Status

**IMPLEMENTED** in Lua:

- `lua/quectel/` - Core library for modem communication and AT command parsing
- `lua/quectel/utils.lua` - Shared utilities (sleep, format_band, extract_enodeb, add_frequency_info, same_cell)
- `lua/prometheus-collectors/quectel.lua` - Signal/cell metrics exporter
- `lua/prometheus-collectors/quectel-watchdog.lua` - Watchdog state exporter
- `bin/5g-info` - CLI tool for displaying modem info (table/JSON output)
- `bin/5g-monitor` - ncurses TUI with color-coded signals and beep feedback
- `bin/at` - Simple AT command wrapper (installed as `quectel-at`)
- `bin/5g-lock` - Band and cell locking utility (declarative: `--apply` makes modem match UCI config exactly)
- `bin/5g-led-bars` - procd daemon mapping NR/LTE RSRP to GL-X3000 panel LEDs
- `bin/5g-watchdog` - procd daemon detecting NSA NR SCG drops via mmcli; recovers with `mmcli --disable`/`--enable`, falls back to `--set-allowed-modes` toggle

See `README.md` for usage documentation.

## Key Decisions

- **Language**: Lua (native to OpenWRT, no external deps except luaposix)
- **Modem communication**: Direct serial via luaposix for the AT-based tools; `mmcli` shell-out for `5g-watchdog` (no AT bus contention with MM)
- **Bearer manager (25.12)**: ModemManager owns the WWAN net + bearer; the toolkit's AT tools coexist via `/etc/modemmanager/ignore-tty` + the patched MM tty hotplug script (shipped by `vjt/openwrt`)
- **Configuration**: UCI only (`/etc/config/quectel`)
- **Audio feedback**: Terminal bell (`\a`) for SSH compatibility
- **Metrics**: Prometheus exporter for Grafana integration
- **SINR source**: Always use `AT+QENG="servingcell"` for SINR; QCAINFO reports a different metric (RSSNR)

## Legacy Python Implementation

The original Python implementation is preserved in `legacy/` for reference. See `legacy/README.md` for why we switched to Lua.

## Configuration

UCI config: `/etc/config/quectel`

```
config modem 'modem'
    option device '/dev/ttyUSB2'
    option timeout '2'
    option refresh_interval '5'
    option beeps_enabled '1'
    list lte_bands '1'
    list lte_bands '3'
    list lte_bands '7'
    list lte_bands '20'
    list nr5g_bands '78'
    # Cell locks (earfcn,pci for LTE; pci,arfcn,scs,band for 5G)
    # list lte_cells '275,280'
    # list nr5g_cells '920,648768,15,78'
```

## Sample AT Command Outputs

These sample outputs are used for parser testing:

```
ATI

Quectel
RM520N-GL
Revision: RM520NGLAAR03A03M4G

OK
AT+QSPN

+QSPN: "I TIM","TIM","",0,"22201"

OK
AT+QNWPREFCFG="mode_pref"

+QNWPREFCFG: "mode_pref",AUTO

OK
AT+QNWPREFCFG="nsa_nr5g_band"

+QNWPREFCFG: "nsa_nr5g_band",78

OK
AT+QNWPREFCFG="lte_band"

+QNWPREFCFG: "lte_band",1:3:7:20

OK
AT+QCAINFO

+QCAINFO: "PCC",275,75,"LTE BAND 1",1,280,-99,-14,-67,-4
+QCAINFO: "SCC",1350,100,"LTE BAND 3",1,240,-95,-18,-68,-10,0,-,-
+QCAINFO: "SCC",648768,10,"NR5G BAND 78",920

OK
AT+QENG="servingcell"

+QENG: "servingcell","NOCONN"
+QENG: "LTE","FDD",222,01,328261F,280,275,1,4,4,BE3,-99,-14,-66,7,4,30,-
+QENG: "NR5G-NSA",222,01,920,-96,18,-10,648768,78,10,1

OK
```

The serving cell response can also appear in single-line format (LTE-only, no NR5G):
```
AT+QENG="servingcell"

+QENG: "servingcell","NOCONN","LTE","FDD",222,01,4940206,427,1350,3,5,5,BFF,-93,-11,-61,11,7,50,-

OK
```

The parser handles both formats.

```
AT+QENG="neighbourcell"

+QENG: "neighbourcell intra","LTE",275,280,-14,-99,-67,-,-,-,-,-,-
+QENG: "neighbourcell intra","LTE",275,34,-20,-105,-76,-,-,-,-,-,-
+QENG: "neighbourcell intra","LTE",275,46,-20,-106,-75,-,-,-,-,-,-
+QENG: "neighbourcell inter","LTE",1350,240,-18,-95,-68,-,-,-,-,-
+QENG: "neighbourcell inter","LTE",1350,427,-14,-94,-69,-,-,-,-,-
+QENG: "neighbourcell inter","LTE",1350,465,-17,-94,-68,-,-,-,-,-
+QENG: "neighbourcell inter","LTE",1350,157,-17,-95,-68,-,-,-,-,-
+QENG: "neighbourcell inter","LTE",1350,47,-18,-96,-69,-,-,-,-,-

OK
```

## SINR vs RSSNR

**Important**: The `AT+QCAINFO` command reports a field that Quectel documentation calls `rssnr`, which is **NOT** the same as SINR from `AT+QENG="servingcell"`.

- **SINR** (from `QENG="servingcell"`): Authoritative Signal-to-Interference-plus-Noise Ratio in dB
- **RSSNR** (from `QCAINFO`): A different metric that can differ significantly from SINR

The parser stores QCAINFO's last numeric field as `rssnr`, and the backfill logic in `modem.lua` populates the `sinr` field from the matching serving cell data. This ensures displayed SINR values are accurate.

## Band lookup from ARFCN

The tables in `lua/quectel/frequency.lua` hold **downlink** ranges only, from
3GPP TS 38.104 Table 5.2-1 (FR1), Table 5.2-2 (FR2) and TS 36.101 Table 5.5-1
(LTE). An FDD band's uplink range looks just as plausible in that position and
is silently wrong, which is exactly the bug `tests/test-frequency` was written
to catch — it re-derives the expected ARFCN ranges from the band edges in MHz
rather than restating the tables, so a copied uplink range fails.

Two consequences worth remembering:

- **The frequency never comes from the table.** It is computed with the raster
  formula, so it is exact for any ARFCN on the raster, band known or not. The
  tables only supply the band *label*.
- **An ARFCN cannot identify the band.** n1/n65/n66, n2/n25, n5/n26, n12/n85,
  n41/n90 and n77/n78 share spectrum. The modem always reports the band in
  `QENG` and `QCAINFO`, so pass it to `nrarfcn_to_mhz()`/`format_frequency()`;
  the table lookup is only the fallback when it did not.

SUL bands (n80-n84, n86, n89, n95, n97-n99) are deliberately absent: they have
no downlink, and their ranges would shadow the bands that do.

## Documentation

- `README.md` - User documentation
- `doc/quectel-rm520n-excerpt.pdf` - Quectel modem AT command reference
