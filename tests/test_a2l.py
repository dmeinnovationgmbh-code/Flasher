"""ASAP2 / A2L parser tests: block structure, conversions and signal export."""

from __future__ import annotations

import pytest

from med17flasher.a2l import (
    A2lCharacteristic,
    A2lMeasurement,
    load_a2l,
    parse_a2l,
)
from med17flasher.exceptions import FirmwareError
from med17flasher.xcp import Signal

# A small but nasty A2L: nested containers, both comment styles, quoted strings
# with spaces and escaped quotes, an IF_DATA that repeats a keyword we look for,
# and one of every conversion shape.
SAMPLE = r"""
/* ------------------------------------------------------------------
   A demo description file.  This comment contains /begin MODULE and a
   stray " quote, neither of which may confuse the tokenizer.
   ------------------------------------------------------------------ */
ASAP2_VERSION 1 61          // line comment after real content

/begin PROJECT DEMO_PRJ "MED17 demo project"
  /begin MODULE DEMO_MOD "the ECU \"main\" module"

    /begin MOD_COMMON "common settings"
      BYTE_ORDER MSB_LAST
      ALIGNMENT_LONG 4
    /end MOD_COMMON

    /begin MEASUREMENT nmot "engine speed \"nmot\" in rpm"
      UWORD CM_RPM 0 0 0 8000
      ECU_ADDRESS 0x80005000
      /begin IF_DATA XCP
        ECU_ADDRESS 0xDEADBEEF      /* belongs to IF_DATA, must be ignored */
      /end IF_DATA
    /end MEASUREMENT

    /begin MEASUREMENT tmot "coolant temperature"
      SWORD CM_TEMP 0 0 -40 215
      ECU_ADDRESS 0x80005002
    /end MEASUREMENT

    /begin MEASUREMENT ub_batt "battery voltage"
      FLOAT32_IEEE CM_IDENT 0 0 0 20
      ECU_ADDRESS 0x80005004
    /end MEASUREMENT

    /begin MEASUREMENT lam_w "lambda, quadratic conversion"
      UWORD CM_QUAD 0 0 0 2
      ECU_ADDRESS 0x80005008
    /end MEASUREMENT

    /begin MEASUREMENT odo_km "odometer, 64 bit in the ECU"
      A_UINT64 CM_IDENT 0 0 0 1e9
      ECU_ADDRESS 0x8000500C
    /end MEASUREMENT

    /begin MEASUREMENT weird_sig "vendor specific datatype"
      A_BOGUS CM_IDENT 0 0 0 1
      ECU_ADDRESS 0x80005014
    /end MEASUREMENT

    /begin MEASUREMENT virt_sig "computed, lives nowhere in memory"
      UWORD CM_IDENT 0 0 0 1
    /end MEASUREMENT

    /begin COMPU_METHOD CM_RPM "engine speed" RAT_FUNC "%6.1" "1/min"
      COEFFS 0 1 0 0 0 4            /* (1*raw + 0) / 4  ->  factor 0.25 */
    /end COMPU_METHOD

    /begin COMPU_METHOD CM_TEMP "coolant temperature" LINEAR "%6.1" "degC"
      COEFFS_LINEAR 0.1 -40
    /end COMPU_METHOD

    /begin COMPU_METHOD CM_IDENT "raw value" IDENTICAL "%6.3" "V"
    /end COMPU_METHOD

    /begin COMPU_METHOD CM_QUAD "not reducible" RAT_FUNC "%6.3" "-"
      COEFFS 1 2 3 0 0 1
    /end COMPU_METHOD

    /begin CHARACTERISTIC KFZW "ignition timing map" MAP 0x80100000
      DAMOS_KF 0 CM_IDENT 0 72
    /end CHARACTERISTIC

  /end MODULE
/end PROJECT
"""


@pytest.fixture
def a2l():
    return parse_a2l(SAMPLE)


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #
def test_project_and_module_names(a2l):
    assert a2l.project == "DEMO_PRJ"
    assert a2l.module == "DEMO_MOD"


