from tests.support import ApiCase, MAIN, ROOT, enrollment, observations, plan, site


def channel_plan(*, store=6_000, support=4_000, minimum_target=10_000,
                 max_channel=10_000, required_units=2):
    trial_plan = plan()
    trial_plan["markets"][0]["cells"][0]["channels"] = [
        {"channel_id": "store", "weight_bp": store},
        {"channel_id": "support", "weight_bp": support},
    ]
    trial_plan["policy"]["required_units_per_arm"] = required_units
    trial_plan["policy"]["minimum_target_coverage_bp"] = minimum_target
    trial_plan["policy"]["max_channel_contribution_bp"] = max_channel
    return trial_plan


def candidate(name, *channels, at="north", load="light",
              start="2026-10-24", end="2026-10-29"):
    return {
        "candidate_id": name,
        "site_id": at,
        "load": load,
        "channels": list(channels),
        "available_from": start,
        "available_until": end,
    }


def invite(name, arm, channel=None):
    payload = {"candidate_id": name, "arm": arm}
    if channel is not None:
        payload["channel_id"] = channel
    return payload


class RecruitmentTest(ApiCase):
    def setup_channels(self, **options):
        self.create(channel_plan(**options))
        self.register()
        return self

    def candidates(self, items, *, status=200):
        return self.call("POST", ROOT + "/candidates", {"candidates": items}, status=status)

    def invitations(self, items, *, status=200):
        return self.call("POST", ROOT + "/invitations", {"invitations": items}, status=status)

    def accept(self, name, on="2026-10-24", *, status=200):
        return self.call(
            "POST", ROOT + f"/invitations/{name}/accept",
            {"accepted_on": on}, status=status,
        )

    def withdraw(self, name, *, status=200):
        return self.call(
            "POST", ROOT + f"/invitations/{name}/withdraw",
            {"note": "Reallocated"}, status=status,
        )

    def test_same_device_across_two_lists_occupies_only_one_slot(self):
        self.setup_channels()
        shared = candidate("door-one", "store")
        shared["device_id"] = "d-nine"
        duplicate = candidate("support-one", "support")
        duplicate["device_id"] = "d-nine"
        result = self.candidates([shared, duplicate], status=409)
        self.assertEqual(result["code"], "device_allocated")
        self.assertEqual(result["details"], {"candidate_id": "door-one"})
        # Registering one candidate with both channels is the intended merge.
        merged = candidate("merged", "store", "support")
        merged["device_id"] = "d-nine"
        self.candidates([merged])
        self.invitations([invite("merged", "standard", "support")])
        self.accept("merged")
        roster = self.call("GET", ROOT + "/roster")
        self.assertEqual(len(roster["units"]), 1)

    def test_candidate_must_use_a_planned_channel(self):
        self.setup_channels()
        result = self.candidates([candidate("c-one", "door")], status=422)
        self.assertEqual(result["code"], "unplanned_channel")
        result = self.candidates([candidate("c-one")], status=422)
        self.assertEqual(result["code"], "candidate_channels")

    def test_multi_channel_candidate_needs_an_explicit_channel(self):
        self.setup_channels()
        self.candidates([candidate("c-one", "store", "support")])
        result = self.invitations([invite("c-one", "standard")], status=422)
        self.assertEqual(result["code"], "channel_required")
        self.invitations([invite("c-one", "standard", "support")])

    def test_single_channel_candidate_is_placed_without_ambiguity(self):
        self.setup_channels()
        self.candidates([candidate("c-one", "support")])
        self.invitations([invite("c-one", "standard")])
        current = self.call("GET", ROOT + "/candidates")["candidates"][0]
        self.assertEqual(current["invitation"]["channel"], "support")
        self.assertEqual(current["invitation"]["status"], "pending")

    def test_pending_invitation_can_be_reassigned_but_accepted_one_is_fixed(self):
        self.setup_channels()
        self.candidates([
            candidate("c-one", "store", "support"),
            candidate("c-two", "store"),
        ])
        state = self.invitations([
            invite("c-one", "standard", "store"),
            invite("c-two", "standard", "store"),
        ])
        self.invitations([invite("c-one", "standard", "support")])
        accepted = self.accept("c-one")
        self.assertEqual(accepted["channel"], "support")
        fixed = self.invitations([invite("c-one", "standard", "store")], status=409)
        self.assertEqual(fixed["code"], "invitation_accepted")
        self.assertEqual(self.withdraw("c-one", status=409)["code"], "invitation_accepted")

    def test_pending_single_channel_candidate_can_be_withdrawn_out_of_the_way(self):
        self.setup_channels()
        self.candidates([candidate("c-one", "support", "store"), candidate("c-two", "store")])
        self.invitations([invite("c-one", "standard", "support")])
        self.withdraw("c-one")
        self.invitations([invite("c-two", "standard", "store")])
        current = {item["candidate_id"]: item for item in
                   self.call("GET", ROOT + "/candidates")["candidates"]}
        self.assertEqual(current["c-one"]["invitation"]["status"], "withdrawn")
        self.assertEqual(current["c-two"]["invitation"]["status"], "pending")

    def test_open_requires_channel_targets_for_every_planned_channel(self):
        self.setup_channels(required_units=4)
        self.candidates([
            candidate("s-store-one", "store"),
            candidate("s-store-two", "store"),
            candidate("s-support", "support"),
            candidate("a-store-one", "store"),
            candidate("a-store-two", "store"),
            candidate("a-support", "support"),
        ])
        for arm in ("standard", "assisted"):
            prefix = "s" if arm == "standard" else "a"
            for name, channel in (
                (f"{prefix}-store-one", "store"),
                (f"{prefix}-store-two", "store"),
                (f"{prefix}-support", "support"),
            ):
                self.invitations([invite(name, arm, channel)])
                self.accept(name)
        version = self.call("GET", ROOT)["version"]
        result = self.call("POST", ROOT + "/open", {"expected_version": version}, status=409)
        self.assertEqual(result["code"], "allocation_shortfall")
        for gap in result["details"]:
            self.assertEqual(gap["channel"], "support")
            self.assertEqual(gap["required"], 2)

    def test_pending_invitation_blocks_opening(self):
        self.setup_channels()
        self.candidates([candidate("c-one", "store")])
        self.invitations([invite("c-one", "standard", "store")])
        version = self.call("GET", ROOT)["version"]
        result = self.call("POST", ROOT + "/open", {"expected_version": version}, status=409)
        self.assertEqual(result["code"], "invitations_pending")
        self.assertEqual(result["details"], ["c-one"])

    def test_acceptance_is_immutable_and_produces_a_channel_attributed_unit(self):
        self.setup_channels()
        self.candidates([candidate("c-one", "store"), candidate("c-two", "store")])
        self.invitations([
            invite("c-one", "standard", "store"),
            invite("c-two", "standard", "store"),
        ])
        self.accept("c-one")
        roster = self.call("GET", ROOT + "/roster")
        self.assertEqual(roster["units"][0]["channel_id"], "store")
        self.assertEqual(roster["units"][0]["unit_id"], "c-one")
        again = self.accept("c-one")
        self.assertEqual(again["channel"], "store")
        outside = self.accept("c-two", on="2026-10-30", status=422)
        self.assertEqual(outside["code"], "acceptance_window")

    def test_recruitment_readout_counts_targets_by_channel_and_arm(self):
        self.setup_channels(required_units=4)
        self.candidates([
            candidate("store-one", "store"), candidate("support-one", "support"),
            candidate("store-two", "store"),
        ])
        self.invitations([
            invite("store-one", "standard", "store"),
            invite("support-one", "standard", "support"),
            invite("store-two", "standard", "store"),
        ])
        readout = self.call("GET", ROOT + "/recruitment")
        cell = readout["markets"][0]["cells"][0]
        self.assertEqual(cell["channels"]["store"]["arms"]["standard"]["target_units"], 2)
        self.assertEqual(cell["channels"]["store"]["arms"]["standard"]["pending"], 2)
        support = cell["channels"]["support"]["arms"]["standard"]
        self.assertEqual((support["target_units"], support["pending"]), (2, 1))

    def test_accepted_channel_cannot_be_removed_by_a_plan_revision(self):
        self.setup_channels()
        self.candidates([candidate("c-one", "support")])
        self.invitations([invite("c-one", "standard", "support")])
        self.accept("c-one")
        version = self.call("GET", ROOT)["version"]
        narrowed = channel_plan(store=10_000, support=0)
        narrowed["markets"][0]["cells"][0]["channels"] = [
            {"channel_id": "store", "weight_bp": 10_000},
        ]
        failure = self.call("PUT", ROOT + "/plan", {
            "expected_version": version, "plan": narrowed,
        }, status=422)
        self.assertEqual(failure["code"], "unplanned_channel")

    def test_planned_channel_weights_are_fixed_at_start(self):
        self.setup_channels()
        self.candidates([candidate("c-one", "store")])
        self.invitations([invite("c-one", "standard", "store")])
        state = self.call("GET", ROOT)
        revised = channel_plan(store=5_000, support=5_000)
        version = state["version"]
        # change is allowed while draft
        self.call("PUT", ROOT + "/plan", {"expected_version": version, "plan": revised})
        self.call("PUT", ROOT + "/plan", {"expected_version": version + 1,
                                          "plan": channel_plan()})
        self.accept("c-one")
        for arm, channel in (("standard", "store"), ("standard", "support"),
                             ("assisted", "store"), ("assisted", "support")):
            name = f"{arm}-{channel}"
            self.candidates([candidate(name, channel)])
            self.invitations([invite(name, arm, channel)])
            self.accept(name)
        state = self.call("GET", ROOT)
        self.open(state["version"])
        opened = self.call("GET", ROOT)
        self.assertEqual(opened["phase"], "open")
        rejected = self.call("PUT", ROOT + "/plan", {
            "expected_version": opened["version"],
            "plan": channel_plan(store=5_000, support=5_000),
        }, status=409)
        self.assertEqual(rejected["code"], "trial_phase")
