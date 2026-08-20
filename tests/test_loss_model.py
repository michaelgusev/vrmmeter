import pytest
from vrmmeter.services.loss_model import calculate_hourly_loss


def test_loss_calculation_zero_load_avr_idle():
    """Verify AVR idle consumption is added even when raw consumption is 0."""
    res = calculate_hourly_loss(
        raw_kwh=0.0,
        grid_voltage_v=220.0,
        nominal_voltage_v=220.0,
        avr_idle_loss_kwh=0.015,
        loss_base_factor=0.03,
        loss_voltage_factor=0.05,
    )
    # Expected: (0 + 0.015) * (1 + 0.03 + 0) = 0.015 * 1.03 = 0.01545 -> rounded 0.0155
    assert res.loss_coefficient == 1.03
    assert res.corrected_kwh == pytest.approx(0.0155, abs=0.0001)
    assert not res.is_voltage_fallback


def test_loss_calculation_with_voltage_deviation():
    """Verify voltage deviation penalty."""
    # 242V on 220V nominal: deviation is 22V / 220V = 0.10 (10%)
    # Loss coef: 1 + 0.03 + 0.05 * 0.10 = 1.035
    # (1.0 + 0.015) * 1.035 = 1.015 * 1.035 = 1.050525 -> rounded 1.0505
    res = calculate_hourly_loss(
        raw_kwh=1.0,
        grid_voltage_v=242.0,
        nominal_voltage_v=220.0,
        avr_idle_loss_kwh=0.015,
        loss_base_factor=0.03,
        loss_voltage_factor=0.05,
    )
    assert res.loss_coefficient == 1.035
    assert res.corrected_kwh == pytest.approx(1.0505, abs=0.0001)
    assert res.effective_voltage == 242.0
    assert not res.is_voltage_fallback


def test_loss_calculation_missing_voltage_fallback():
    """Verify missing voltage falls back gracefully to nominal voltage."""
    res = calculate_hourly_loss(
        raw_kwh=2.0,
        grid_voltage_v=None,
        nominal_voltage_v=220.0,
        avr_idle_loss_kwh=0.015,
        loss_base_factor=0.03,
        loss_voltage_factor=0.05,
    )
    # Voltage deviation is 0
    assert res.effective_voltage == 220.0
    assert res.is_voltage_fallback is True
    assert res.loss_coefficient == 1.03
    # (2.0 + 0.015) * 1.03 = 2.015 * 1.03 = 2.07545 -> 2.0755
    assert res.corrected_kwh == pytest.approx(2.0755, abs=0.0001)


def test_loss_calculation_configurable_nominal_voltage():
    """Verify support for different nominal voltages, e.g. 230V."""
    res = calculate_hourly_loss(
        raw_kwh=1.5,
        grid_voltage_v=230.0,
        nominal_voltage_v=230.0,
        avr_idle_loss_kwh=0.02,
        loss_base_factor=0.04,
        loss_voltage_factor=0.05,
    )
    assert res.effective_voltage == 230.0
    assert res.loss_coefficient == 1.04
    # (1.5 + 0.02) * 1.04 = 1.52 * 1.04 = 1.5808
    assert res.corrected_kwh == pytest.approx(1.5808, abs=0.0001)