def test_measurement_names_addresses_and_dtypes(a2l):
    assert set(a2l.measurements) == {
        "nmot", "tmot", "ub_batt", "lam_w", "odo_km", "weird_sig",
    }
    assert a2l.measurements["nmot"].address == 0x80005000
    assert a2l.measurements["nmot"].dtype == "u16"
    assert a2l.measurements["tmot"].address == 0x80005002
    assert a2l.measurements["tmot"].dtype == "s16"
    assert a2l.measurements["ub_batt"].address == 0x80005004
    assert a2l.measurements["ub_batt"].dtype == "f32"
    assert isinstance(a2l.measurements["nmot"], A2lMeasurement)


def test_nested_if_data_does_not_leak_its_ecu_address(a2l):
    # The IF_DATA block repeats ECU_ADDRESS; only the outer one counts.
    assert a2l.measurements["nmot"].address != 0xDEADBEEF


def test_quoted_string_keeps_spaces_and_escaped_quotes(a2l):
    assert a2l.measurements["nmot"].long_identifier == 'engine speed "nmot" in rpm'
    assert a2l.measurements["tmot"].long_identifier == "coolant temperature"


def test_characteristic_parsed(a2l):
    kfzw = a2l.characteristics["KFZW"]
    assert isinstance(kfzw, A2lCharacteristic)
    assert kfzw.address == 0x80100000
    assert kfzw.kind == "MAP"
    assert kfzw.long_identifier == "ignition timing map"


# --------------------------------------------------------------------------- #
# conversions
# --------------------------------------------------------------------------- #
def test_rat_func_coeffs_reduce_to_factor_and_offset(a2l):
    nmot = a2l.measurements["nmot"]
    assert nmot.factor == pytest.approx(0.25)
    assert nmot.offset == pytest.approx(0.0)
    assert nmot.unit == "1/min"
    assert nmot.nonlinear is False


def test_coeffs_linear_conversion(a2l):
    tmot = a2l.measurements["tmot"]
    assert tmot.factor == pytest.approx(0.1)
    assert tmot.offset == pytest.approx(-40.0)
    assert tmot.unit == "degC"
    assert tmot.nonlinear is False


def test_identical_conversion_keeps_raw_scaling_but_takes_the_unit(a2l):
    batt = a2l.measurements["ub_batt"]
    assert (batt.factor, batt.offset) == (1.0, 0.0)
    assert batt.unit == "V"
    assert batt.nonlinear is False


def test_nonlinear_conversion_is_flagged(a2l):
    lam = a2l.measurements["lam_w"]
    assert lam.nonlinear is True
    assert (lam.factor, lam.offset) == (1.0, 0.0)   # cannot be expressed linearly
    assert any("CM_QUAD" in w for w in a2l.warnings)


def test_compu_methods_are_exposed(a2l):
    assert a2l.compu_methods["CM_RPM"].conversion_type == "RAT_FUNC"
    assert a2l.compu_methods["CM_RPM"].coeffs == (0.0, 1.0, 0.0, 0.0, 0.0, 4.0)
    assert a2l.compu_methods["CM_TEMP"].unit == "degC"


# --------------------------------------------------------------------------- #
# warnings for the survivable oddities
# --------------------------------------------------------------------------- #
def test_unknown_datatype_defaults_to_u16_and_warns(a2l):
    assert a2l.measurements["weird_sig"].dtype == "u16"
    assert any("A_BOGUS" in w for w in a2l.warnings)


def test_uint64_is_clamped_to_u32_and_warns(a2l):
    assert a2l.measurements["odo_km"].dtype == "u32"
    assert any("A_UINT64" in w for w in a2l.warnings)


def test_measurement_without_ecu_address_is_skipped_with_a_warning(a2l):
    assert "virt_sig" not in a2l.measurements
    assert any("virt_sig" in w for w in a2l.warnings)


# --------------------------------------------------------------------------- #
# lookup + export
# --------------------------------------------------------------------------- #
def test_find_matches_substring_glob_and_ignores_case(a2l):
    assert [m.name for m in a2l.find("mot")] == ["nmot", "tmot"]
    assert [m.name for m in a2l.find("NMOT")] == ["nmot"]
    assert [m.name for m in a2l.find("*_w")] == ["lam_w"]
    assert [m.name for m in a2l.find("u?_batt")] == ["ub_batt"]
    assert a2l.find("no_such_signal") == []
    assert len(a2l.find("*")) == len(a2l.measurements)


