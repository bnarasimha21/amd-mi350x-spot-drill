"""scale_watch.py: the diffing and the interval arithmetic, on hand-written snapshots."""
import json
import unittest

from . import _util  # noqa: F401
import scale_watch as sw


def node(name, ready=True, unschedulable=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": unschedulable},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def pod(name, node_name=None, ready=False, terminating=False):
    meta = {"name": name}
    if terminating:
        meta["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    return {"metadata": meta, "spec": {"nodeName": node_name} if node_name else {},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def snap(nodes, pods, spec=1, ready=1, inflight=None):
    return sw.snapshot_from({"items": nodes}, {"items": pods},
                            {"spec": {"replicas": spec}, "status": {"readyReplicas": ready}}, inflight)


def names(events):
    return [e["event"] for e in events]


class Diff(unittest.TestCase):
    def test_first_poll_describes_the_start(self):
        ev = sw.diff(None, snap([node("n1")], [pod("p1", "n1", True)], inflight=3.0), 24)
        self.assertEqual(names(ev), ["watch_started"])
        self.assertEqual((ev[0]["nodes"], ev[0]["pods"], ev[0]["spec_replicas"]), (["n1"], ["p1"], 1))

    def test_no_change_no_events(self):
        s = snap([node("n1")], [pod("p1", "n1", True)], inflight=3.0)
        self.assertEqual(sw.diff(s, s, 24), [])

    def test_scale_up_sequence(self):
        s0 = snap([node("n1")], [pod("p1", "n1", True)], 1, 1, 10.0)
        s1 = snap([node("n1")], [pod("p1", "n1", True)], 1, 1, 30.0)                  # load arrives
        s2 = snap([node("n1")], [pod("p1", "n1", True), pod("p2")], 2, 1, 30.0)       # HPA wants 2
        s3 = snap([node("n1"), node("n2", ready=False)], [pod("p1", "n1", True), pod("p2")], 2, 1, 30.0)
        s4 = snap([node("n1"), node("n2")], [pod("p1", "n1", True), pod("p2", "n2")], 2, 1, 30.0)
        s5 = snap([node("n1"), node("n2")], [pod("p1", "n1", True), pod("p2", "n2", True)], 2, 2, 30.0)
        self.assertEqual(names(sw.diff(s0, s1, 24)), ["inflight_above_threshold"])
        self.assertEqual(names(sw.diff(s1, s2, 24)), ["pod_seen", "replicas_wanted_changed"])
        self.assertEqual(names(sw.diff(s2, s3, 24)), ["node_seen"])
        self.assertEqual(names(sw.diff(s3, s4, 24)), ["node_ready", "pod_scheduled"])
        self.assertEqual(names(sw.diff(s4, s5, 24)), ["pod_ready", "replicas_ready_changed"])

    def test_drain_sequence(self):
        s0 = snap([node("n1"), node("n2")], [pod("p1", "n1", True), pod("p2", "n2", True)], 2, 2)
        s1 = snap([node("n1"), node("n2", unschedulable=True)],
                  [pod("p1", "n1", True), pod("p2", "n2", True, terminating=True)], 2, 2)
        s2 = snap([node("n1"), node("n2", unschedulable=True)],
                  [pod("p1", "n1", True), pod("p3")], 2, 1)
        s3 = snap([node("n1")], [pod("p1", "n1", True), pod("p3")], 2, 1)
        self.assertEqual(names(sw.diff(s0, s1, None)), ["node_cordoned", "pod_terminating"])
        self.assertEqual(names(sw.diff(s1, s2, None)), ["pod_seen", "pod_gone", "replicas_ready_changed"])
        self.assertEqual(names(sw.diff(s2, s3, None)), ["node_gone"])

    def test_threshold_crossing_both_ways_and_missing_metric(self):
        a, b = snap([], [], inflight=30.0), snap([], [], inflight=2.0)
        none = snap([], [], inflight=None)
        self.assertEqual(names(sw.diff(a, b, 24)), ["inflight_below_threshold"])
        self.assertEqual(names(sw.diff(b, a, 24)), ["inflight_above_threshold"])
        self.assertEqual(sw.diff(a, none, 24), [])     # a failed query is not a crossing
        self.assertEqual(names(sw.diff(none, a, 24)), ["inflight_above_threshold"])


class Prom(unittest.TestCase):
    def test_vector_scalar_empty_garbage(self):
        vec = {"status": "success", "data": {"resultType": "vector",
               "result": [{"metric": {}, "value": [1700000000.0, "37"]}]}}
        self.assertEqual(sw.parse_prom_scalar(json.dumps(vec)), 37.0)
        sc = {"status": "success", "data": {"resultType": "scalar", "result": [1700000000.0, "4.5"]}}
        self.assertEqual(sw.parse_prom_scalar(json.dumps(sc)), 4.5)
        empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
        self.assertIsNone(sw.parse_prom_scalar(json.dumps(empty)))
        self.assertIsNone(sw.parse_prom_scalar("<html>502</html>"))


class Summary(unittest.TestCase):
    def test_intervals_by_hand(self):
        ev = [
            {"t": 90.0, "event": "node_ready", "node": "n1"},          # before t0: ignored
            {"t": 100.0, "event": "inflight_above_threshold"},
            {"t": 112.0, "event": "replicas_wanted_changed", "from": 1, "to": 2},
            {"t": 190.5, "event": "node_seen", "node": "n2"},
            {"t": 250.0, "event": "node_ready", "node": "n2"},
            {"t": 251.0, "event": "pod_scheduled", "pod": "p2", "node": "n2"},
            {"t": 400.0, "event": "pod_ready", "pod": "p2"},
            {"t": 400.0, "event": "replicas_ready_changed", "from": 1, "to": 2},
        ]
        s = sw.summarise(ev)
        self.assertTrue(s["t0_found"])
        self.assertEqual(s["intervals_s"], {
            "replicas_wanted_increased": 12.0, "new_node_seen": 90.5, "new_node_ready": 150.0,
            "new_pod_scheduled": 151.0, "new_pod_ready": 300.0, "replicas_ready_increased": 300.0})

    def test_no_start_and_unobserved_milestones(self):
        self.assertFalse(sw.summarise([{"t": 1.0, "event": "node_seen"}])["t0_found"])
        s = sw.summarise([{"t": 1.0, "event": "inflight_above_threshold"},
                          {"t": 5.0, "event": "replicas_wanted_changed", "from": 2, "to": 1}])
        self.assertIsNone(s["intervals_s"]["replicas_wanted_increased"])   # a decrease
        self.assertIsNone(s["intervals_s"]["new_pod_ready"])


if __name__ == "__main__":
    unittest.main()
