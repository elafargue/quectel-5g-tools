-- EARFCN and NR-ARFCN to frequency conversion
-- Based on 3GPP TS 36.101 (LTE) and TS 38.104 (NR: operating bands in
-- Tables 5.2-1 and 5.2-2, channel raster in 5.4.2.1)

local M = {}

-- LTE EARFCN to frequency conversion
-- Format: {band, earfcn_dl_low, earfcn_dl_high, f_dl_low_mhz, earfcn_offset}
-- Formula: F_DL = F_DL_low + 0.1 * (EARFCN - EARFCN_offset)
local LTE_BANDS = {
    -- FDD Bands
    {1, 0, 599, 2110.0, 0},
    {2, 600, 1199, 1930.0, 600},
    {3, 1200, 1949, 1805.0, 1200},
    {4, 1950, 2399, 2110.0, 1950},
    {5, 2400, 2649, 869.0, 2400},
    {6, 2650, 2749, 875.0, 2650},
    {7, 2750, 3449, 2620.0, 2750},
    {8, 3450, 3799, 925.0, 3450},
    {9, 3800, 4149, 1844.9, 3800},
    {10, 4150, 4749, 2110.0, 4150},
    {11, 4750, 4949, 1475.9, 4750},
    {12, 5010, 5179, 729.0, 5010},
    {13, 5180, 5279, 746.0, 5180},
    {14, 5280, 5379, 758.0, 5280},
    {17, 5730, 5849, 734.0, 5730},
    {18, 5850, 5999, 860.0, 5850},
    {19, 6000, 6149, 875.0, 6000},
    {20, 6150, 6449, 791.0, 6150},
    {21, 6450, 6599, 1495.9, 6450},
    {22, 6600, 7399, 3510.0, 6600},
    {23, 7500, 7699, 2180.0, 7500},
    {24, 7700, 8039, 1525.0, 7700},
    {25, 8040, 8689, 1930.0, 8040},
    {26, 8690, 9039, 859.0, 8690},
    {27, 9040, 9209, 852.0, 9040},
    {28, 9210, 9659, 758.0, 9210},
    {29, 9660, 9769, 717.0, 9660},
    {30, 9770, 9869, 2350.0, 9770},
    {31, 9870, 9919, 462.5, 9870},
    {32, 9920, 10359, 1452.0, 9920},
    -- TDD Bands
    {33, 36000, 36199, 1900.0, 36000},
    {34, 36200, 36349, 2010.0, 36200},
    {35, 36350, 36949, 1850.0, 36350},
    {36, 36950, 37549, 1930.0, 36950},
    {37, 37550, 37749, 1910.0, 37550},
    {38, 37750, 38249, 2570.0, 37750},
    {39, 38250, 38649, 1880.0, 38250},
    {40, 38650, 39649, 2300.0, 38650},
    {41, 39650, 41589, 2496.0, 39650},
    {42, 41590, 43589, 3400.0, 41590},
    {43, 43590, 45589, 3600.0, 43590},
    {44, 45590, 46589, 703.0, 45590},
    {45, 46590, 46789, 1447.0, 46590},
    {46, 46790, 54539, 5150.0, 46790},
    {47, 54540, 55239, 5855.0, 54540},
    {48, 55240, 56739, 3550.0, 55240},
    {49, 56740, 58239, 3550.0, 56740},
    {50, 58240, 59089, 1432.0, 58240},
    {51, 59090, 59139, 1427.0, 59090},
    {52, 59140, 60139, 3300.0, 59140},
    {53, 60140, 60254, 2483.5, 60140},
    {65, 65536, 66435, 2110.0, 65536},
    {66, 66436, 67335, 2110.0, 66436},
    {67, 67336, 67535, 738.0, 67336},
    {68, 67536, 67835, 753.0, 67536},
    {69, 67836, 68335, 2570.0, 67836},
    {70, 68336, 68585, 1995.0, 68336},
    {71, 68586, 68935, 617.0, 68586},
}