def test_to_signals_returns_real_xcp_signals(a2l):
    signals = a2l.to_signals(["nmot", "tmot"])
    assert all(isinstance(s, Signal) for s in signals)
    rpm, coolant = signals
    assert (rpm.name, rpm.address, rpm.dtype) == ("nmot", 0x80005000, "u16")
    assert rpm.factor == pytest.approx(0.25)
    assert rpm.size == 2
    # The scaling really is the A2L's: 4000 raw counts are 1000 rpm.
    assert rpm.decode((4000).to_bytes(2, "little")) == pytest.approx(1000.0)
    assert coolant.dtype == "s16"
    assert coolant.decode((900).to_bytes(2, "little")) == pytest.approx(50.0)
    assert coolant.unit == "degC"


def test_to_signals_defaults_to_every_measurement(a2l):
    assert len(a2l.to_signals()) == len(a2l.measurements)


def test_to_signals_accepts_a_single_name_and_is_case_insensitive(a2l):
    assert [s.name for s in a2l.to_signals("NMOT")] == ["nmot"]


def test_to_signals_rejects_unknown_names(a2l):
    with pytest.raises(FirmwareError):
        a2l.to_signals(["not_in_the_file"])


# --------------------------------------------------------------------------- #
# malformed input
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "",                                             # empty
        "just some text without any blocks at all",     # not an A2L
        "/begin MEASUREMENT n \"x\" UWORD CM 0 0 0 1",  # never closed
        "/end MODULE",                                  # closed but never opened
        '/begin MODULE M "m" /end PROJECT',             # mismatched /end
        '/begin PROJECT P "unterminated string',        # dangling quote
        "/begin",                                       # truncated after /begin
    ],
)
def test_malformed_input_raises_firmware_error(text):
    with pytest.raises(FirmwareError):
        parse_a2l(text)


def test_parse_a2l_rejects_non_text():
    with pytest.raises(FirmwareError):
        parse_a2l(b"/begin PROJECT P \"x\" /end PROJECT")


# --------------------------------------------------------------------------- #
# file loading
# --------------------------------------------------------------------------- #
def test_load_a2l_reads_a_file(tmp_path):
    path = tmp_path / "demo.a2l"
    path.write_text(SAMPLE, encoding="utf-8")
    loaded = load_a2l(str(path))
    assert loaded.project == "DEMO_PRJ"
    assert loaded.measurements["nmot"].address == 0x80005000


def test_load_a2l_accepts_latin1_and_a_bom(tmp_path):
    path = tmp_path / "latin1.a2l"
    text = SAMPLE.replace("coolant temperature", "Kühlmitteltemperatur")
    path.write_bytes(text.encode("latin-1"))
    assert load_a2l(str(path)).measurements["tmot"].long_identifier == (
        "Kühlmitteltemperatur"
    )

    bom = tmp_path / "bom.a2l"
    bom.write_bytes(b"\xef\xbb\xbf" + SAMPLE.encode("utf-8"))
    assert load_a2l(str(bom)).project == "DEMO_PRJ"


def test_load_a2l_missing_file_raises_firmware_error(tmp_path):
    with pytest.raises(FirmwareError):
        load_a2l(str(tmp_path / "nope.a2l"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_a2l_lists_and_emits_signal_specs(tmp_path, capsys):
    from med17flasher import cli
    from med17flasher.xcp import parse_signal

    path = tmp_path / "demo.a2l"
    path.write_text(SAMPLE, encoding="utf-8")
    out_file = tmp_path / "signals.txt"

    rc = cli.main(["a2l", str(path), "--find", "mot",
                   "--emit-signals", str(out_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEMO_PRJ" in out and "nmot" in out
    assert "0x80005000" in out

    # The emitted specs must be exactly what `xcp --signals-file` consumes.
    specs = [line for line in out_file.read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    signals = [parse_signal(spec) for spec in specs]
    assert [s.name for s in signals] == ["nmot", "tmot"]
    assert signals[0].address == 0x80005000
    assert signals[0].factor == pytest.approx(0.25)
    assert signals[1].dtype == "s16"
    assert signals[1].offset == pytest.approx(-40.0)


def test_cli_a2l_reports_a_broken_file(tmp_path, capsys):
    from med17flasher import cli

    path = tmp_path / "broken.a2l"
    path.write_text("/begin PROJECT P \"never closed\"", encoding="utf-8")
    assert cli.main(["a2l", str(path)]) == 1
    assert "a2l failed" in capsys.readouterr().err
