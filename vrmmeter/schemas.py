from datetime import date, datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class TariffInterval(BaseModel):
    zone: str = Field(..., description="Zone name, e.g. DAY, NIGHT, PEAK")
    start: str = Field(..., description="Start time in HH:MM format (local time)")
    end: str = Field(..., description="End time in HH:MM format (local time)")


# --- System Settings Schemas ---
class SystemSettingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    key: str
    value: str
    updated_at: datetime


class VrmTokenUpdate(BaseModel):
    token: str = Field(..., description="Victron VRM API Access Token")


class VrmTestResponse(BaseModel):
    success: bool
    message: str
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    sites_count: int = 0


class VrmDiscoveryItem(BaseModel):
    site_id: str
    name: str
    timezone: str = "UTC"
    device_name: Optional[str] = None
    already_imported: bool = False


class VrmImportResponse(BaseModel):
    success: bool
    imported_count: int
    updated_count: int
    installations: List[VrmDiscoveryItem]


# --- Installation Schemas ---
class InstallationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    vrm_site_id: str = Field(..., min_length=1, max_length=64)
    timezone: str = Field(default="UTC", description="IANA timezone name, e.g. Europe/Kyiv")
    nominal_voltage_v: float = Field(default=220.0, ge=50.0, le=500.0)
    avr_idle_loss_kwh: float = Field(default=0.015, ge=0.0, description="Hourly AVR/inverter idle draw in kWh")
    loss_base_factor: float = Field(default=0.03, ge=0.0, description="Base loss percentage factor (e.g. 0.03 = 3%)")
    loss_voltage_factor: float = Field(default=0.05, ge=0.0, description="Voltage deviation sensitivity factor")
    tariff_schedule: List[TariffInterval] = Field(
        default_factory=lambda: [
            TariffInterval(zone="NIGHT", start="23:00", end="07:00"),
            TariffInterval(zone="DAY", start="07:00", end="23:00"),
        ]
    )
    currency: str = Field(default="EUR", max_length=8, description="Currency code or symbol, e.g. EUR, USD, UAH, GBP")
    is_active: bool = True
    is_demo: bool = False


class InstallationCreate(InstallationBase):
    pass


class InstallationUpdate(BaseModel):
    name: Optional[str] = None
    timezone: Optional[str] = None
    nominal_voltage_v: Optional[float] = None
    avr_idle_loss_kwh: Optional[float] = None
    loss_base_factor: Optional[float] = None
    loss_voltage_factor: Optional[float] = None
    tariff_schedule: Optional[List[TariffInterval]] = None
    currency: Optional[str] = None
    is_active: Optional[bool] = None
    is_demo: Optional[bool] = None


class InstallationOut(InstallationBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime


# --- Electricity Price Schemas ---
class ElectricityPriceBase(BaseModel):
    effective_from: date
    day_price_per_kwh: float = Field(..., ge=0.0)
    night_price_per_kwh: float = Field(..., ge=0.0)
    zone_prices: Optional[Dict[str, float]] = None
    standing_charge_daily: float = Field(default=0.0, ge=0.0)
    currency: str = Field(default="EUR", max_length=8)


class ElectricityPriceCreate(ElectricityPriceBase):
    installation_id: str
    currency: Optional[str] = Field(default=None, max_length=8)



class ElectricityPriceOut(ElectricityPriceBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    installation_id: str
    created_at: datetime


# --- Reference Meter Reading Schemas ---
class MeterReadingBase(BaseModel):
    timestamp: datetime
    day_reading_kwh: int = Field(..., ge=0)
    night_reading_kwh: int = Field(..., ge=0)
    zone_readings: Optional[Dict[str, float]] = None
    is_reference: bool = True
    notes: Optional[str] = None


class MeterReadingCreate(MeterReadingBase):
    installation_id: str


class MeterReadingOut(MeterReadingBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    installation_id: str
    created_at: datetime
    delta_day_kwh: float = 0.0
    delta_night_kwh: float = 0.0
    delta_total_kwh: float = 0.0
    delta_day_str: str = "(0)"
    delta_night_str: str = "(0)"
    delta_total_str: str = "(0)"
    expected_day_reading: Optional[float] = None
    expected_night_reading: Optional[float] = None
    expected_total_reading: Optional[float] = None


# --- Telemetry Ingestion Schemas ---
class TelemetryItem(BaseModel):
    timestamp: datetime
    metric_name: str
    metric_value: float
    source: str = "external_sensor"


class TelemetryBatchIngest(BaseModel):
    installation_id: str
    readings: List[TelemetryItem]


class TelemetryParameterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    installation_id: str
    timestamp: datetime
    metric_name: str
    metric_value: float
    source: str
    created_at: datetime


# --- Hourly & Aggregated Consumption Schemas ---
class HourlyConsumptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    installation_id: str
    timestamp: datetime
    raw_grid_import_kwh: float
    raw_grid_export_kwh: float
    grid_voltage_v: float
    is_voltage_fallback: bool
    loss_coefficient: float
    corrected_grid_import_kwh: float
    tariff_zone: str
    day_kwh: float
    night_kwh: float
    telemetry_snapshot: Optional[Dict[str, Any]] = None


class DailyConsumptionItem(BaseModel):
    date: date
    day_kwh: float
    night_kwh: float
    total_kwh: float
    raw_kwh: float
    loss_kwh: float
    avg_voltage_v: float


# --- Calculation & Reporting Schemas ---
class MeterCalculationRequest(BaseModel):
    installation_id: str
    target_time: datetime = Field(..., description="Point in time to estimate meter readings for (UTC or with timezone)")


class ReferenceReadingAudit(BaseModel):
    id: int
    timestamp: datetime
    day_reading_kwh: int
    night_reading_kwh: int
    notes: Optional[str] = None


class MeterCalculationResponse(BaseModel):
    installation_id: str
    target_time: datetime
    reference_reading: Optional[ReferenceReadingAudit] = None
    delta_day_kwh: float
    delta_night_kwh: float
    delta_total_kwh: float
    estimated_day_reading: float
    estimated_night_reading: float
    hours_calculated: int
    is_extrapolated_from_reference: bool


class TariffCostBreakdown(BaseModel):
    zone: str
    kwh: float
    rate_per_kwh: float
    cost: float


class MonthlyReportResponse(BaseModel):
    installation_id: str
    installation_name: str
    year: int
    month: int
    month_name: str
    is_incomplete_month: bool
    currency: str

    # Consumption totals
    total_kwh: float
    day_kwh: float
    night_kwh: float
    raw_kwh: float
    avr_and_system_loss_kwh: float

    # Cost breakdown
    day_rate: float
    night_rate: float
    day_cost: float
    night_cost: float
    standing_charge: float
    total_bill: float
    zone_breakdown: List[TariffCostBreakdown] = []

    # Estimated meter readings
    start_meter_day: Optional[float] = None
    start_meter_night: Optional[float] = None
    end_meter_day: Optional[float] = None
    end_meter_night: Optional[float] = None
    last_reference_reading_date: Optional[datetime] = None

    # Daily breakdown list
    daily_breakdown: List[DailyConsumptionItem] = []


# --- Sync & Backfill Schemas ---
class SyncRequest(BaseModel):
    installation_id: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    force_recalculate_losses: bool = False


class SyncResponse(BaseModel):
    status: str
    message: str
    records_synced: int