-- NR5G NR-ARFCN to band lookup
--
-- Downlink NR-ARFCN ranges from 3GPP TS 38.104 Table 5.2-1 (FR1 operating
-- bands) and Table 5.2-2 (FR2), converted with the raster formula below.
-- Downlink only: these ranges label a channel the modem is receiving on, and
-- the uplink of an FDD band is a different slice of spectrum entirely. SUL
-- bands (n80-n84, n86, n89, n95, n97-n99) are therefore absent -- they have
-- no downlink to look up.
--
-- Every FR1 band in Table 5.2-1 that has a downlink is listed, so a band the
-- modem reports can always be recognised. Sorted by (arfcn_low, arfcn_high)
-- so the narrower of two overlapping bands is tried first.
--
-- The overlaps are real and unresolvable. n1/n65/n66, n2/n25, n5/n26,
-- n12/n85, n41/n90 and n77/n78 share spectrum, and the L-band is worse:
-- 1427-1432 MHz is n51, n76, n91 and n93 at once, 1432-1517 MHz is n50, n75,
-- n92 and n94, with n74 overlapping from 1475. n50/n51 are TDD while n75/n76
-- are SDL -- the same spectrum, differing only in whether an uplink shares it
-- -- and a downlink channel number cannot tell them apart. Nothing but the
-- network's own signalling can, which is what the modem reports.
--
-- So this table is the fallback, not the authority: pass the modem's band to
-- nrarfcn_to_mhz() and it is used as given. A band derived from here instead
-- is marked "inferred" and displayed with a "?" so it is never mistaken for a
-- reading. Among identically ranged bands the lowest number is returned; the
-- choice is arbitrary and the marker says so.
--
-- Format: {band, arfcn_low, arfcn_high, is_fr2}
local NR5G_BANDS = {
    -- FR1 Bands (Sub-6 GHz)
    {71, 123400, 130400, false},
    {29, 143400, 145600, false},
    {85, 145600, 149200, false},
    {12, 145800, 149200, false},
    {67, 147600, 151600, false},
    {13, 149200, 151200, false},
    {14, 151600, 153600, false},
    {28, 151600, 160600, false},
    {20, 158200, 164200, false},
    {26, 171800, 178800, false},
    {18, 172000, 175000, false},
    {5, 173800, 178800, false},
    {100, 183880, 185000, false},
    {8, 185000, 192000, false},
    -- 1427-1432 MHz: n51 is TDD, n76 SDL, n91/n93 FDD downlinks
    {51, 285400, 286400, false},
    {76, 285400, 286400, false},
    {91, 285400, 286400, false},
    {93, 285400, 286400, false},
    -- 1432-1517 MHz: n50 is TDD, n75 SDL, n92/n94 FDD downlinks
    {50, 286400, 303400, false},
    {75, 286400, 303400, false},
    {92, 286400, 303400, false},
    {94, 286400, 303400, false},
    {74, 295000, 303600, false},
    {24, 305000, 311800, false},
    {3, 361000, 376000, false},
    {39, 376000, 384000, false},
    {101, 380000, 384000, false},
    {2, 386000, 398000, false},
    {25, 386000, 399000, false},
    {70, 399000, 404000, false},
    {34, 402000, 405000, false},
    {1, 422000, 434000, false},
    {66, 422000, 440000, false},
    {65, 422000, 440000, false},
    {40, 460000, 480000, false},
    {30, 470000, 472000, false},
    {53, 496700, 499000, false},
    -- n41/n90 share spectrum exactly; TS 38.101-1 Table 5.4.2.3-1 gives
    -- n41 as 499200-537999 on the 15 kHz raster and 499200-538000 on the
    -- 30 kHz one, so the wider bound is the band's real top channel.
    {41, 499200, 538000, false},
    {90, 499200, 538000, false},
    {38, 514000, 524000, false},
    {7, 524000, 538000, false},
    {78, 620000, 653333, false},
    {77, 620000, 680000, false},
    {48, 636667, 646666, false},
    {79, 693334, 733333, false},
    {46, 743334, 795000, false},
    -- 6 GHz NR-U; n102/n104 are the halves of n96
    {102, 795000, 828333, false},
    {96, 795000, 875000, false},
    {104, 828334, 875000, false},
    -- FR2 Bands (mmWave)
    {258, 2016667, 2070832, true},
    {257, 2054166, 2104165, true},
    {261, 2070833, 2084999, true},
    {260, 2229166, 2279165, true},
    {259, 2270833, 2337499, true},
}

