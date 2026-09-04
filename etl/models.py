"""Pydantic models for trip.json, matching docs/SCHEMA.md section 6.

Stage 1 leaves location-resolution and network-derived fields (lat/lng/place_id,
sunrise/sunset, legs, documents, checklist) null/empty — that's stage 2.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Document(BaseModel):
    id: str
    type: str
    title: str
    url: str


class Lodging(BaseModel):
    name: str
    check_in: str | None = None
    notes: str | None = None


class Leg(BaseModel):
    from_: str = Field(alias="from")
    to: str
    how: str
    distance_m: int | None = None
    duration_s: int | None = None
    polyline: str | None = None

    model_config = {"populate_by_name": True}


class Stop(BaseModel):
    id: str
    seq: int
    title: str
    kind: str
    lat: float | None = None
    lng: float | None = None
    place_id: str | None = None
    address_source: str | None = None
    timezone: str
    day_offset: int = 0
    how: str | None = None
    travel_minutes: int
    dwell_minutes: int
    timing: str
    fixed_time: str | None = None
    arrive_before: int | None = None
    daylight_required: bool = False
    notes: str | None = None
    price: str | None = None
    links: list[str] = Field(default_factory=list)
    documents: list[str] | None = None


class Day(BaseModel):
    day: int
    date: str
    leg: str | None = None
    start_location: str | None = None
    end_location: str | None = None
    timezone: str
    anchor_time: str | None = None
    sunrise: str | None = None
    sunset: str | None = None
    lodging: Lodging | None = None
    checklist: list[str] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    stops: list[Stop] = Field(default_factory=list)
    legs: list[Leg] = Field(default_factory=list)


class TripMeta(BaseModel):
    title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    generated_at: str | None = None
    source_revision: str | None = None


class Trip(BaseModel):
    trip: TripMeta
    days: list[Day]
