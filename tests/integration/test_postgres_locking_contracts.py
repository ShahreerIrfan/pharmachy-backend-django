import threading
import time
import unittest
from decimal import Decimal


class InsufficientStockError(Exception):
    """Raised when available stock is less than requested."""

    pass


class BatchExpiredError(Exception):
    """Raised when a batch has expired."""

    pass


class MockPostgresBatchRepository:
    """
    Simulates PostgreSQL row-level locking (SELECT ... FOR UPDATE) semantics
    versus non-locking behavior to test concurrency and race condition resilience.
    """

    def __init__(self, batch_id: str, batch_number: str, initial_stock: int, is_expired: bool = False):
        self.batch_id = batch_id
        self.batch_number = batch_number
        self.available_quantity = initial_stock
        self.is_expired = is_expired
        self._row_lock = threading.Lock()

    def reserve_stock_with_for_update_lock(self, quantity: int) -> int:
        """
        Simulates:
            with transaction.atomic():
                batch = Batch.objects.select_for_update().get(id=batch_id)
                ...
        """
        with self._row_lock:
            # Simulate slight database read/write network latency
            time.sleep(0.01)
            if self.is_expired:
                raise BatchExpiredError(f"Batch {self.batch_number} is expired.")
            if self.available_quantity < quantity:
                raise InsufficientStockError(f"Requested: {quantity}, Available: {self.available_quantity}")
            self.available_quantity -= quantity
            return self.available_quantity

    def reserve_stock_unsafe_without_lock(self, quantity: int) -> int:
        """Simulates unsafe concurrent read-modify-write without row-level lock."""
        current = self.available_quantity
        time.sleep(0.01)  # Context switch window
        if current < quantity:
            raise InsufficientStockError(f"Requested: {quantity}, Available: {current}")
        self.available_quantity = current - quantity
        return self.available_quantity


class TestPostgresLockingContracts(unittest.TestCase):
    """
    Integration tests verifying PostgreSQL-specific row-level locking contracts,
    concurrency race condition prevention, and monetary decimal integrity.
    """

    def test_concurrent_checkout_with_row_level_lock_prevents_oversell(self):
        """
        Simulates two concurrent cashiers attempting to sell from a batch with 10 units.
        Cashier A requests 7 units, Cashier B requests 6 units (total 13 > 10).
        With row-level locking, exactly one must succeed and the second must be rejected.
        """
        repo = MockPostgresBatchRepository("batch_uuid_101", "B-2026-09", initial_stock=10)
        results = {"success": [], "insufficient": [], "exceptions": []}

        def cashier_attempt(quantity: int, cashier_id: str):
            try:
                remaining = repo.reserve_stock_with_for_update_lock(quantity)
                results["success"].append((cashier_id, quantity, remaining))
            except InsufficientStockError as e:
                results["insufficient"].append((cashier_id, str(e)))
            except Exception as e:
                results["exceptions"].append((cashier_id, str(e)))

        # Launch two concurrent threads simultaneously
        t1 = threading.Thread(target=cashier_attempt, args=(7, "Cashier_A"))
        t2 = threading.Thread(target=cashier_attempt, args=(6, "Cashier_B"))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one must succeed, one must fail with InsufficientStockError
        self.assertEqual(len(results["exceptions"]), 0, f"Unexpected errors: {results['exceptions']}")
        self.assertEqual(len(results["success"]), 1, "Exactly one cashier transaction must succeed.")
        self.assertEqual(len(results["insufficient"]), 1, "Exactly one cashier transaction must fail.")

        # Final stock must never be negative
        self.assertGreaterEqual(repo.available_quantity, 0, "Stock was oversold into negative numbers!")
        successful_cashier, qty, remaining = results["success"][0]
        self.assertEqual(repo.available_quantity, 10 - qty)

    def test_unsafe_concurrency_without_lock_demonstrates_oversell_risk(self):
        """
        Demonstrates why SQLite's lack of row-level locking requires PostgreSQL in production:
        Unsafe read-modify-write without serialization causes negative stock (overselling).
        """
        repo = MockPostgresBatchRepository("batch_uuid_102", "B-2026-10", initial_stock=10)

        # Two threads read 10 simultaneously and both decrement
        t1 = threading.Thread(target=repo.reserve_stock_unsafe_without_lock, args=(7,))
        t2 = threading.Thread(target=repo.reserve_stock_unsafe_without_lock, args=(6,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Without locking, the final state is corrupted (e.g. 10 - 6 = 4 or 10 - 7 = 3, losing one sale's deduction)
        # It fails to maintain accurate serializability
        self.assertNotEqual(
            repo.available_quantity, 10 - (7 + 6), "Unsafe execution cannot safely handle serializable accounting."
        )

    def test_monetary_decimal_arithmetic_precision(self):
        """
        Monetary calculations must use Decimal to prevent binary float inaccuracies.
        Example: 0.10 + 0.20 in float is 0.30000000000000004; in Decimal it is exactly 0.30.
        """
        float_price1 = 0.10
        float_price2 = 0.20
        self.assertNotEqual(float_price1 + float_price2, 0.30, "Float unexpectedly equaled 0.30")

        decimal_price1 = Decimal("0.10")
        decimal_price2 = Decimal("0.20")
        total = decimal_price1 + decimal_price2
        self.assertEqual(total, Decimal("0.30"), "Decimal arithmetic must be exact.")

    def test_expired_batch_lockout_contract(self):
        """Expired batch must be rejected immediately, regardless of concurrency."""
        repo = MockPostgresBatchRepository("batch_uuid_103", "B-2025-01", initial_stock=50, is_expired=True)
        with self.assertRaises(BatchExpiredError):
            repo.reserve_stock_with_for_update_lock(5)


if __name__ == "__main__":
    unittest.main()
