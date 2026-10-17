from __future__ import annotations

import os

from freightmesh_agent.simulation import primary_demo_world


def seed_firestore() -> None:
    from google.cloud import firestore

    client = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
    world = primary_demo_world()
    batch = client.batch()
    for shipment in world.shipments:
        batch.set(
            client.collection("shipments").document(shipment.shipment_id),
            {
                "company": shipment.company,
                "origin": shipment.origin,
                "destination": shipment.destination,
                "weight_tons": shipment.weight_tons,
                "deadline": shipment.deadline,
                "cargo_type": shipment.cargo_type,
                "dedicated_cost": shipment.dedicated_cost,
                "dedicated_eta": shipment.dedicated_eta,
                "dedicated_emissions_kg": shipment.dedicated_emissions_kg,
            },
        )
    for truck in world.trucks:
        batch.set(
            client.collection("trucks").document(truck.truck_id),
            {
                "company": truck.company,
                "route": list(truck.route),
                "capacity_tons": truck.capacity_tons,
                "current_load_tons": truck.current_load_tons,
                "eta": truck.eta,
                "shared_single_cost": truck.shared_single_cost,
                "shared_pair_cost": truck.shared_pair_cost,
                "shared_single_emissions_kg": truck.shared_single_emissions_kg,
                "shared_pair_emissions_kg": truck.shared_pair_emissions_kg,
                "empty_km_saved": truck.empty_km_saved,
                "shareable": truck.shareable,
                "operational_buffer_tons": truck.operational_buffer_tons,
            },
        )
    batch.set(client.collection("companies").document("blinkit"), {"name": "Blinkit"})
    batch.set(client.collection("companies").document("zepto"), {"name": "Zepto"})
    batch.set(client.collection("companies").document("amazon"), {"name": "Amazon"})
    batch.commit()
    print("Seeded the FreightMesh demo companies, trucks, and shipments to Firestore.")


if __name__ == "__main__":
    seed_firestore()
