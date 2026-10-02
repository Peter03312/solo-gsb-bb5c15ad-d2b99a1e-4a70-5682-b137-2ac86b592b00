"""End-to-end tests for the waitlist feature.

Registration and cancellation consume no revision and leave the
application pending.  A suspension or a revocation drains the active
queue inside the same transaction: each release keeps its single revision,
every automatic approval consumes the following revision, blocked entries
stay queued and later entries are still considered.
"""
from fastapi.testclient import TestClient  # noqa: F401  (kept for type hints)


def body(
    circle=(0, 0, 10),
    freq=(100, 110),
    start="2026-10-01T08:00:00Z",
    end="2026-10-01T10:00:00Z",
    name="Event",
):
    from factories import make_payload

    return make_payload(circle, freq, start, end, name)


class TestWaitlistRegistration:
    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def _register(self, client, app_id, **kw):
        return client.post(f"/api/applications/{app_id}/waitlist", json=kw)

    def test_register_pending_returns_position_and_current_revision(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        # an unrelated approval advances the revision but registration
        # must not consume one itself
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()

        r = self._register(client, b, reason="band busy")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["waitlisted"] is True
        assert j["application_id"] == b
        assert j["queue_position"] == 1
        assert j["position"] == 1
        assert j["registered_at_revision"] == 1
        assert j["current_revision"] == 1
        assert j["reason"] == "band busy"
        assert j["application"]["status"] == "pending"

        # no revision was consumed and the application stayed pending
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 1
        assert current["waitlist_application_ids"] == [b]
        assert current["waitlist_count"] == 1
        assert [x["id"] for x in current["waitlist_applications"]] == [b]
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"

    def test_empty_body_allowed(self, client):
        a = self._submit(client, "A")
        r = client.post(f"/api/applications/{a}/waitlist")
        assert r.status_code == 200, r.text
        assert r.json()["reason"] == ""

    def test_queue_positions_follow_registration_order(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        cc = self._submit(client, "C", freq=(300, 301))
        for idx, app_id in enumerate((a, b, cc), start=1):
            r = self._register(client, app_id)
            assert r.json()["queue_position"] == idx

        wl = client.get("/api/waitlist").json()
        assert [e["application_id"] for e in wl["waitlist"]] == [a, b, cc]
        assert [e["queue_position"] for e in wl["waitlist"]] == [1, 2, 3]
        assert wl["total"] == 3 and wl["filter"] == "active"

    def test_duplicate_registration_refused_with_position(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        self._register(client, a).raise_for_status()
        self._register(client, b).raise_for_status()

        r = self._register(client, a)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "already_waitlisted"
        assert err["details"]["application_id"] == a
        assert err["details"]["queue_position"] == 1
        assert err["details"]["current_revision"] == 0

    def test_unknown_application_404(self, client):
        r = self._register(client, 999)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_non_pending_application_refused(self, client):
        a = self._submit(client, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        r = self._register(client, a)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_pending"
        assert r.json()["error"]["details"]["status"] == "approved"

        client.post(f"/api/applications/{a}/revoke", json={}).raise_for_status()
        r2 = self._register(client, a)
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "not_pending"
        assert r2.json()["error"]["details"]["status"] == "revoked"


class TestWaitlistCancel:
    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def test_cancel_keeps_pending_and_consumes_no_revision(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        client.post(f"/api/applications/{a}/waitlist").raise_for_status()
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()

        r = client.post(
            f"/api/applications/{a}/waitlist/cancel", json={"reason": "never mind"}
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["cancelled"] is True
        assert j["current_revision"] == 0
        assert j["reason"] == "never mind"
        assert j["application"]["status"] == "pending"
        assert j["remaining_waitlist_count"] == 1
        assert j["remaining_waitlist_application_ids"] == [b]

        # cancellation moved no revision and changed no application status
        assert client.get(f"/api/applications/{a}").json()["status"] == "pending"
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 0
        assert current["waitlist_application_ids"] == [b]

        # the surviving entry's dense queue position closed the hole
        entry = client.get(f"/api/applications/{b}/waitlist").json()["entry"]
        assert entry["queue_position"] == 1
        # the cancelled row is retained as history with its terminal state
        single = client.get(f"/api/applications/{a}/waitlist").json()["entry"]
        assert single["status"] == "cancelled"
        assert single["cancelled_at_revision"] == 0
        assert single["cancelled_reason"] == "never mind"
        assert "queue_position" not in single

    def test_cancel_without_body_allowed(self, client):
        a = self._submit(client, "A")
        client.post(f"/api/applications/{a}/waitlist").raise_for_status()
        r = client.post(f"/api/applications/{a}/waitlist/cancel")
        assert r.status_code == 200, r.text

    def test_cancel_not_waitlisted_refused(self, client):
        a = self._submit(client, "A")
        r = client.post(f"/api/applications/{a}/waitlist/cancel")
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "not_waitlisted"
        assert err["details"]["waitlist_status"] is None

    def test_cancel_unknown_application_404(self, client):
        r = client.post("/api/applications/999/waitlist/cancel")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_reregister_after_cancel_joins_tail_with_new_position(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        client.post(f"/api/applications/{a}/waitlist").raise_for_status()
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()
        client.post(f"/api/applications/{a}/waitlist/cancel").raise_for_status()

        r = client.post(f"/api/applications/{a}/waitlist")
        assert r.status_code == 200
        j = r.json()
        assert j["queue_position"] == 2  # tail; order is now b, a
        assert j["position"] == 3  # monotonic key, not reused
        order = [
            e["application_id"] for e in client.get("/api/waitlist").json()["waitlist"]
        ]
        assert order == [b, a]


class TestWaitlistQueries:
    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def test_filters(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        client.post(f"/api/applications/{a}/waitlist").raise_for_status()
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()
        client.post(f"/api/applications/{a}/waitlist/cancel").raise_for_status()

        assert [
            e["application_id"]
            for e in client.get("/api/waitlist").json()["waitlist"]
        ] == [b]
        cancelled = client.get("/api/waitlist?status=cancelled").json()
        assert cancelled["filter"] == "cancelled"
        assert [e["application_id"] for e in cancelled["waitlist"]] == [a]
        assert "queue_position" not in cancelled["waitlist"][0]

        all_rows = client.get("/api/waitlist?status=all").json()
        assert all_rows["total"] == 2

        bad = client.get("/api/waitlist?status=bogus")
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "validation_failed"

    def test_single_entry_missing_and_not_found(self, client):
        a = self._submit(client, "A")
        r = client.get(f"/api/applications/{a}/waitlist")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_waitlisted"

        assert client.get("/api/applications/999/waitlist").status_code == 404


class TestWaitlistDrain:
    """Suspend/revoke drain the active queue in one transaction."""

    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def _approve(self, client, app_id, expected):
        r = client.post(
            f"/api/applications/{app_id}/approve",
            json={"expected_revision": expected},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_revoke_releases_consecutive_waitlisted_in_registration_order(
        self, client
    ):
        # holder A occupies [100,110] at revision 1
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)
        # B and C both intersect A in frequency; queued in that order
        b = self._submit(client, "B", freq=(105, 106))
        cc = self._submit(client, "C", freq=(108, 109))  # gap 2 == guard vs B [105,106]
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()
        client.post(f"/api/applications/{cc}/waitlist").raise_for_status()

        # revoking A (revision 2) auto-approves B (3) then C (4)
        r = client.post(f"/api/applications/{a}/revoke", json={})
        assert r.status_code == 200, r.text
        j = r.json()
        # "revision" stays the revocation's own revision (compatible field);
        # final_revision additionally accounts for the auto-approvals.
        assert j["revision"] == 2
        assert j["final_revision"] == 4
        w = j["waitlist"]
        assert w["released_at_revision"] == 2
        assert w["final_revision"] == 4
        assert w["processed"] == 2
        assert w["approved_application_ids"] == [b, cc]
        assert w["still_waiting_application_ids"] == []
        assert [x["application_id"] for x in w["auto_approvals"]] == [b, cc]
        assert [x["revision"] for x in w["auto_approvals"]] == [3, 4]
        assert all(x["trigger_revision"] == 2 for x in w["auto_approvals"])
        assert all(x["trigger_kind"] == "revocation" for x in w["auto_approvals"])

        for app_id, rev in ((b, 3), (cc, 4)):
            app = client.get(f"/api/applications/{app_id}").json()
            assert app["status"] == "approved"
            assert app["approval_revision"] == rev
            entry = client.get(f"/api/applications/{app_id}/waitlist").json()
            assert entry["entry"]["status"] == "dequeued_approved"

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 4
        assert current["waitlist_count"] == 0

    def test_suspend_drains_with_suspension_trigger_kind(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)
        b = self._submit(client, "B", freq=(105, 106))
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()

        r = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["revision"] == 2  # the suspension's own revision
        assert j["final_revision"] == 3
        (auto,) = j["waitlist"]["auto_approvals"]
        assert auto["application_id"] == b
        assert auto["revision"] == 3
        assert auto["trigger_kind"] == "suspension"
        assert auto["trigger_revision"] == 2

    def test_blocked_entry_stays_and_later_entries_still_checked(self, client):
        # A and C both effective: A [100,110], C [200,210].
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)  # rev 1
        cc = self._submit(client, "C", freq=(200, 210))
        self._approve(client, cc, 1)  # rev 2
        # B clashes with A [105,106]; E [300,301] clashes with nobody once
        # the release happens.
        b = self._submit(client, "B", freq=(105, 106))
        e = self._submit(client, "E", freq=(300, 301))
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()
        client.post(f"/api/applications/{e}/waitlist").raise_for_status()

        # Revoking C frees nothing B needs: B stays queued (conflict with A
        # reported) but E is still checked and auto-approved.
        r = client.post(f"/api/applications/{cc}/revoke", json={})
        assert r.status_code == 200, r.text
        w = r.json()["waitlist"]
        assert w["released_at_revision"] == 3
        assert w["final_revision"] == 4
        assert w["approved_application_ids"] == [e]
        (waiting,) = w["still_waiting"]
        assert waiting["application_id"] == b
        assert waiting["queue_position"] == 1
        (conflict,) = waiting["conflicts"]
        assert conflict["application"]["id"] == a
        assert conflict["reason"] == "frequency_intersection"

        # B is still pending and still queued; no revision beyond E's
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 4
        assert current["waitlist_application_ids"] == [b]

        # revoking A now auto-approves B at revision 6
        r2 = client.post(f"/api/applications/{a}/revoke", json={})
        assert r2.status_code == 200
        w2 = r2.json()["waitlist"]
        assert w2["released_at_revision"] == 5
        assert w2["final_revision"] == 6
        assert [x["application_id"] for x in w2["auto_approvals"]] == [b]

    def test_auto_approved_entry_conflicts_with_earlier_auto_approval(
        self, client
    ):
        # A [100,110] effective. B [105,106] and D [105.5,107] both clash
        # with A and with each other; queued B then D.
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)  # 1
        b = self._submit(client, "B", freq=(105, 106))
        d = self._submit(client, "D", freq=(105.5, 107))
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()
        client.post(f"/api/applications/{d}/waitlist").raise_for_status()

        r = client.post(f"/api/applications/{a}/revoke", json={})
        assert r.status_code == 200, r.text
        w = r.json()["waitlist"]
        # B passes and is approved (rev 3) inside the drain; D is then
        # re-checked against the *new* effective set containing B and stays.
        assert [x["application_id"] for x in w["auto_approvals"]] == [b]
        (waiting,) = w["still_waiting"]
        assert waiting["application_id"] == d
        (conflict,) = waiting["conflicts"]
        assert conflict["application"]["id"] == b
        assert w["released_at_revision"] == 2
        assert w["final_revision"] == 3

    def test_drain_uses_current_guard_band(self, client):
        # A [100,110], C [115,116] effective; B [112,113] has gap 2 to C.
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)  # 1
        cc = self._submit(client, "C", freq=(115, 116))
        self._approve(client, cc, 1)  # 2
        b = self._submit(client, "B", freq=(112, 113))
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()

        # widen to 3 (rev 3); suspending A (rev 4) cannot help B: gap 2 < 3
        client.post(
            "/api/config/guard-band",
            json={"guard_band": 3.0, "expected_revision": 2},
        ).raise_for_status()
        r = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 3}
        )
        assert r.status_code == 200, r.text
        w = r.json()["waitlist"]
        assert w["approved_application_ids"] == []
        (waiting,) = w["still_waiting"]
        assert waiting["application_id"] == b
        assert waiting["conflicts"][0]["application"]["id"] == cc
        assert waiting["conflicts"][0]["reason"] == "within_guard_band"
        assert w["final_revision"] == 4  # release only, no auto revision

        # tightening back to 2 (rev 5) then revoking C (rev 6) lets B pass
        # under the live band 2.0; its automatic approval (rev 7) snapshots it
        client.post(
            "/api/config/guard-band",
            json={"guard_band": 2.0, "expected_revision": 4},
        ).raise_for_status()
        r2 = client.post(f"/api/applications/{cc}/revoke", json={})
        (auto,) = r2.json()["waitlist"]["auto_approvals"]
        assert auto["application_id"] == b
        assert auto["guard_band"] == 2.0
        assert auto["revision"] == 7

    def test_empty_queue_release_uses_one_revision(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)
        r = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        )
        assert r.status_code == 200
        j = r.json()
        assert j["revision"] == 2
        assert j["final_revision"] == 2
        assert j["waitlist"] == {
            "processed": 0,
            "released_at_revision": 2,
            "final_revision": 2,
            "approved_application_ids": [],
            "still_waiting_application_ids": [],
            "auto_approvals": [],
            "still_waiting": [],
        }

    def test_stale_release_changes_nothing_and_drains_nothing(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)  # rev 1
        b = self._submit(client, "B", freq=(105, 106))
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()

        # expected 0 is stale
        r = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 0}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_revision"

        # queue, licenses, revisions all unchanged
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 1
        assert current["effective_application_ids"] == [a]
        assert current["waitlist_application_ids"] == [b]
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"

    def test_manual_single_approval_dequeues_and_is_never_auto_approved(
        self, client
    ):
        # A [100,110] effective; B and C waitlisted (B clashes with A).
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)  # 1
        b = self._submit(client, "B", freq=(200, 201))
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()

        # manually approve B (rev 2): it must leave the queue and flag it
        r = self._approve(client, b, 1)
        assert r["dequeued_from_waitlist"] is True
        entry = client.get(f"/api/applications/{b}/waitlist").json()["entry"]
        assert entry["status"] == "dequeued_approved"

        # a later release must not "auto-approve" B again (no new event, no
        # error): only A is suspended (rev 3), queue is empty
        r2 = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 2}
        )
        assert r2.status_code == 200
        assert r2.json()["waitlist"]["processed"] == 0
        assert r2.json()["revision"] == 3
        assert r2.json()["final_revision"] == 3
        # B's approval record is a manual one
        snap = client.get("/api/revision/3").json()
        record = next(x for x in snap["approvals"] if x["application_id"] == b)
        assert record["revision"] == 2
        assert record["auto_approved"] is False
        assert record["trigger_revision"] is None

    def test_non_waitlisted_manual_approval_flags_false(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        r = self._approve(client, a, 0)
        assert r["dequeued_from_waitlist"] is False

    def test_batch_approval_dequeues_all_members(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)  # 1
        x = self._submit(client, "X", freq=(300, 301))
        y = self._submit(client, "Y", freq=(400, 401))
        client.post(f"/api/applications/{x}/waitlist").raise_for_status()
        client.post(f"/api/applications/{y}/waitlist").raise_for_status()

        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 1, "application_ids": [x, y]},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["dequeued_from_waitlist_application_ids"] == [x, y]
        assert all(z["dequeued_from_waitlist"] for z in j["results"])
        for app_id in (x, y):
            entry = client.get(f"/api/applications/{app_id}/waitlist").json()
            assert entry["entry"]["status"] == "dequeued_approved"

        # a later release sees an empty queue
        r2 = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 3}
        )
        assert r2.json()["waitlist"]["processed"] == 0

    def test_failed_batch_keeps_members_queued(self, client):
        x = self._submit(client, "X", freq=(500, 510))
        y = self._submit(client, "Y", freq=(505, 515))  # clashes with X
        client.post(f"/api/applications/{x}/waitlist").raise_for_status()
        client.post(f"/api/applications/{y}/waitlist").raise_for_status()

        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 0, "application_ids": [x, y]},
        )
        assert r.status_code == 409
        # both still pending and still active in the queue
        wl = client.get("/api/waitlist").json()
        assert [e["application_id"] for e in wl["waitlist"]] == [x, y]
        assert client.get("/api/revision/current").json()["current_revision"] == 0


