import os
import unittest


class TransactionState:
    DRAFT = "DRAFT"
    PENDING_SYNC = "PENDING_SYNC"
    CONFIRMED = "CONFIRMED"
    SYNC_REJECTED = "SYNC_REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REVERSED = "REVERSED"
    CANCELLED = "CANCELLED"


class IllegalStateTransitionError(Exception):
    pass


class TransactionStateMachine:
    """Reference state machine implementing ADR-0003 transaction lifecycle rules."""

    ALLOWED_TRANSITIONS = {
        TransactionState.DRAFT: {TransactionState.PENDING_SYNC, TransactionState.CONFIRMED, TransactionState.CANCELLED},
        TransactionState.PENDING_SYNC: {TransactionState.CONFIRMED, TransactionState.SYNC_REJECTED},
        TransactionState.CONFIRMED: {TransactionState.REVERSED},
        TransactionState.SYNC_REJECTED: {TransactionState.NEEDS_REVIEW},
        TransactionState.NEEDS_REVIEW: {TransactionState.REVERSED, TransactionState.PENDING_SYNC},
        TransactionState.REVERSED: set(),
        TransactionState.CANCELLED: set(),
    }

    def __init__(self, initial_state=TransactionState.DRAFT):
        self.state = initial_state

    def transition_to(self, new_state):
        allowed = self.ALLOWED_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise IllegalStateTransitionError(f"Illegal state transition from {self.state} to {new_state}")
        self.state = new_state
        return self.state


class TestP003CloudAuthorityContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0003-cloud-authority-and-sync-states.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0003 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

    def test_adr0003_file_exists_and_substantial(self):
        """ADR-0003 must exist and contain substantial specification."""
        self.assertGreater(len(self.content), 5000, "ADR-0003 is too brief.")

    def test_tripartite_authority_roles_defined(self):
        """Must define Cloud PostgreSQL as canonical, JavaFX SQLite as offline copy, and Flutter as cloud-first client."""
        self.assertIn("Canonical Source of Truth", self.content)
        self.assertIn("Managed PostgreSQL", self.content)
        self.assertIn("Offline Working Copy", self.content)
        self.assertIn("SQLCipher", self.content)
        self.assertIn("Flutter Mobile", self.content)
        self.assertIn("Cloud-First Reporting Client", self.content)

    def test_all_eight_architectural_invariants_present(self):
        """All eight architectural invariants (INV-01 to INV-08) must be explicitly listed."""
        for i in range(1, 9):
            inv_code = f"INV-0{i}"
            self.assertIn(inv_code, self.content, f"Invariant {inv_code} missing from ADR-0003.")

    def test_all_transaction_states_defined(self):
        """Transaction states DRAFT, PENDING_SYNC, CONFIRMED, SYNC_REJECTED, REVERSED must be defined."""
        states = ["DRAFT", "PENDING_SYNC", "CONFIRMED", "SYNC_REJECTED", "REVERSED"]
        for s in states:
            self.assertIn(f"`{s}`", self.content, f"State `{s}` missing from ADR-0003.")

    def test_network_drop_and_reconnect_sequences_present(self):
        """All 4 sequence diagrams must be present in ADR-0003 with mermaid notation."""
        self.assertIn("Sequence 1: Normal Online Checkout", self.content)
        self.assertIn("Sequence 2: Network Drop During Checkout", self.content)
        self.assertIn("Sequence 3: Reconnection, Durable Outbox Push & Canonical ACK", self.content)
        self.assertIn("Sequence 4: Sync Rejection & Exception Handling", self.content)
        self.assertIn("sequenceDiagram", self.content)

    def test_offline_thermal_receipt_watermark_specified(self):
        """Must require offline thermal receipt watermark banner."""
        self.assertIn("অফলাইন ইনভয়েস • ক্লাউড সিঙ্ক পেন্ডিং", self.content)
        self.assertIn("OFFLINE RECEIPT • PENDING CLOUD SYNC", self.content)

    def test_state_machine_valid_transitions(self):
        """State machine must follow valid business paths."""
        # Path 1: Offline sale -> Sync -> Confirmed
        sm = TransactionStateMachine(TransactionState.DRAFT)
        sm.transition_to(TransactionState.PENDING_SYNC)
        self.assertEqual(sm.state, TransactionState.PENDING_SYNC)
        sm.transition_to(TransactionState.CONFIRMED)
        self.assertEqual(sm.state, TransactionState.CONFIRMED)

        # Path 2: Confirmed sale -> Return/Reversed
        sm.transition_to(TransactionState.REVERSED)
        self.assertEqual(sm.state, TransactionState.REVERSED)

        # Path 3: Direct online confirmation
        sm_online = TransactionStateMachine(TransactionState.DRAFT)
        sm_online.transition_to(TransactionState.CONFIRMED)
        self.assertEqual(sm_online.state, TransactionState.CONFIRMED)

        # Path 4: Offline sale -> Rejection -> Review
        sm_rejected = TransactionStateMachine(TransactionState.DRAFT)
        sm_rejected.transition_to(TransactionState.PENDING_SYNC)
        sm_rejected.transition_to(TransactionState.SYNC_REJECTED)
        sm_rejected.transition_to(TransactionState.NEEDS_REVIEW)
        self.assertEqual(sm_rejected.state, TransactionState.NEEDS_REVIEW)

    def test_state_machine_blocks_illegal_transitions(self):
        """State machine must block illegal transitions and deletions."""
        # Illegal 1: Cannot jump from CONFIRMED back to DRAFT
        sm = TransactionStateMachine(TransactionState.CONFIRMED)
        with self.assertRaises(IllegalStateTransitionError):
            sm.transition_to(TransactionState.DRAFT)

        # Illegal 2: Cannot jump from CONFIRMED back to PENDING_SYNC
        with self.assertRaises(IllegalStateTransitionError):
            sm.transition_to(TransactionState.PENDING_SYNC)

        # Illegal 3: Cannot jump from REVERSED to CONFIRMED
        sm_rev = TransactionStateMachine(TransactionState.REVERSED)
        with self.assertRaises(IllegalStateTransitionError):
            sm_rev.transition_to(TransactionState.CONFIRMED)

        # Illegal 4: Cannot jump from CANCELLED to CONFIRMED
        sm_can = TransactionStateMachine(TransactionState.CANCELLED)
        with self.assertRaises(IllegalStateTransitionError):
            sm_can.transition_to(TransactionState.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
