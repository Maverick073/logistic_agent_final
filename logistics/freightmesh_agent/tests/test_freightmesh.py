"""Regression tests for the FreightMesh scenario and Google integrations."""

import json
import unittest
from unittest.mock import Mock, patch

from freightmesh_agent.optimizer import optimize_world
from freightmesh_agent.simulation import primary_demo_world
from freightmesh_agent.tools import (
    calculate_route,
    get_amazon_requirements,
    publish_logistics_event,
    validate_consolidation,
)


class FreightMeshOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.world = primary_demo_world()

    def test_cost_objective_consolidates_and_rejects_insufficient_capacity(self):
        plan, rejections = optimize_world(self.world)
        allocations = {allocation.shipment_id: allocation for allocation in plan.allocations}

        self.assertEqual(plan.baseline_cost, 32_400)
        self.assertEqual(plan.cost, 21_700)
        self.assertEqual(plan.savings, 10_700)
        self.assertEqual(allocations["ZEP-001"].truck_id, "BLK-002")
        self.assertEqual(allocations["AMZ-001"].truck_id, "BLK-002")
        self.assertTrue(any("BLK-001" in item and "capacity exceeded" in item for item in rejections))

    def test_deadline_what_if_preserves_sla_by_splitting_plan(self):
        plan, rejections = optimize_world(self.world, amazon_deadline="19:00")
        allocations = {allocation.shipment_id: allocation for allocation in plan.allocations}

        self.assertIsNone(allocations["AMZ-001"].truck_id)
        self.assertEqual(allocations["AMZ-001"].eta, "18:45")
        self.assertIsNotNone(allocations["ZEP-001"].truck_id)
        self.assertTrue(any("AMZ-001" in item and "SLA missed" in item for item in rejections))
        self.assertEqual(self.world.shipment("AMZ-001").deadline, "21:00")

    def test_carbon_objective_selects_lower_emission_feasible_truck(self):
        plan, _ = optimize_world(self.world, objective="carbon")

        self.assertEqual(plan.truck_ids, ("BLK-003",))
        self.assertEqual(plan.cost, 24_800)
        self.assertEqual(plan.emissions_kg, 360)

    def test_unknown_objective_is_rejected(self):
        with self.assertRaises(ValueError):
            optimize_world(self.world, objective="fastest")

    def test_maps_route_is_explicitly_marked_as_demo_when_no_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = calculate_route("Hyderabad", "Chennai")

        self.assertEqual(result["provider"], "simulated demo data")
        self.assertIn("configure GOOGLE_MAPS_API_KEY", result["note"])

    def test_maps_route_uses_routes_api_when_configured(self):
        response = Mock()
        response.json.return_value = {
            "routes": [{"distanceMeters": 700_000, "duration": "28800s"}],
        }
        with (
            patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "demo-key"}, clear=True),
            patch("requests.post", return_value=response) as post,
        ):
            result = calculate_route("Hyderabad", "Chennai")

        self.assertEqual(result["provider"], "Google Maps Routes API")
        self.assertEqual(result["duration"], "28800s")
        self.assertEqual(post.call_args.kwargs["headers"]["X-Goog-Api-Key"], "demo-key")

    def test_deadline_override_reaches_operator_and_compliance_tools(self):
        amazon = get_amazon_requirements(amazon_deadline="19:00")
        compliance = validate_consolidation("BLK-002", ["AMZ-001"], amazon_deadline="19:00")

        self.assertEqual(amazon["shipments"][0]["deadline"], "19:00")
        self.assertFalse(compliance["feasible"])
        self.assertIn("SLA missed", compliance["checks"][-1]["result"])

    def test_pubsub_configuration_errors_are_explicit(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GOOGLE_CLOUD_PROJECT"):
                publish_logistics_event("NEW_SHIPMENT", "ZEP-009")

    def test_pubsub_event_is_serialized_and_published(self):
        publisher = Mock()
        publisher.topic_path.return_value = "projects/demo/topics/events"
        publisher.publish.return_value.result.return_value = "message-1"
        with (
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_CLOUD_PROJECT": "demo",
                    "FREIGHTMESH_PUBSUB_TOPIC": "events",
                },
                clear=True,
            ),
            patch("google.cloud.pubsub_v1.PublisherClient", return_value=publisher),
        ):
            result = publish_logistics_event(
                "DEADLINE_CHANGED",
                "AMZ-001",
                deadline="19:00",
            )

        self.assertTrue(result["published"])
        payload = json.loads(publisher.publish.call_args.args[1].decode("utf-8"))
        self.assertEqual(payload["event"], "DEADLINE_CHANGED")
        self.assertEqual(payload["deadline"], "19:00")


if __name__ == "__main__":
    unittest.main()
