from __future__ import annotations

from dataclasses import dataclass, replace

from freightmesh_agent.models import Shipment, Truck


@dataclass(frozen=True)
class LogisticsWorld:
    shipments: tuple[Shipment, ...]
    trucks: tuple[Truck, ...]

    def shipment(self, shipment_id: str) -> Shipment:
        for shipment in self.shipments:
            if shipment.shipment_id == shipment_id:
                return shipment
        raise KeyError(f"Unknown shipment: {shipment_id}")

    def with_deadline(self, shipment_id: str, deadline: str) -> LogisticsWorld:
        shipments = tuple(
            replace(shipment, deadline=deadline) if shipment.shipment_id == shipment_id else shipment
            for shipment in self.shipments
        )
        self.shipment(shipment_id)
        return replace(self, shipments=shipments)


def primary_demo_world() -> LogisticsWorld:
    return LogisticsWorld(
        shipments=(
            Shipment(
                shipment_id="ZEP-001",
                company="Zepto",
                origin="Hyderabad",
                destination="Chennai",
                weight_tons=3,
                deadline="22:00",
                cargo_type="ELECTRONICS",
                dedicated_cost=15_000,
                dedicated_eta="18:45",
                dedicated_emissions_kg=480,
            ),
            Shipment(
                shipment_id="AMZ-001",
                company="Amazon",
                origin="Hyderabad",
                destination="Chennai",
                weight_tons=2,
                deadline="21:00",
                cargo_type="GENERAL",
                dedicated_cost=17_400,
                dedicated_eta="18:45",
                dedicated_emissions_kg=440,
            ),
        ),
        trucks=(
            Truck(
                truck_id="BLK-001",
                company="Blinkit",
                route=("Mumbai", "Hyderabad", "Chennai"),
                capacity_tons=10,
                current_load_tons=6,
                eta="19:40",
                shared_single_cost=11_200,
                shared_pair_cost=21_700,
                shared_single_emissions_kg=350,
                shared_pair_emissions_kg=610,
                empty_km_saved=0,
                operational_buffer_tons=0,
            ),
            Truck(
                truck_id="BLK-002",
                company="Blinkit",
                route=("Hyderabad", "Chennai"),
                capacity_tons=8,
                current_load_tons=2,
                eta="20:15",
                shared_single_cost=13_000,
                shared_pair_cost=21_700,
                shared_single_emissions_kg=370,
                shared_pair_emissions_kg=610,
                empty_km_saved=222,
                operational_buffer_tons=0,
            ),
            Truck(
                truck_id="BLK-003",
                company="Blinkit",
                route=("Hyderabad", "Chennai"),
                capacity_tons=8,
                current_load_tons=1,
                eta="20:00",
                shared_single_cost=14_500,
                shared_pair_cost=24_800,
                shared_single_emissions_kg=220,
                shared_pair_emissions_kg=360,
                empty_km_saved=250,
                operational_buffer_tons=0,
            ),
        ),
    )
