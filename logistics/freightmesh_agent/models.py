"""Typed scenario, truck, shipment, and plan values used by FreightMesh agents."""

from __future__ import annotations

from dataclasses import dataclass


def minutes_after_midnight(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError(f"Invalid time: {value}")
    return hour * 60 + minute


@dataclass(frozen=True)
class Shipment:
    shipment_id: str
    company: str
    origin: str
    destination: str
    weight_tons: float
    deadline: str
    cargo_type: str
    dedicated_cost: int
    dedicated_eta: str
    dedicated_emissions_kg: float


@dataclass(frozen=True)
class Truck:
    truck_id: str
    company: str
    route: tuple[str, ...]
    capacity_tons: float
    current_load_tons: float
    eta: str
    shared_single_cost: int
    shared_pair_cost: int
    shared_single_emissions_kg: float
    shared_pair_emissions_kg: float
    empty_km_saved: float
    shareable: bool = True
    operational_buffer_tons: float = 0.0

    @property
    def available_capacity_tons(self) -> float:
        return self.capacity_tons - self.current_load_tons

    @property
    def usable_capacity_tons(self) -> float:
        return max(0.0, self.available_capacity_tons - self.operational_buffer_tons)

    def shared_cost(self, shipment_count: int) -> int:
        if shipment_count <= 0:
            return 0
        if shipment_count == 1:
            return self.shared_single_cost
        return self.shared_pair_cost + (shipment_count - 2) * self.shared_single_cost

    def shared_emissions(self, shipment_count: int) -> float:
        if shipment_count <= 0:
            return 0.0
        if shipment_count == 1:
            return self.shared_single_emissions_kg
        return self.shared_pair_emissions_kg + (shipment_count - 2) * self.shared_single_emissions_kg


@dataclass(frozen=True)
class ObjectiveWeights:
    cost: float = 0.30
    sla: float = 0.30
    utilization: float = 0.15
    empty_km: float = 0.15
    carbon: float = 0.10

    @classmethod
    def carbon_first(cls) -> ObjectiveWeights:
        return cls(cost=0.15, sla=0.25, utilization=0.20, empty_km=0.20, carbon=0.20)


@dataclass(frozen=True)
class Allocation:
    shipment_id: str
    truck_id: str | None
    eta: str
    cost: int
    emissions_kg: float


@dataclass(frozen=True)
class Plan:
    allocations: tuple[Allocation, ...]
    cost: int
    baseline_cost: int
    emissions_kg: float
    baseline_emissions_kg: float
    empty_km_saved: float
    score: float
    objective: str

    @property
    def savings(self) -> int:
        return self.baseline_cost - self.cost

    @property
    def shared_allocations(self) -> tuple[Allocation, ...]:
        return tuple(allocation for allocation in self.allocations if allocation.truck_id is not None)

    @property
    def truck_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            allocation.truck_id
            for allocation in self.allocations
            if allocation.truck_id is not None
        ))
