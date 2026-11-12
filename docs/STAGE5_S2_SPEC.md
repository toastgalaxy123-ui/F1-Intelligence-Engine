# Stage 5.2 — Pit execution and tire transitions

## Development gates

1. Gate A verifies route events and the two-lap timing identity with synthetic
   inputs. The scheduler invalidates the obsolete racing itinerary on diversion.
2. Gate B checks fresh and used replacement sets against the frozen Pace V2
   new-stint interface. Standalone continuation and S0 replay stay available.
3. Gate C checks common-checkpoint merges, uncertain merges, concurrent stops
   and exclusive same-box service before any interpretation of physical order.

## Timing and model ownership

A stop during lap `l` changes the canonical stint starting on lap `l+1`.
The time comparison always covers those same two laps:

```text
net loss = elapsed pit-in + elapsed pit-out
           − normal old-tire lap l − normal replacement-tire lap l+1
```

Pace V2 supplies the normal references, including its existing age effects.
Stationary service is inside the measured lane interval. A resolved timeline
already contains all traveled time. A coarse timeline posts its single net
adjustment at the end of lap `l+1`. Neither path charges a separate lane,
service or warm-up penalty.

## Execution contracts

- A request declares the driver, decision cutoff, pit-in lap, replacement set,
  future track state and optional inventory evidence. Unlisted historical
  availability remains unknown. The rules subset supports routine dry
  green-flag stops before the last lap. An explicitly supplied VSC or SC
  timeline can exercise synthetic timing with fixed track state. Full
  neutralization mechanics and sporting legality require additional evidence.
- The resolved synthetic layout diverts after in-lap sector 2, crosses the
  timing line on the pit route, and rejoins at out-lap sector 1. All route
  offsets are supplied relative to the start of the in-lap. The timing line
  may precede or follow service. A service start acquires its box, service end
  fits the tires and releases the box, and route exit precedes the merge.
- One box is exclusive to one car at a time. Box arrival time, then grid and
  driver ID, determines the queue. The wait shifts subsequent route timestamps
  and contributes to elapsed loss once. Different boxes can operate concurrently.
- A resolved merge compares passages through the same physical checkpoint
  using a declared comparison window and timing uncertainty. Nearby ties
  remain unresolved. The comparator can be a lapped car. It does not change
  classification or create an on-track overtake.
- Coarse execution has no physical service or rejoin timestamps. Its
  transition state remains unresolved until the out-lap accounting boundary.
  The pending out-lap sectors have no observed or inferred physical order.
  An overlapping coarse window also prevents a precise resolved order for
  other cars at the merge. An incomplete window is reported if the race ends first.
- An in-progress lap carries its last observed timing-line origin, completed
  checkpoints and elapsed time separately from the following full laps. It
  must finish exactly once. Initial S2 pit plans require a wholly future
  pit-in lap when initialized partway through a lap. The frozen S1 model
  retains its 20-step support limit.

## Tire and pace transitions

Fit the declared replacement set exactly once. Its stint counter advances even
for the same compound. A pit-route crossing ending the old in-lap does not age
the replacement set fitted during that lap. The out-lap starts at the declared
initial age, and its completion increments that age.

Use D8's existing fresh/used new-stint request with cutoff-known race history,
zero current-stint observations, and no updates from simulated lap times.
D8 resets the tire-specific posterior and retains its cutoff-derived race pace
anchor. This integration uses separate stochastic draws for the old and new
stints because the frozen interface exposes no conditionally transferable
random car effect. Unseen-circuit forecasts without clean race evidence are
unsupported. This compatibility mode supports one model-driven replacement per
driver in a window. Repeated declared stops are supported by the mechanical
engine. Known first-new-stint bias remains visible.

## P1 and historical evidence

The locked Stage 4.1 P1 artifact may enter coarse green-flag execution only
after its SHA-256 matches the selection record. Retain the provisional role
and original support. P1 offers no physical merge or approved service-tail
model. Its samples require consistent reference-error accounting when
combined with pace; deterministic expected loss avoids a fabricated shared
stochastic reference. An absent artifact is an explicit failure.

Historical lane windows from the Stage 4 reconstruction are retrospective
provider estimates. They support event coverage and timing audits. Synthetic
geometry establishes the mechanical merge tests; archived provider windows
do not establish exact historical merge order.
