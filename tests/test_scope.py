from tests.support import ApiCase, MAIN, ROOT, observations
from tests.test_recruitment import channel_plan


def channel_units(cell="cold/light"):
    units = []
    for arm, prefix in (("standard", "s"), ("assisted", "a")):
        for channel in ("store", "support"):
            units.append({
                "unit_id": f"{prefix}-{channel}",
                "site_id": "north",
                "arm": arm,
                "load": "light",
                "active_from": "2026-10-24",
                "active_until": "2026-10-29",
                "channel_id": channel,
            })
    return units


class TargetScopeTest(ApiCase):
    def setup_channels(self, **policy):
        self.create(channel_plan(**policy))
        self.register()
        opened = self.open(self.units(channel_units())["version"])
        return opened

    def events(self, channels=("store", "support"), *, assisted_wh=90):
        rows = []
        for arm, prefix in (("standard", "s"), ("assisted", "a")):
            for channel in channels:
                rows += observations(
                    f"{prefix}-{channel}", manual=(arm == "standard"), wh=assisted_wh
                )
        return rows

    def result(self, **params):
        return self.draft(**params)["market_results"][0]

    def test_full_target_requires_both_arms_in_every_channel(self):
        self.setup_channels()
        cut = self.deliver("all", self.events())["current_cut"]
        result = self.result(cut=cut)
        self.assertEqual(result["scope"], "full")
        self.assertEqual(result["target_coverage"]["supported_weight_bp"], "10000.000")
        self.assertEqual(result["scope_options"]["full"]["available"], True)
        self.assertEqual(result["scope_options"]["restricted"]["available"], False)

    def test_supported_channel_alone_cannot_stand_in_for_the_whole_market(self):
        self.setup_channels(minimum_target=5_000)
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        result = self.result(cut=cut)
        self.assertEqual(result["scope"], "restricted")
        coverage = result["target_coverage"]
        self.assertEqual(coverage["supported_weight_bp"], "6000.000")
        self.assertEqual(coverage["unsupported_weight_bp"], "4000.000")
        # Full-market metric stays unavailable so the restricted number cannot be
        # republished as market-wide performance.
        self.assertIsNone(result["metrics"]["manual"]["assisted"])
        self.assertEqual(
            result["supported_metrics"]["manual"]["assisted_minus_standard"],
            "-10000.000",
        )
        self.assertEqual(result["scope_options"]["full"]["available"], False)
        self.assertEqual(result["scope_options"]["restricted"]["available"], True)

    def test_minimum_target_bounds_restricted_publication(self):
        self.setup_channels(minimum_target=7_000)
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        result = self.result(cut=cut)
        self.assertEqual(result["scope"], "insufficient")
        self.assertEqual(result["scope_options"]["restricted"]["available"], False)

    def test_concentration_guard_blocks_a_dominant_channel(self):
        self.setup_channels(minimum_target=5_000, max_channel=5_000)
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        result = self.result(cut=cut)
        self.assertEqual(
            result["target_coverage"]["concentration"]["shares"], {"store": "10000.000"}
        )
        self.assertTrue(result["target_coverage"]["concentration"]["breach"])
        self.assertEqual(result["scope"], "insufficient")
        self.assertEqual(result["scope_options"]["restricted"]["available"], False)

    def test_missing_channel_distinguishes_recruitment_from_observation(self):
        self.create(channel_plan(minimum_target=5_000))
        self.register()
        self.open(self.units(channel_units())["version"])
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        result = self.result(cut=cut)
        unsupported = result["target_coverage"]["unsupported"]
        self.assertEqual(len(unsupported), 1)
        gap = unsupported[0]
        self.assertEqual(gap["channel"], "support")
        self.assertEqual(gap["stage"], "observation")
        self.assertEqual(gap["arms"]["standard"]["recruited_units"], 1)
        self.assertEqual(gap["arms"]["standard"]["eligible_units"], 0)
        self.assertIn("units", gap["arms"]["standard"]["reasons"])
        self.assertEqual(
            result["target_coverage"]["unsupported_by_reason"], {"observation": "4000.000"}
        )

    def test_later_capacity_changes_the_current_publishable_scope(self):
        self.setup_channels(minimum_target=5_000)
        first = self.deliver("store", self.events(("store",)))["current_cut"]
        self.assertEqual(self.result(cut=first)["scope"], "restricted")
        second = self.deliver("support", self.events(("support",)))["current_cut"]
        after = self.result(cut=second)
        self.assertEqual(after["scope"], "full")
        self.assertEqual(after["target_coverage"]["supported_weight_bp"], "10000.000")
        self.assertEqual(after["scope_options"]["restricted"]["available"], False)

    def test_reconciliation_reports_a_scope_change_between_cuts(self):
        self.setup_channels(minimum_target=5_000)
        first = self.deliver("store", self.events(("store",)))["current_cut"]
        second = self.deliver("support", self.events(("support",)))["current_cut"]
        changes = self.call("GET", ROOT + "/reconciliation", params={
            "before_cut": first, "after_cut": second,
        })
        market = changes["markets"][0]
        self.assertEqual(market["before_scope"], "restricted")
        self.assertEqual(market["after_scope"], "full")
        self.assertTrue(market["scope_changed"])
        self.assertEqual(
            market["target_coverage"]["supported_weight_bp"], "10000.000"
        )

    def test_restricted_bulletin_records_supported_weight_and_refuses_full_claim(self):
        opened = self.setup_channels(minimum_target=5_000)
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        self.close(opened["version"], cut)
        frozen = self.freeze(cut)
        evidence_id = frozen["evidence_id"]
        self.review(evidence_id, "field", "ada")
        self.review(evidence_id, "methods", "bea")
        refusal = self.issue(evidence_id, status=409)
        self.assertEqual(refusal["code"], "scope_unavailable")
        self.assertEqual(refusal["details"]["supported"], "restricted")
        bulletin = self.issue(evidence_id, scope="restricted")
        self.assertEqual(bulletin["scope"], "restricted")
        self.assertEqual(bulletin["supported_weight_bp"], "6000.000")
        self.assertIsNone(bulletin["result"]["metrics"]["manual"]["assisted"])
        self.assertEqual(
            bulletin["result"]["scope_options"]["full"]["available"], False
        )

    def test_an_unsupported_scope_may_still_be_held(self):
        opened = self.setup_channels(minimum_target=5_000)
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        self.close(opened["version"], cut)
        frozen = self.freeze(cut)
        evidence_id = frozen["evidence_id"]
        self.review(evidence_id, "field", "ada")
        self.review(evidence_id, "methods", "bea")
        bulletin = self.issue(evidence_id, outcome="hold")
        self.assertEqual(bulletin["scope"], "full")
        self.assertEqual(bulletin["decision_reason"], "scope_unavailable")
        self.assertIsNone(bulletin["result"]["metrics"]["manual"]["assisted"])

    def test_restricted_scope_is_never_full_scope_for_legacy_cells(self):
        # A climate/load-only plan keeps its original comparison meaning: it can
        # only be published whole.
        self.setup_trial()
        cut = self.deliver("good", self.green_events())["current_cut"]
        result = self.result(cut=cut)
        self.assertEqual(result["scope"], "full")
        self.assertNotIn("restricted", result["scope_options"])

    def test_reaching_planned_headcount_does_not_mask_analysis_shortfall(self):
        self.setup_channels(minimum_target=5_000)
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        result = self.result(cut=cut)
        cell = result["cells"][0]
        counts = {
            entry["channel"]: {
                arm: entry["arms"][arm]["recruited_units"] for arm in ("standard", "assisted")
            }
            for entry in cell["channels"]
        }
        # Every channel arm has the planned device, yet the support channel has no
        # qualified observation and so cannot support a market conclusion.
        self.assertEqual(counts["support"], {"standard": 1, "assisted": 1})
        self.assertEqual(result["scope"], "restricted")
        gap = result["target_coverage"]["unsupported"][0]
        self.assertEqual((gap["channel"], gap["stage"]), ("support", "observation"))
        self.assertEqual(gap["arms"]["assisted"]["eligible_units"], 0)

    def test_sensitivity_attributes_unknown_risk_to_the_unobserved_channel(self):
        self.setup_channels(minimum_target=5_000)
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        readout = self.call("GET", MAIN + "/sensitivity", params={"cut": cut})
        cell = readout["unknown_by_cell"][0]
        self.assertEqual(cell["channels"]["store"]["assisted"]["unqualified_units"], 0)
        support = cell["channels"]["support"]["assisted"]
        self.assertEqual(support["unqualified_units"], 1)
        self.assertEqual(support["market_weight_exposed_bp"], "4000.000")

    def test_calendar_exposes_channel_target_progress(self):
        self.setup_channels()
        cut = self.deliver("store", self.events(("store",)))["current_cut"]
        calendar = self.call("GET", MAIN + "/coverage-calendar", params={"cut": cut})
        day = next(item for item in calendar["days"] if item["local_date"] == "2026-10-25")
        cell = day["cells"][0]
        self.assertEqual(set(cell["channels"]), {"store", "support"})
        store = cell["channels"]["store"]["arms"]["assisted"]
        support = cell["channels"]["support"]["arms"]["assisted"]
        self.assertEqual(store["qualified_to_date"], 1)
        self.assertEqual(support["qualified_to_date"], 0)
