from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """DateTime type that ensures UTC timezone is attached when reading/writing in SQLite."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )


class Installation(Base):
    __tablename__ = "installations"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    vrm_site_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    # Loss model and voltage parameters
    nominal_voltage_v: Mapped[float] = mapped_column(Float, default=220.0, nullable=False)
    avr_idle_loss_kwh: Mapped[float] = mapped_column(Float, default=0.015, nullable=False)  # kWh/h idle
    loss_base_factor: Mapped[float] = mapped_column(Float, default=0.03, nullable=False)    # 3% baseline
    loss_voltage_factor: Mapped[float] = mapped_column(Float, default=0.05, nullable=False) # voltage sensitivity

    # Tariff schedule stored as list of intervals in local time:
    # [{"zone": "NIGHT", "start": "23:00", "end": "07:00"}, {"zone": "DAY", "start": "07:00", "end": "23:00"}]
    tariff_schedule: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON,
        default=lambda: [
            {"zone": "NIGHT", "start": "23:00", "end": "07:00"},
            {"zone": "DAY", "start": "07:00", "end": "23:00"},
        ],
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )

    # Relationships
    electricity_prices: Mapped[List["ElectricityPrice"]] = relationship(
        "ElectricityPrice", back_populates="installation", cascade="all, delete-orphan", order_by="ElectricityPrice.effective_from.desc()"
    )
    meter_readings: Mapped[List["MeterReading"]] = relationship(
        "MeterReading", back_populates="installation", cascade="all, delete-orphan", order_by="MeterReading.timestamp.desc()"
    )
    hourly_consumption: Mapped[List["HourlyConsumption"]] = relationship(
        "HourlyConsumption", back_populates="installation", cascade="all, delete-orphan"
    )
    telemetry_parameters: Mapped[List["TelemetryParameter"]] = relationship(
        "TelemetryParameter", back_populates="installation", cascade="all, delete-orphan"
    )
    sync_histories: Mapped[List["SyncHistory"]] = relationship(
        "SyncHistory", back_populates="installation", cascade="all, delete-orphan"
    )


class ElectricityPrice(Base):
    __tablename__ = "electricity_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("installations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    day_price_per_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    night_price_per_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    # Optional dictionary for extensible multi-zone pricing: {"DAY": 0.25, "NIGHT": 0.12, "PEAK": 0.35}
    zone_prices: Mapped[Optional[Dict[str, float]]] = mapped_column(JSON, nullable=True)

    standing_charge_daily: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="EUR", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    installation: Mapped["Installation"] = relationship("Installation", back_populates="electricity_prices")

    __table_args__ = (
        UniqueConstraint("installation_id", "effective_from", name="uq_inst_effective_price"),
    )


class MeterReading(Base):
    __tablename__ = "meter_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("installations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)

    day_reading_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    night_reading_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    zone_readings: Mapped[Optional[Dict[str, float]]] = mapped_column(JSON, nullable=True)

    is_reference: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    installation: Mapped["Installation"] = relationship("Installation", back_populates="meter_readings")


class HourlyConsumption(Base):
    __tablename__ = "hourly_consumption"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("installations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Start of the hour interval in UTC
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)

    raw_grid_import_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    raw_grid_export_kwh: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Grid voltage applied in loss model (actual or fallback to nominal)
    grid_voltage_v: Mapped[float] = mapped_column(Float, nullable=False)
    is_voltage_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    loss_coefficient: Mapped[float] = mapped_column(Float, nullable=False)
    corrected_grid_import_kwh: Mapped[float] = mapped_column(Float, nullable=False)

    tariff_zone: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "DAY", "NIGHT"
    day_kwh: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    night_kwh: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Key-value snapshot of all telemetry known for this hour
    telemetry_snapshot: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )

    installation: Mapped["Installation"] = relationship("Installation", back_populates="hourly_consumption")

    __table_args__ = (
        UniqueConstraint("installation_id", "timestamp", name="uq_inst_hour_consumption"),
    )


class TelemetryParameter(Base):
    __tablename__ = "telemetry_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("installations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="external_sensor", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    installation: Mapped["Installation"] = relationship("Installation", back_populates="telemetry_parameters")

    __table_args__ = (
        UniqueConstraint("installation_id", "timestamp", "metric_name", name="uq_inst_ts_metric"),
    )


class SyncHistory(Base):
    __tablename__ = "sync_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("installations.id", ondelete="CASCADE"), index=True, nullable=True
    )
    sync_started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    sync_completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", nullable=False)  # RUNNING, SUCCESS, PARTIAL, FAILED
    records_synced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    installation: Mapped[Optional["Installation"]] = relationship("Installation", back_populates="sync_histories")