-- Convert NR-ARFCN to frequency in kHz using 3GPP formula
local function nrarfcn_to_freq_khz(arfcn)
    if arfcn < 600000 then
        -- Range 0-599999: ΔF = 5 kHz
        return arfcn * 5
    elseif arfcn < 2016667 then
        -- Range 600000-2016666: ΔF = 15 kHz, F_REF = 3000 MHz
        return 3000000 + (arfcn - 600000) * 15
    else
        -- Range 2016667-3279165: ΔF = 60 kHz, F_REF = 24250.08 MHz
        return 24250080 + (arfcn - 2016667) * 60
    end
end

--- Convert LTE EARFCN to frequency in MHz and band number
-- @param earfcn E-UTRA Absolute Radio Frequency Channel Number
-- @return freq_mhz, band (or nil, nil if not found)
function M.earfcn_to_mhz(earfcn)
    for _, entry in ipairs(LTE_BANDS) do
        local band, start, stop, freq_low, offset = entry[1], entry[2], entry[3], entry[4], entry[5]
        if earfcn >= start and earfcn <= stop then
            local freq = freq_low + 0.1 * (earfcn - offset)
            return freq, band
        end
    end
    return nil, nil
end

-- Highest NR-ARFCN the raster is defined for (TS 38.104 Table 5.4.2.1-1)
local NRARFCN_MAX = 3279165

--- Test whether an NR-ARFCN falls inside a band's downlink, per the table
-- @param arfcn NR Absolute Radio Frequency Channel Number
-- @param band Band number
-- @return true if it does, false if it does not, nil if the band is unknown
function M.nr_band_contains(arfcn, band)
    local known = false
    for _, entry in ipairs(NR5G_BANDS) do
        if entry[1] == band then
            known = true
            if arfcn >= entry[2] and arfcn <= entry[3] then
                return true
            end
        end
    end
    if not known then return nil end
    return false
end

--- Convert NR5G NR-ARFCN to frequency in MHz and band number
--
-- The frequency comes from the raster formula and never from the band table,
-- so it is exact for any ARFCN on the raster, band known or not.
--
-- The band is only ever a label, and the modem's is authoritative: it comes
-- from the network's own signalling, whereas this table is a static copy of a
-- spec that gets revised. So a reported band is returned as given and is
-- never replaced by a guess -- including when it is a band this table has
-- never heard of, which means the table is behind, not that the modem is
-- wrong. Overlapping allocations make the reverse direction hopeless anyway:
-- 1450 MHz is a valid n50, n75, n92 and n94 channel all at once.
--
-- @param arfcn NR Absolute Radio Frequency Channel Number
-- @param band Optional band number as reported by the modem
-- @return freq_mhz, band, source
--         freq_mhz nil if the ARFCN is off the raster.
--         source is "reported" when the band came from the caller,
--         "inferred" when it was guessed here, nil when there is no band.
--         An inferred band may well be a different band sharing the
--         spectrum, so callers must present it as a guess.
function M.nrarfcn_to_mhz(arfcn, band)
    if arfcn < 0 or arfcn > NRARFCN_MAX then
        return nil, nil, nil
    end

    local freq_mhz = nrarfcn_to_freq_khz(arfcn) / 1000.0

    if band then
        return freq_mhz, band, "reported"
    end

    for _, entry in ipairs(NR5G_BANDS) do
        if arfcn >= entry[2] and arfcn <= entry[3] then
            return freq_mhz, entry[1], "inferred"
        end
    end

    return freq_mhz, nil, nil
