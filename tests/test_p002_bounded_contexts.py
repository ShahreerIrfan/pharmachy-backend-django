import os
import unittest
from collections import defaultdict, deque


class TestP002BoundedContextsArchitecture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0002-bounded-contexts-and-service-ownership.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0002 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

        # The 11 canonical bounded contexts defined in SRS v2 and P002
        cls.expected_contexts = [
            "apps.platform",
            "apps.identity",
            "apps.catalogue",
            "apps.inventory",
            "apps.dispensing",
            "apps.procurement",
            "apps.pos",
            "apps.accounting",
            "apps.compliance",
            "apps.sync",
            "apps.mobile_gateway",
        ]

        # Explicit architectural dependency definition (Layered DAG)
        # An edge from A -> B means A depends on B (A imports B)
        cls.dependency_graph = {
            "apps.platform": [],
            "apps.identity": ["apps.platform"],
            "apps.catalogue": ["apps.identity", "apps.platform"],
            "apps.inventory": ["apps.catalogue", "apps.identity", "apps.platform"],
            "apps.accounting": ["apps.identity", "apps.platform"],
            "apps.dispensing": ["apps.catalogue", "apps.identity", "apps.platform"],
            "apps.procurement": [
                "apps.inventory",
                "apps.accounting",
                "apps.catalogue",
                "apps.identity",
                "apps.platform",
            ],
            "apps.pos": [
                "apps.dispensing",
                "apps.inventory",
                "apps.accounting",
                "apps.catalogue",
                "apps.identity",
                "apps.platform",
            ],
            "apps.compliance": ["apps.inventory", "apps.dispensing", "apps.identity", "apps.platform"],
            "apps.sync": [
                "apps.pos",
                "apps.dispensing",
                "apps.inventory",
                "apps.procurement",
                "apps.identity",
                "apps.platform",
            ],
            "apps.mobile_gateway": [
                "apps.pos",
                "apps.accounting",
                "apps.inventory",
                "apps.compliance",
                "apps.identity",
                "apps.platform",
            ],
        }

        cls.layer_map = {
            "apps.platform": 0,
            "apps.identity": 1,
            "apps.catalogue": 2,
            "apps.accounting": 3,
            "apps.inventory": 3,
            "apps.dispensing": 4,
            "apps.compliance": 5,
            "apps.procurement": 6,
            "apps.pos": 6,
            "apps.sync": 7,
            "apps.mobile_gateway": 7,
        }

    def test_all_eleven_bounded_contexts_documented(self):
        """ADR-0002 must document all 11 bounded contexts in detail."""
        for ctx in self.expected_contexts:
            self.assertIn(ctx, self.content, f"Bounded context '{ctx}' is missing in ADR-0002.")

    def test_modular_monolith_architecture_choice(self):
        """ADR-0002 must explicitly choose a modular monolith in Django."""
        self.assertIn("Monolith-First Modular Architecture", self.content)
        self.assertIn("apps/", self.content)

    def test_service_ownership_matrix_prevents_duplicate_logic(self):
        """ADR-0002 must contain the Service Ownership Matrix to prevent duplicate logic."""
        self.assertIn("## 3. Service Ownership Matrix", self.content)
        core_capabilities = [
            "Tenant & Plan Entitlements",
            "Authentication & RBAC",
            "Medicine Scientific Data & Unit Ratios",
            "Batch Allocation & Stock Deductions",
            "Rx Validity & Antibiotic Logging",
            "Order Totaling & Cash Drawer Shifts",
            "General Ledger & Financial Journaling",
            "Offline Idempotency & Conflict Check",
        ]
        for cap in core_capabilities:
            self.assertIn(cap, self.content, f"Capability '{cap}' not found in Service Ownership Matrix.")

    def test_dependency_graph_is_strictly_acyclic_dag(self):
        """Mathematical proof using Kahn's algorithm that the module dependency graph has ZERO cycles."""
        in_degree = defaultdict(int)
        adj = defaultdict(list)
        all_nodes = list(self.dependency_graph.keys())

        for u in all_nodes:
            in_degree[u] = 0

        for u, deps in self.dependency_graph.items():
            for v in deps:
                adj[v].append(u)
                in_degree[u] += 1

        queue = deque([u for u in all_nodes if in_degree[u] == 0])
        visited_count = 0
        topological_order = []

        while queue:
            node = queue.popleft()
            topological_order.append(node)
            visited_count += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        self.assertEqual(
            visited_count,
            len(all_nodes),
            f"Circular dependency detected! Visited {visited_count} of {len(all_nodes)} nodes.",
        )

    def test_layer_invariants_respected(self):
        """An app in layer N can only depend on apps in layer < N."""
        for app, deps in self.dependency_graph.items():
            app_layer = self.layer_map[app]
            for dep in deps:
                dep_layer = self.layer_map[dep]
                self.assertLess(
                    dep_layer,
                    app_layer,
                    f"Layer invariant violation: {app} (Layer {app_layer}) depends on {dep} (Layer {dep_layer})!",
                )


if __name__ == "__main__":
    unittest.main()