class TestWaitlistHistory:
    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def test_snapshots_restore_auto_approvals_per_revision(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # 1
        b = self._submit(client, "B", freq=(105, 106))
        cc = self._submit(client, "C", freq=(108, 109))  # gap 2 == guard vs B [105,106]
        client.post(f"/api/applications/{b}/waitlist").raise_for_status()
        client.post(f"/api/applications/{cc}/waitlist").raise_for_status()
        client.post(f"/api/applications/{a}/revoke", json={}).raise_for_status()
        # rev 2 = revocation, rev 3 = B auto, rev 4 = C auto

        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == []
        assert [r["application_id"] for r in snap2["revocations"]] == [a]

        snap3 = client.get("/api/revision/3").json()
        assert snap3["effective_application_ids"] == [b]
        rec_b = next(x for x in snap3["approvals"] if x["application_id"] == b)
        assert rec_b["revision"] == 3
        assert rec_b["auto_approved"] is True
        assert rec_b["trigger_revision"] == 2
        # the manual approval of A is flagged accordingly
        rec_a = next(x for x in snap3["approvals"] if x["application_id"] == a)
        assert rec_a["auto_approved"] is False
        assert rec_a["trigger_revision"] is None
        assert rec_a["status_as_of"] == "revoked"

        snap4 = client.get("/api/revision/4").json()
        assert snap4["effective_application_ids"] == [b, cc]
        assert [
            (x["application_id"], x["auto_approved"], x["trigger_revision"])
            for x in snap4["approvals"]
        ] == [
            (a, False, None),
            (b, True, 2),
            (cc, True, 2),
        ]