end

--- Format EARFCN/NR-ARFCN as human-readable frequency string
-- @param arfcn Channel number
-- @param is_5g True for NR-ARFCN, false for EARFCN
-- @param band Optional band the modem reported. Used as given when present.
-- @return String like "1845.0 MHz (B3)" for a band the modem reported,
--         "1450.0 MHz (n50?)" for one inferred from the ARFCN -- the "?"
--         marks a guess that may name a different band sharing the same
--         spectrum -- "931.0 MHz" when no band could be established at all,
--         or "Unknown (186200)" when the channel number makes no sense. A
--         nil arfcn is "Unknown": ARFCN 0 is a real raster point, so it
--         cannot double as a missing-value marker.
function M.format_frequency(arfcn, is_5g, band)
    if not arfcn then
        return "Unknown"
    end

    local freq, found, source
    local prefix
    if is_5g then
        freq, found, source = M.nrarfcn_to_mhz(arfcn, band)
        prefix = "n"
    else
        -- LTE EARFCNs partition cleanly by band, so the lookup is exact and
        -- there is nothing for a reported band to disambiguate.
        freq, found = M.earfcn_to_mhz(arfcn)
        source = found and "reported" or nil
        prefix = "B"
    end
    if not freq then
        return string.format("Unknown (%d)", arfcn)
    end
    if not found then
        return string.format("%.1f MHz", freq)
    end
    return string.format("%.1f MHz (%s%d%s)", freq, prefix, found,
        source == "inferred" and "?" or "")
end

-- LTE Bandwidth mappings
local LTE_BW_INDEX = {
    [0] = 1.4, [1] = 3, [2] = 5, [3] = 10, [4] = 15, [5] = 20
}
local LTE_BW_RB = {
    [6] = 1.4, [15] = 3, [25] = 5, [50] = 10, [75] = 15, [100] = 20
}

-- NR5G Bandwidth index mapping
-- Indices per the RM520N AT manual (<NR_DL_bandwidth>, QENG and QCAINFO).
-- 15 and 16 sit out of order after 400 MHz in the vendor table, which is why
-- they are easy to miss; without them a 35 or 45 MHz carrier reads "? MHz".
local NR5G_BW_INDEX = {
    [0] = 5, [1] = 10, [2] = 15, [3] = 20, [4] = 25, [5] = 30,
    [6] = 40, [7] = 50, [8] = 60, [9] = 70, [10] = 80, [11] = 90,
    [12] = 100, [13] = 200, [14] = 400, [15] = 35, [16] = 45
}

--- Get LTE bandwidth in MHz from index or RB count
-- @param value Bandwidth index (from QENG) or RB count (from QCAINFO)
-- @param from_qcainfo True if value is RB count
-- @return Bandwidth in MHz, or nil if unknown
function M.lte_bandwidth_mhz(value, from_qcainfo)
    if from_qcainfo then
        return LTE_BW_RB[value]
    else
        return LTE_BW_INDEX[value]
    end
end

--- Get NR5G bandwidth in MHz from index
-- @param index Bandwidth index from modem
-- @return Bandwidth in MHz, or nil if unknown
function M.nr5g_bandwidth_mhz(index)
    return NR5G_BW_INDEX[index]
end

--- Format bandwidth as string
-- @param bw_mhz Bandwidth in MHz
-- @return String like "20 MHz" or "? MHz" if nil
function M.format_bandwidth(bw_mhz)
    if bw_mhz then
        return string.format("%.0f MHz", bw_mhz)
    else
        return "? MHz"
    end
end

return M
