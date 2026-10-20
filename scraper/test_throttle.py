import threading
import time
from unittest import TestCase, mock

from scraper import throttle
from scraper.throttle import Gate


class GateTests(TestCase):
    def test_slot_never_lets_more_than_the_limit_through(self):
        gate, inside, peak, lock = Gate(2, 2), [0], [0], threading.Lock()

        def worker():
            with gate.slot():
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.05)
                with lock:
                    inside[0] -= 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(peak[0], 2)

    def test_adaptive_halves_when_struggling_with_a_cooldown(self):
        gate = Gate(4, 4, adaptive=True)
        gate.struggling()
        self.assertEqual(gate.limit, 2)
        gate.struggling()  # inside the cooldown: no second cut
        self.assertEqual(gate.limit, 2)
        later = time.monotonic() + throttle.COOLDOWN + 1
        with mock.patch.object(throttle.time, "monotonic", lambda: later):
            gate.struggling()
        self.assertEqual(gate.limit, 1)
        gate.struggling()
        self.assertEqual(gate.limit, 1)  # never below one

    def test_adaptive_grows_after_a_run_of_fast_answers_up_to_the_maximum(self):
        gate = Gate(2, 3, adaptive=True)
        for _ in range(throttle.GROW_AFTER - 1):
            gate.healthy(1)
        self.assertEqual(gate.limit, 2)
        gate.healthy(1)
        self.assertEqual(gate.limit, 3)
        for _ in range(throttle.GROW_AFTER * 2):
            gate.healthy(1)
        self.assertEqual(gate.limit, 3)  # capped

    def test_a_slow_answer_resets_the_streak(self):
        gate = Gate(1, 3, adaptive=True)
        for _ in range(throttle.GROW_AFTER - 1):
            gate.healthy(1)
        gate.healthy(throttle.FAST_SECONDS + 1)
        gate.healthy(1)
        self.assertEqual(gate.limit, 1)

    def test_fixed_gate_ignores_feedback(self):
        gate = Gate(3, 3, adaptive=False)
        gate.struggling()
        for _ in range(50):
            gate.healthy(1)
        self.assertEqual(gate.limit, 3)
