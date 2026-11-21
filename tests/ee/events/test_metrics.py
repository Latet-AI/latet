"""E4's metrics in E0c's registry (`latet.server.metrics.Metrics`, as Prometheus reads it): named as E0c names them
(`latet_<area>_<what>`), counted with `inc`, what waits for each output read at each scrape, and labelled by a type of
event or an output's kind only, each from the set E0c asks for (R13): never an output's name, nor a value."""

from latet.ee.events.metrics import ServerMetrics
from latet.server.metrics import Metrics

from .receivers import until


def test_e4s_metrics_are_named_as_e0c_names_them_and_labelled_by_a_type_or_an_outputs_kind_only():
    registry = Metrics()
    try:
        kinds = {"splunk-eu": "splunk", "elastic": "webhook"}
        metrics = ServerMetrics.of(registry)
        assert ServerMetrics.of(registry) is metrics  # once a registry, whatever editions are made on it (I10)
        waiting = {"splunk-eu": 12, "elastic": 3}
        metrics.bind(lambda: dict(waiting), kinds.get)
        metrics.kept({"exchange": 3, "stopped": 1})
        metrics.lost(2)
        metrics.sent("splunk-eu", 500)
        metrics.failed("splunk-eu")
        metrics.dropped("splunk-eu", 4)
        metrics.projects_full(5)
        text = registry.render()
        assert (
            'latet_events_kept_total{type="exchange"} 3' in text and 'latet_events_kept_total{type="stopped"} 1' in text
        )
        assert "latet_events_lost_total 2" in text and "or not of the schema" in text  # Task 6's refused events too
        assert "latet_events_other_project_total 5" in text  # a full projects bucket (Task 3's and Task 6's carry)
        assert 'latet_siem_sent_total{kind="splunk"} 500' in text and 'latet_siem_failed_total{kind="splunk"} 1' in text
        assert 'latet_siem_dropped_total{kind="splunk"} 4' in text
        # What waits is read from the queue in the metrics' own threads (M12): the same on every instance.
        assert until(lambda: 'latet_siem_queued{kind="splunk"} 12' in registry.render())
        text = registry.render()
        assert 'latet_siem_queued{kind="webhook"} 3' in text and "splunk-eu" not in text and "elastic" not in text
    finally:
        registry.close()


def test_an_output_whose_kind_cannot_be_told_is_counted_as_other_and_never_stops_a_count():
    registry = Metrics()
    try:
        metrics = ServerMetrics.of(registry)

        def unread(name):
            raise OSError("the settings cannot be read")

        metrics.bind(lambda: {"hook": 2, "gone": 1}, unread)
        metrics.sent("hook", 7)  # never raised to the delivery that counts it
        metrics.failed("nothing-of-that-name")
        assert 'latet_siem_sent_total{kind="other"} 7' in registry.render()
        assert until(lambda: 'latet_siem_queued{kind="other"} 3' in registry.render())
    finally:
        registry.close()


def test_a_service_closed_leaves_the_metrics_bound_to_the_one_made_after_it():
    registry = Metrics()
    try:
        metrics = ServerMetrics.of(registry)

        def first():
            return {"hook": 1}

        def second():
            return {"hook": 2}

        metrics.bind(first, lambda name: "webhook")
        metrics.bind(second, lambda name: "splunk")  # an edition made again on the same server (I10)
        metrics.unbind(first)  # the first one closes after: what is bound now stays
        assert until(lambda: 'latet_siem_queued{kind="splunk"} 2' in registry.render())
        metrics.unbind(second)
        metrics.sent("hook", 1)
        assert 'latet_siem_sent_total{kind="other"} 1' in registry.render()
    finally:
        registry.close()
