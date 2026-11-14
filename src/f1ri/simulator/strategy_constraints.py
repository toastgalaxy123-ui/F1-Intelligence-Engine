"""Read-only attribution of the bounds used by committed strategy crossings."""
from f1ri.simulator.events import EventKind as K
from f1ri.simulator.race_control import SC_PHASES, VSC_PHASES, ControlPhase

RESTART = {ControlPhase.SC_IN_THIS_LAP, ControlPhase.SC_RESTART_PENDING, ControlPhase.RESTART_ROLLING}
CROSSINGS = {K.LAP_COMPLETED, K.CHECKPOINT_CROSSING, K.ON_TRACK_PASS}


class CheckpointConstraints:
    def __init__(self):
        self.predictions = {}
        self.rows = []

    def prediction(self, row, start, headway, clock_floor, movement):
        # Object identity distinguishes the committed schedule from tentative
        # pass plans and regenerated rows that share driver/checkpoint keys.
        key = id(row)
        self.predictions[key] = (row, start, max(headway, 0), max(clock_floor, 0), movement)

    def commit(self, event, state, field):
        driver = event.driver_number
        if event.kind not in CROSSINGS | {K.PIT_SERVICE_START}:
            return
        car = next(c for c in state.cars if c.driver_number == driver)
        row = {"driver": driver, "checkpoint": event.checkpoint, "at_us": event.at_us,
               "event": event.kind.name, "source": event.source, "phase": field.phase,
               "track_state": state.track_state, "route": car.route,
               "compound": car.tyres.compound, "tyre_age": car.tyres.age_laps,
               "set_id": car.tyres.set_id, "stint": car.tyres.stint_number,
               "predecessor": field.ahead.get(driver),
               "latent_us": None, "effective_us": None, "following_only_us": None,
               "headway_floor_us": None, "movement_floor_us": None, "clock_floor_us": None}
        if event.kind is K.PIT_SERVICE_START:
            queued = next((r for r in reversed(field.pit_ledger) if r["driver"] == driver and
                           r["status"] == "SERVICE_QUEUE" and r["service_start_us"] == event.at_us), None)
            row.update(category="SERVICE_WAIT" if queued and queued["wait_us"] else "PIT_ROUTE",
                       effective_us=queued["wait_us"] if queued else None)
        elif event.source in ("PIT_ROUTE_LINE", "COMMON_MERGE"):
            row.update(category="PIT_ROUTE")
        elif event.kind is K.ON_TRACK_PASS:
            traversal = field.exchange[0]
            row.update(category="SCRIPTED_PASS", latent_us=traversal.clean_us,
                       effective_us=traversal.effective_us)
        elif record := self.predictions.get(id(field.plan.get((driver, event.checkpoint)))):
            traversal, start, headway, clock, motion = record
            if traversal.crossing_us != event.at_us:
                raise ValueError("Committed crossing does not match its strategy prediction")
            clean = traversal.clean_us
            other_floor = max(clean, motion, clock)
            following = max(0, headway-other_floor)
            if following:
                category = "PHYSICAL_FOLLOWING_LIMITED"
            elif motion > clean and motion >= clock:
                category = "RESTART_LIMITED" if field.phase in RESTART else "NEUTRALIZATION_LIMITED"
            elif clock > clean:
                category = "NEUTRALIZATION_LIMITED" if field.phase in SC_PHASES | VSC_PHASES else "CLOCK_FLOOR"
            else:
                category = "CLEAN_AIR_FREE"
            row.update(category=category, start_us=start, latent_us=clean, effective_us=traversal.effective_us,
                       following_only_us=following, headway_floor_us=headway, movement_floor_us=motion,
                       clock_floor_us=clock, predecessor=traversal.predecessor_driver,
                       predecessor_checkpoint=traversal.predecessor_checkpoint)
        else:
            row.update(category="UNSUPPORTED")
        self.rows.append(row)

    def prune(self, field):
        active = {id(row) for row in field.plan.values()}
        self.predictions = {k: row for k, row in self.predictions.items() if k in active}


def constraint_summary(rows, driver, marker):
    racing = [r for r in rows if r["driver"] == driver and r["checkpoint"] is not None and
              r["checkpoint"] <= marker and r["latent_us"] is not None]
    following = [r for r in racing if r["category"] == "PHYSICAL_FOLLOWING_LIMITED"]
    return {"racing_traversals": len(racing), "following_traversals": len(following),
            "following_share": len(following)/len(racing) if racing else None,
            "following_only_us": sum(r["following_only_us"] for r in following),
            "following_effective_us": sum(r["effective_us"] for r in following)}
