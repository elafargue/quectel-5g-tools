-- AT command response parsing
-- Unified parsing approach for Quectel modem responses

local M = {}

--- Parse a single line of AT response, extracting values after prefix
-- Handles quoted strings and numeric values
-- @param line The response line
-- @param prefix The expected prefix (e.g., "+QENG")
-- @return Table of values, or nil if line doesn't match prefix
function M.parse_line(line, prefix)
    line = line:match("^%s*(.-)%s*$")  -- trim
    if line == "" or line == "OK" or line == "ERROR" then
        return nil
    end

    local expected = prefix .. ":"
    if line:sub(1, #expected) ~= expected then
        return nil
    end

    local content = line:sub(#expected + 1):match("^%s*(.-)%s*$")
    local values = {}

    -- Parse comma-separated values, handling quoted strings
    local pos = 1
    while pos <= #content do
        local char = content:sub(pos, pos)
        if char == '"' then
            -- Quoted string
            local end_quote = content:find('"', pos + 1)
            if end_quote then
                table.insert(values, content:sub(pos + 1, end_quote - 1))
                pos = end_quote + 1
                -- Skip comma
                if content:sub(pos, pos) == "," then
                    pos = pos + 1
                end
            else
                break
            end
        elseif char == "," then
            -- Empty value
            table.insert(values, "")
            pos = pos + 1
        else
            -- Unquoted value
            local comma = content:find(",", pos)
            local val
            if comma then
                val = content:sub(pos, comma - 1)
                pos = comma + 1
            else
                val = content:sub(pos)
                pos = #content + 1
            end
            val = val:match("^%s*(.-)%s*$")  -- trim
            table.insert(values, val)
        end
    end

    return values
end

--- Parse multi-line AT response, yielding all matching lines
-- @param text Full AT response text
-- @param prefix The expected prefix (e.g., "+QENG")
-- @return Iterator function yielding value tables
function M.parse_response(text, prefix)
    local lines = {}
    for line in text:gmatch("[^\r\n]+") do
        table.insert(lines, line)
    end

    local i = 0
    return function()
        while true do
            i = i + 1
            if i > #lines then return nil end
            local values = M.parse_line(lines[i], prefix)
            if values then return values end
        end
    end
end

--- Parse ATI response for device info
-- @param text ATI response
-- @return Table with manufacturer, model, revision
function M.parse_ati(text)
    local lines = {}
    for raw in text:gmatch("[^\r\n]+") do
        local line = raw:match("^%s*(.-)%s*$")
        if line ~= "" and line ~= "OK" then
            table.insert(lines, line)
        end
    end

    return {
        manufacturer = lines[1] or "",
        model = lines[2] or "",
        revision = (lines[3] or ""):gsub("^Revision:%s*", "")
    }
end

--- Parse +QSPN response for operator info
-- @param text AT+QSPN response
-- @return Table with operator name, short name, mcc_mnc
function M.parse_qspn(text)
    local iter = M.parse_response(text, "+QSPN")
    local values = iter()
    if values then
        return {
            operator = values[1] or "",
            operator_short = values[2] or "",
            mcc_mnc = values[5] or ""
        }
    end
    return nil
end

--- Parse +QENG="servingcell" response
-- @param text AT+QENG="servingcell" response
-- @return Table with serving cell info (LTE and optionally NR5G-NSA)
--
-- Handles two response formats:
--   Two-line:  +QENG: "servingcell","NOCONN"  /  +QENG: "LTE","FDD",...
--   One-line:  +QENG: "servingcell","NOCONN","LTE","FDD",...

-- Parse LTE fields from values array starting at offset o
-- o is the index of the duplex field (FDD/TDD)
local function parse_lte_fields(values, o)
    return {
        duplex = values[o],
        mcc = tonumber(values[o + 1]),
        mnc = tonumber(values[o + 2]),
        cell_id = values[o + 3],
        pci = tonumber(values[o + 4]),
        arfcn = tonumber(values[o + 5]),
        band = tonumber(values[o + 6]),
        bandwidth_dl = tonumber(values[o + 7]),
        bandwidth_ul = tonumber(values[o + 8]),
        tac = values[o + 9],
        rsrp = tonumber(values[o + 10]),
        rsrq = tonumber(values[o + 11]),
        rssi = tonumber(values[o + 12]),
        sinr = tonumber(values[o + 13]),
    }
end

-- Parse NR5G-NSA fields from values array starting at offset o
-- o is the index of the mcc field
local function parse_nr5g_fields(values, o)
    return {
        mcc = tonumber(values[o]),
        mnc = tonumber(values[o + 1]),
        pci = tonumber(values[o + 2]),
        rsrp = tonumber(values[o + 3]),
        sinr = tonumber(values[o + 4]),
        rsrq = tonumber(values[o + 5]),
        arfcn = tonumber(values[o + 6]),
        band = tonumber(values[o + 7]),
        bandwidth = tonumber(values[o + 8]),
        scs = tonumber(values[o + 9]),
    }
end

-- Parse NR5G-SA fields from values array starting at offset o
-- o is the index of the duplex (FDD/TDD) field.
--
-- Standalone NR has no LTE anchor: the serving cell *is* the NR cell, and
-- the line carries a full cell identity (cell_id/TAC) plus the signal
-- triplet, in a different order from the NSA variant above. Confirmed
-- against an RM520N on T-Mobile US (310/260):
--
--   +QENG: "servingcell","NOCONN","NR5G-SA","FDD",310,260,1C5EC3016,484,
--          2F8D00,396250,25,3,-108,-10,15,0,-
--
-- The arfcn (396250) and band (25) cross-check against the same modem's
-- AT+QNWINFO ("NR5G BAND 25", 396250) and AT+QCAINFO PCC line, which
-- pins the field offsets: the tail is <RSRP>,<RSRQ>,<SINR>, not the
-- <RSRP>,<SINR>,<RSRQ> order NSA uses.
local function parse_nr5g_sa_fields(values, o)
    return {
        duplex = values[o],
        mcc = tonumber(values[o + 1]),
        mnc = tonumber(values[o + 2]),
        cell_id = values[o + 3],
        pci = tonumber(values[o + 4]),
        tac = values[o + 5],
        arfcn = tonumber(values[o + 6]),
        band = tonumber(values[o + 7]),
        bandwidth = tonumber(values[o + 8]),
        rsrp = tonumber(values[o + 9]),
        rsrq = tonumber(values[o + 10]),
        sinr = tonumber(values[o + 11]),
        scs = tonumber(values[o + 12]),
    }
end

-- Parse WCDMA fields from values array starting at offset o
-- o is the index of the MCC field.
--
-- Field order from the RM520N AT manual (doc/quectel-rm520n-excerpt.pdf,
-- "AT+QENG Query Primary Serving Cell and Neighbour Cell Information",
-- In WCDMA mode):
--
--   +QENG: "servingcell",<state>,"WCDMA",<MCC>,<MNC>,<LAC>,<cellID>,
--          <uarfcn>,<PSC>,<RAC>,<RSCP>,<ecio>,<phych>,<SF>,<slot>,
--          <speech_code>,<comMod>
--
-- DERIVED FROM THE MANUAL, NOT FROM A LIVE CAPTURE. Every other branch here
-- was pinned against real output from a real operator, and each time that
-- turned up something the manual did not say: a one-character TAC, a PCI of
-- 0, a six-digit cell ID. Treat the offsets below as the manual's claim
-- until a 3G capture confirms them, and see tests/test-parser Test 22.
--
-- Three things deliberately do not reuse the LTE/NR names:
--
--   * `uarfcn`, not `arfcn`. UTRA-ARFCN is a different raster, and
--     frequency.lua's tables and formulae are for E-UTRA and NR only. Naming
--     it `arfcn` would invite a confident, wrong frequency.
--   * `rscp` and `ecio`, not `rsrp` and `rsrq`. Received Signal Code Power
--     and Ec/No are related quantities but not the same ones, and the
--     thresholds in thresholds.lua do not apply to them.
--   * there is no SINR at all in WCDMA. The manual lists no such field, so
--     anything aiming by SINR -- 5g-monitor's beeps, the SINR panels -- has
--     nothing to work with on 3G. That absence is the honest answer.
--
-- `lac` and `cell_id` stay strings for the same reason `tac` does: they are
-- hexadecimal, and coercing them to numbers loses leading characters.
-- The serving cell's quality field is <ecio> in Quectel's manual while the
-- neighbour line's is <ecno>, for the same CPICH measurement. Both names are
-- kept as the modem gives them -- a parser that renamed them would be harder
-- to check against the manual -- and the display calls both Ec/No, which is
-- the 3GPP term.
-- 0x8000, the smallest 16-bit signed integer, which this modem sends for a
-- field it has no value for. Seen in <rank> on a live Free/Orange 3G attach,
-- on every neighbour of every line, while RSCP and Ec/No beside it were
-- ordinary readings.
--
-- Guarded on the signal fields as well, though it has not been seen there:
-- -32768 is not a value either can take -- RSCP runs to about -120 dBm and
-- Ec/No to about -24 dB -- so treating it as absent is right whether or not
-- the modem ever uses it that way. Left unguarded and scaled it becomes
-- -3276.8 dBm, which stores as a reading and drags the whole panel's y-axis
-- down to it, flattening every real value into a line.
local UNAVAILABLE = -32768

local function available(value)
    local n = tonumber(value)
    if not n or n == UNAVAILABLE then return nil end
    return n
end

local function parse_wcdma_fields(values, o)
    return {
        mcc = tonumber(values[o]),
        mnc = tonumber(values[o + 1]),
        lac = values[o + 2],
        cell_id = values[o + 3],
        uarfcn = tonumber(values[o + 4]),
        psc = tonumber(values[o + 5]),
        rac = tonumber(values[o + 6]),
        rscp = available(values[o + 7]),
        ecio = available(values[o + 8]),
        phych = tonumber(values[o + 9]),
        sf = tonumber(values[o + 10]),
        slot = tonumber(values[o + 11]),
        speech_code = tonumber(values[o + 12]),
        commod = tonumber(values[o + 13]),
    }
end

function M.parse_serving_cell(text)
    local result = {
        state = nil,
        mode = nil,
        lte = nil,
        nr5g = nil
    }

    for values in M.parse_response(text, "+QENG") do
        local cell_type = values[1]

        if cell_type == "servingcell" then
            result.state = values[2]

            -- Handle single-line format: "servingcell","NOCONN","LTE","FDD",...
            if values[3] == "LTE" then
                result.lte = parse_lte_fields(values, 4)
            elseif values[3] == "NR5G-NSA" then
                result.mode = "NSA"
                result.nr5g = parse_nr5g_fields(values, 4)
            elseif values[3] == "NR5G-SA" then
                result.mode = "SA"
                result.nr5g = parse_nr5g_sa_fields(values, 4)
            elseif values[3] == "WCDMA" then
                -- Single-line only: the manual documents no multi-line form
                -- for WCDMA, because there is no anchor/secondary split to
                -- express. A bare "WCDMA" continuation line is therefore not
                -- handled, unlike LTE and NR5G-NSA which genuinely have one.
                result.wcdma = parse_wcdma_fields(values, 4)
            end

        elseif cell_type == "LTE" then
            result.lte = parse_lte_fields(values, 2)

        elseif cell_type == "NR5G-NSA" then
            result.mode = "NSA"
            result.nr5g = parse_nr5g_fields(values, 2)

        elseif cell_type == "NR5G-SA" then
            result.mode = "SA"
            result.nr5g = parse_nr5g_sa_fields(values, 2)
        end
    end

    return result
end

--- Parse +QCAINFO response for carrier aggregation
-- @param text AT+QCAINFO response
-- @return Table with pcc (primary) and scc (list of secondary carriers)
--
-- Formats from Quectel RM520N-GL documentation:
--
-- PCC formats vary by RAT:
--   10 fields (LTE):  "PCC",<earfcn>,<bandwidth>,<band>,<state>,<pcid>,<rsrp>,<rsrq>,<rssi>,<rssnr>
--   5 fields (NR5G):  "PCC",<arfcn>,<bw_idx>,<band>,<pcid>   -- standalone (SA)
--
-- SCC formats vary by field count:
--   5 fields (NR5G):  "SCC",<arfcn>,<bw_idx>,<band>,<pcid>
--   9 fields:         "SCC",<earfcn>,<bw>,<band>,<state>,<pcid>,<ul_cfg>,<ul_band>,<ul_earfcn>
--   12 fields:        "SCC",<earfcn>,<bw>,<band>,<state>,<pcid>,<ul_cfg>,<ul_band>,<ul_earfcn>,<rsrp>,<rsrq>,<rssnr>
--   13 fields:        "SCC",<earfcn>,<bw>,<band>,<state>,<pcid>,<rsrp>,<rsrq>,<rssi>,<rssnr>,<ul_cfg>,<ul_band>,<ul_earfcn>
--
-- Note: rssnr is NOT the same as SINR from QENG="servingcell". Use serving cell SINR for display.
--
-- Examples:
--   +QCAINFO: "PCC",1350,100,"LTE BAND 3",1,427,-94,-15,-58,-1
--   +QCAINFO: "SCC",275,75,"LTE BAND 1",1,406,-102,-20,-74,-10,0,-,-
--   +QCAINFO: "SCC",648768,10,"NR5G BAND 78",697
function M.parse_qcainfo(text)
    local result = {
        pcc = nil,
        scc = {}
    }

    -- Helper to parse int, returning nil for "-" or empty
    local function parse_int(val)
        if not val or val == "-" or val == "" then return nil end
        return tonumber(val)
    end

    for values in M.parse_response(text, "+QCAINFO") do
        local num_fields = #values
        -- Lua 5.1 compatible: use if instead of goto
        if num_fields >= 5 then
            local role = values[1]  -- "PCC" or "SCC"
            local band_str = values[4] or ""

            -- Parse band string like "LTE BAND 1" or "NR5G BAND 78"
            local rat, band_num = band_str:match("(%w+) BAND (%d+)")
            local is_nr = (rat == "NR5G")

            local carrier = {
                role = role:lower(),
                arfcn = parse_int(values[2]),
                bandwidth_rb = parse_int(values[3]),
                band = tonumber(band_num),
                rat = is_nr and "5g" or "lte",
            }

            if role == "PCC" then
                if num_fields >= 10 then
                    -- LTE PCC: role,earfcn,bw,band,state,pci,rsrp,rsrq,rssi,rssnr
                    carrier.state = parse_int(values[5])
                    carrier.pci = parse_int(values[6])
                    carrier.rsrp = parse_int(values[7])
                    carrier.rsrq = parse_int(values[8])
                    carrier.rssi = parse_int(values[9])
                    carrier.rssnr = parse_int(values[10])
                elseif num_fields == 5 then
                    -- NR5G PCC (standalone): role,arfcn,bw_idx,band,pci.
                    -- Same short shape as an NR5G SCC — in SA the primary
                    -- carrier is NR, so there is no LTE anchor line here
                    -- and no signal fields. RSRP/RSRQ/SINR arrive via
                    -- backfill from QENG="servingcell" instead.
                    carrier.pci = parse_int(values[5])
                end
                result.pcc = carrier
            else
                -- SCC format varies by field count
                if num_fields == 5 then
                    -- NR5G SCC: role,arfcn,bw_idx,band,pci
                    carrier.pci = parse_int(values[5])
                elseif num_fields == 9 then
                    -- SCC without signal: role,earfcn,bw,band,state,pci,ul_cfg,ul_band,ul_earfcn
                    carrier.state = parse_int(values[5])
                    carrier.pci = parse_int(values[6])
                    carrier.ul_configured = parse_int(values[7])
                    carrier.ul_band = values[8] ~= "-" and values[8] or nil
                    carrier.ul_earfcn = parse_int(values[9])
                elseif num_fields == 12 then
                    -- SCC with signal (different order): role,earfcn,bw,band,state,pci,ul_cfg,ul_band,ul_earfcn,rsrp,rsrq,rssnr
                    carrier.state = parse_int(values[5])
                    carrier.pci = parse_int(values[6])
                    carrier.ul_configured = parse_int(values[7])
                    carrier.ul_band = values[8] ~= "-" and values[8] or nil
                    carrier.ul_earfcn = parse_int(values[9])
                    carrier.rsrp = parse_int(values[10])
                    carrier.rsrq = parse_int(values[11])
                    carrier.rssnr = parse_int(values[12])
                elseif num_fields >= 13 then
                    -- Full SCC: role,earfcn,bw,band,state,pci,rsrp,rsrq,rssi,rssnr,ul_cfg,ul_band,ul_earfcn
                    carrier.state = parse_int(values[5])
                    carrier.pci = parse_int(values[6])
                    carrier.rsrp = parse_int(values[7])
                    carrier.rsrq = parse_int(values[8])
                    carrier.rssi = parse_int(values[9])
                    carrier.rssnr = parse_int(values[10])
                    carrier.ul_configured = parse_int(values[11])
                    carrier.ul_band = values[12] ~= "-" and values[12] or nil
                    carrier.ul_earfcn = parse_int(values[13])
                end
                table.insert(result.scc, carrier)
            end
        end
    end

    return result
end

--- Parse +QENG="neighbourcell" response
-- @param text AT+QENG="neighbourcell" response
-- @return List of neighbour cells
-- Scale factor on the RSCP and Ec/No a WCDMA neighbour line reports.
--
-- Inferred from the Free/Orange capture, not from the manual, which gives no
-- units: the serving cell reported RSCP -69 dBm and AT+CSQ independently
-- agreed at -69, while its neighbours on the same UARFCN reported -770 and
-- -790. Unscaled those are not physical; at a tenth they are -77.0 and -79.0,
-- weaker than the serving cell as neighbours should be. Ec/No lands the same
-- way: -135 and -155 against the serving cell's -4 dB.
--
-- The serving line is *not* scaled, so the two must be reconciled here or a
-- neighbour sorts above the cell we are actually on.
local WCDMA_NEIGHBOUR_SCALE = 10

local function scaled(value)
    local n = available(value)
    if not n then return nil end
    return n / WCDMA_NEIGHBOUR_SCALE
end

--- Parse +QENG="neighbourcell" response
-- @param text Raw AT response
-- @param serving_technology Technology of the serving cell ("WCDMA", "LTE",
--        "NSA", "SA"), which disambiguates the two WCDMA neighbour layouts.
--        Omit it and a WCDMA neighbour records only its UARFCN.
-- @return Array of neighbour cells
function M.parse_neighbours(text, serving_technology)
    local neighbours = {}

    for values in M.parse_response(text, "+QENG") do
        local cell_type = values[1]

        if cell_type:match("^neighbourcell") then
            local scope = cell_type:match("neighbourcell (%w+)")  -- "intra" or "inter"
            local rat = values[2]  -- "LTE" or "WCDMA"

            if rat == "WCDMA" then
                -- A WCDMA neighbour is reported while camped on LTE as well
                -- as while camped on 3G, so this line reaches us on a modem
                -- that never leaves LTE. Falling through to the LTE layout
                -- below is silently wrong: <cell_resel_priority> lands in pci
                -- and the two reselection thresholds land in rsrq and rsrp.
                -- Small integers, entirely plausible on screen, and strong
                -- enough that print_neighbours would sort them to the top of
                -- the five it shows.
                --
                -- The manual gives two layouts of the same length, differing
                -- in where PSC and RSCP sit:
                --
                --   LTE mode:   <uarfcn>,<cell_resel_priority>,<thresh_Xhigh>,
                --               <thresh_Xlow>,<PSC>,<RSCP>,<ecno>,<srxlev>
                --   WCDMA mode: <uarfcn>,<srxqual>,<PSC>,<RSCP>,<ecno>,<set>,
                --               <rank>,<srxlev>
                --
                -- Nothing in the line says which. What does say is the
                -- serving cell, read from the same modem moments earlier, so
                -- the caller passes it in. Told nothing, we still record only
                -- the UARFCN rather than guess -- putting a reselection
                -- threshold in a signal field is the failure being avoided.
                local n = { scope = scope, rat = "wcdma",
                            uarfcn = tonumber(values[3]) }

                if serving_technology == "WCDMA" then
                    -- Confirmed against a live Free/Orange 3G attach.
                    n.psc  = tonumber(values[5])
                    n.rscp = scaled(values[6])
                    n.ecno = scaled(values[7])
                elseif serving_technology == "LTE"
                        or serving_technology == "NSA"
                        or serving_technology == "SA" then
                    -- Doc-derived. No capture of a WCDMA neighbour seen from
                    -- an LTE serving cell has ever reached this repository,
                    -- and every capture so far has corrected the manual in
                    -- some way -- so treat these three with suspicion until
                    -- one does.
                    n.psc  = tonumber(values[7])
                    n.rscp = scaled(values[8])
                    n.ecno = scaled(values[9])
                end

                table.insert(neighbours, n)
            elseif rat == "LTE" and serving_technology == "WCDMA" then
                -- An LTE neighbour seen *from* a WCDMA serving cell, which
                -- the manual gives its own layout in the "In WCDMA mode"
                -- section -- and it is the LTE-mode one with RSRP and RSRQ
                -- the other way round:
                --
                --   LTE mode:   ...,<PCID>,<RSRQ>,<RSRP>,<RSSI>,<SINR>,...
                --   WCDMA mode: ...,<PCID>,<RSRP>,<RSRQ>,<srxlev>
                --
                -- Read with the LTE-mode order it stored an RSRQ of about
                -- -12 as an RSRP, which is not a power any cell transmits
                -- and is strong enough that print_neighbours sorted those
                -- rows above every real neighbour -- the same failure a
                -- WCDMA neighbour read as an LTE one used to have, arriving
                -- from the opposite direction.
                --
                -- There is no intra/inter suffix on this line, so scope
                -- stays nil: from a WCDMA cell every LTE neighbour is on
                -- another radio, and the modem does not classify them.
                table.insert(neighbours, {
                    scope = scope,
                    rat = rat:lower(),
                    arfcn = tonumber(values[3]),
                    pci = tonumber(values[4]),
                    rsrp = tonumber(values[5]),
                    rsrq = tonumber(values[6]),
                    srxlev = tonumber(values[7]),
                })
            elseif rat == "LTE" then
                table.insert(neighbours, {
                    scope = scope,
                    rat = rat:lower(),
                    arfcn = tonumber(values[3]),
                    pci = tonumber(values[4]),
                    rsrq = tonumber(values[5]),
                    rsrp = tonumber(values[6]),
                    rssi = tonumber(values[7]),
                })
            else
                -- A radio with no layout here. This used to be the LTE
                -- branch's job -- it caught everything that was not WCDMA --
                -- so an "NR5G" neighbour would have been read with LTE field
                -- offsets, which is the trap WCDMA neighbours were in and
                -- LTE-from-3G ones after them. Two guesses in a row were
                -- wrong; there is no reason to expect a third to be right.
                --
                -- Nothing is known to produce one. The manual documents an
                -- "In LTE mode" and an "In WCDMA mode" neighbour layout and
                -- nothing else, and a live 5G standalone attach returned a
                -- bare OK on every attempt. So this is a guard, not support:
                -- if a line ever does arrive, the channel number is recorded
                -- and the signal fields are not invented.
                --
                -- `channel`, not `arfcn`: an NR-ARFCN is a different raster,
                -- and naming it arfcn would invite add_frequency_info() to
                -- read it against the E-UTRA table -- the same reason a
                -- WCDMA neighbour's is called uarfcn.
                table.insert(neighbours, {
                    scope = scope,
                    rat = rat:lower(),
                    channel = tonumber(values[3]),
                })
            end
        end
    end

    return neighbours
end

--- Parse +QNWPREFCFG response for band configuration
-- @param text AT+QNWPREFCFG response
-- @return setting name, value (string or table of bands)
function M.parse_qnwprefcfg(text)
    local iter = M.parse_response(text, "+QNWPREFCFG")
    local values = iter()
    if not values then
        return nil, nil
    end

    local setting = values[1]
    local value = values[2]

    -- Parse band lists like "1:3:7:20"
    if setting:match("band$") and value then
        local bands = {}
        for band in value:gmatch("(%d+)") do
            table.insert(bands, tonumber(band))
        end
        return setting, bands
    end

    return setting, value
end

--- Parse +QNWPREFCFG multi-line response for a specific setting
-- Used for ue_capability_band/policy_band which return multiple lines
-- @param text AT+QNWPREFCFG response (may contain multiple +QNWPREFCFG lines)
-- @param target Setting name to find (e.g., "lte_band", "nsa_nr5g_band")
-- @return setting name, value (string or table of bands)
function M.parse_qnwprefcfg_from(text, target)
    for values in M.parse_response(text, "+QNWPREFCFG") do
        local setting = values[1]
        if setting == target then
            local value = values[2]
            if setting:match("band$") and value then
                local bands = {}
                for band in value:gmatch("(%d+)") do
                    table.insert(bands, tonumber(band))
                end
                return setting, bands
            end
            return setting, value
        end
    end
    return nil, nil
end

--- Parse +QNWLOCK response for cell lock status
-- @param text AT+QNWLOCK="common/4g" or "common/5g" response
-- @return Table with type, num_cells, cells list; or nil if not parseable
--
-- 4G format: +QNWLOCK: "common/4g",<num>[,<earfcn>,<pci>,...]
-- 5G format: +QNWLOCK: "common/5g",<num>[,<pci>,<arfcn>,<scs>,<band>,...]
function M.parse_qnwlock(text)
    for values in M.parse_response(text, "+QNWLOCK") do
        local lock_type = values[1]  -- "common/4g" or "common/5g"

        if lock_type == "common/4g" then
            local num_cells = tonumber(values[2]) or 0
            local cells = {}
            -- Pairs of earfcn,pci starting at index 3
            local i = 3
            while i + 1 <= #values do
                table.insert(cells, {
                    earfcn = tonumber(values[i]),
                    pci = tonumber(values[i + 1]),
                })
                i = i + 2
            end
            return { type = "4g", num_cells = num_cells, cells = cells }

        elseif lock_type == "common/5g" then
            local num_cells = tonumber(values[2]) or 0
            local cells = {}
            -- Groups of pci,arfcn,scs,band starting at index 3
            local i = 3
            while i + 3 <= #values do
                table.insert(cells, {
                    pci = tonumber(values[i]),
                    arfcn = tonumber(values[i + 1]),
                    scs = tonumber(values[i + 2]),
                    band = tonumber(values[i + 3]),
                })
                i = i + 4
            end
            return { type = "5g", num_cells = num_cells, cells = cells }
        end
    end
    return nil
end

return M
