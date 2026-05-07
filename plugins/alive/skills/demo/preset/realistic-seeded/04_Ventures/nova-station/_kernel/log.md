---
walnut: nova-station
created: 2026-01-12
last-entry: 2026-04-22
entry-count: 5
summary: Nova Station moved from spec to crewed-test gating across the quarter.
---

<!-- LOG: prepend-only event spine for Nova Station. Newest at top. -->

## 2026-04-22 -- squirrel:e6f8a2b4c3d56789

You walked the launch-readiness bundle with Ryn Okata, line by line. The integration handoff to LaunchCo is on track for May. You closed three tasks and opened one new one.

### Decisions
- **Hold the May 2026 crewed window** because shielding-review is green and the launch-logistics integration test passes on the LaunchCo side. The alternative (slip to June) was rejected because it cascades into the southern-hemisphere tracking-station blackout.

### Work Done
- Reviewed the launch-readiness checklist; 14 of 17 items complete.
- Signed the partner integration handoff letter for LaunchCo.

### Tasks
- [x] Sign launch-logistics handoff letter
- [x] Confirm crewed window with Ryn
- [ ] Final go/no-go meeting one week before launch

signed: squirrel:e6f8a2b4c3d56789

## 2026-04-05 -- squirrel:d5e7f0a9b1c34567

You pulled the launch-readiness bundle into focus. The integration test with LaunchCo slipped a week because the telemetry handshake spec had a units mismatch. Ryn caught it on the first dry run.

### Decisions
- **Adopt SI units across the telemetry handshake** because the LaunchCo side is SI-native and the Nova Station-side conversion was the source of the dry-run failure. Imperial in the legacy harness is now the only exception.

### Work Done
- Patched the telemetry handshake spec to SI throughout.
- Re-ran the dry test; clean.

### Tasks
- [ ] Sign launch-logistics handoff letter
- [ ] Confirm crewed window with Ryn

signed: squirrel:d5e7f0a9b1c34567

## 2026-03-19 -- squirrel:c4d9e1f3a2b56789

You met Jax Stellara on the ExampleCorp side. The outer-hull panel batch is on schedule but the inner aluminum-lithium frame is two weeks late because of a heat-treatment furnace outage. Jax offered to expedite for a 12 percent surcharge.

### Decisions
- **Accept the expedite surcharge** because the two-week slip would push the crewed window into the southern-hemisphere tracking blackout, and the surcharge is below the contingency reserve. The alternative (let the schedule slip) was rejected on tracking-blackout grounds.

### Work Done
- Captured ExampleCorp delivery dates in the Drive folder.
- Filed the expedite-surcharge change order.

### Tasks
- [ ] Polaris confirms expedited delivery on the second Tuesday of April
- [x] File the change order

signed: squirrel:c4d9e1f3a2b56789

## 2026-03-02 -- squirrel:b2c8d4e9f1a35678

You ran the shielding-review pass with Ryn. The third-iteration shielding stack passes the 2 sievert per orbit threshold by a comfortable margin. The pass-band radiation review is green. Ryn flagged the docking-port hatch seal as the new top-of-list risk.

### Decisions
- **Lock the third-iteration shielding stack** because it clears the threshold by 18 percent and adds only 4 kilograms over the second iteration. The fourth iteration (a further 6 kilograms) was rejected on mass-budget grounds.

### Work Done
- Closed the shielding-review bundle's primary checklist.
- Filed the hatch-seal risk into the launch-readiness bundle.

### Tasks
- [x] Close shielding-review primary checklist
- [ ] Re-test docking-port hatch seal under cycled vacuum

signed: squirrel:b2c8d4e9f1a35678

## 2026-02-14 -- squirrel:a3b9f2c8d1e4567a

You opened the Nova Station walnut. Phase is testing. Goal is the first civilian orbital tourism platform with per-seat pricing. You scoped the two open bundles (shielding-review, launch-readiness), captured the key people, and set the May 2026 crewed window as the working target.

### Decisions
- **Run shielding-review and launch-readiness in parallel** because the dependencies between them only converge at the docking-port spec, and the docking-port spec was already locked in January.

### Work Done
- Created the walnut.
- Scoped the shielding-review and launch-readiness bundles.

### Tasks
- [ ] Schedule shielding-review pass with Ryn
- [ ] Confirm ExampleCorp delivery dates with Jax
- [ ] Lock the May 2026 crewed window with the LaunchCo partner

signed: squirrel:a3b9f2c8d1e4567a
