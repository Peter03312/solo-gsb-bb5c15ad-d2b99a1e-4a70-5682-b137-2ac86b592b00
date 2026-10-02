#!/usr/bin/env bash
# End-to-end curl walkthrough for the Temporary Event Radio Spectrum API.
# Usage: ./examples/walkthrough.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

echo "== health =="
curl -s "$BASE/health"; echo

echo "== 1. invalid submission (422, whole request rejected) =="
post /api/applications '{
  "name":"bad",
  "circle":{"cx":0,"cy":0,"radius":-5},
  "frequency":{"low":200,"high":100},
  "time_window":{"start":"2026-10-01T10:00:00Z","end":"2026-10-01T08:00:00Z"}
}' | J

echo "== 2. submit A =="
A=$(post /api/applications '{
  "name":"Festival A",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T12:00:00Z"}
}')
echo "$A" | J
A_ID=$(echo "$A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 3. preview A =="
post "/api/applications/$A_ID/preview" | J

echo "== 4. approve A at revision 0 =="
post "/api/applications/$A_ID/approve" '{"expected_revision":0}' | J

echo "== 5. submit B (1 MHz gap < 2 MHz guard band) =="
B=$(post /api/applications '{
  "name":"Marathon B",
  "circle":{"cx":100,"cy":0,"radius":1000},
  "frequency":{"low":111,"high":112},
  "time_window":{"start":"2026-10-01T09:00:00Z","end":"2026-10-01T11:00:00Z"}
}')
B_ID=$(echo "$B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$B_ID/preview" | J

echo "== 6. stale approval (expected 0, current 1) =="
post "/api/applications/$B_ID/approve" '{"expected_revision":0}' | J || true

echo "== 7. current revision but interference =="
post "/api/applications/$B_ID/approve" '{"expected_revision":1}' | J || true

echo "== 8. revoke A =="
post "/api/applications/$A_ID/revoke" '{"reason":"postponed"}' | J

echo "== 9. approve B at revision 2 =="
post "/api/applications/$B_ID/approve" '{"expected_revision":2}' | J

echo "== 10. history snapshots =="
curl -s "$BASE/api/revision/1" | J
curl -s "$BASE/api/revision/2" | J
curl -s "$BASE/api/revision/current" | J

echo "== 11. submit three more devices for one event (disjoint bands) =="
mk() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}"; }
C=$(mk "Device C" 200 210); C_ID=$(echo "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
D=$(mk "Device D" 300 310); D_ID=$(echo "$D" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
E=$(mk "Device E" 400 410); E_ID=$(echo "$E" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "ids: $C_ID $D_ID $E_ID"

echo "== 12. batch approve at revision 3, input order D,C,E -> revisions 4,5,6 =="
post /api/applications/batch-approve \
  "{\"expected_revision\":3,\"application_ids\":[$D_ID,$C_ID,$E_ID]}" | J

echo "== 13. snapshot as of the second item (revision 5) has D and C effective =="
curl -s "$BASE/api/revision/5" | J

echo "== 14. failing batch: two members clash with each other -> whole group rejected =="
F=$(mk "Device F" 500 510); F_ID=$(echo "$F" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G=$(mk "Device G" 505 515); G_ID=$(echo "$G" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
H=$(mk "Device H" 600 610); H_ID=$(echo "$H" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[$F_ID,$G_ID,$H_ID]}" | J || true
echo "revision is still 6 and all three are pending:"
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print("current_revision =", json.load(sys.stdin)["current_revision"])'

echo "== 15. malformed batches (duplicate ids / missing id) =="
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[$F_ID,$F_ID]}" | J || true
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[999999]}" | J || true

echo "== 16. guard band: current config (live value, initial value, revision) =="
curl -s "$BASE/api/config/guard-band" | J

echo "== 17. preview a wider band (100): pairs among the effective grants appear =="
echo "    (devices C/D/E sit 90 MHz apart; B's time window does not overlap theirs)"
post /api/config/guard-band/preview '{"guard_band":100}' | J

echo "== 18. confirming it is refused (409 interference); nothing changes =="
post /api/config/guard-band '{"guard_band":100,"expected_revision":6}' | J || true
curl -s "$BASE/api/config/guard-band" | J

echo "== 19. tighten to 1.0: accepted, exactly one new revision (7) =="
post /api/config/guard-band '{"guard_band":1.0,"expected_revision":6}' | J

echo "== 20. the change shares revision arbitration: stale confirms/approvals lose =="
post /api/config/guard-band '{"guard_band":0.5,"expected_revision":6}' | J || true
post "/api/applications/$F_ID/approve" '{"expected_revision":6}' | J || true

echo "== 21. invalid values are rejected with 422 (no revision consumed) =="
post /api/config/guard-band '{"guard_band":-5,"expected_revision":7}' | J || true

echo "== 22. history restores the band in force at each revision =="
curl -s "$BASE/api/revision/6" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revision", d["revision"], "guard_band =", d["guard_band"])'
curl -s "$BASE/api/revision/7" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revision", d["revision"], "guard_band =", d["guard_band"], "changes =", [(c["revision"], c["previous_guard_band"], c["guard_band"]) for c in d["guard_band_changes"]])'
echo "    approval events keep the band they were decided under (not rewritten):"
curl -s "$BASE/api/revision/7" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("approval guard_bands =", [(a["revision"], a["guard_band"]) for a in d["approvals"]])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "guard_band =", d["guard_band"])'

echo "== 23. available-ranges search for pending device F (band [500,510], width 10) =="
echo "    effective grants in its 13:00-18:00 window: C [200,210], D [300,310], E [400,410]; guard band is now 1.0"
post "/api/applications/$F_ID/available-ranges" \
  '{"candidate_low_min":180,"candidate_low_max":430}' | J
echo "    forbidden by C: (200-10-1, 210+1) = (189,211); by D: (289,311); by E: (389,411)"
echo "    -> free lower bounds: [180,189], [211,289], [311,389], [411,430] (endpoints attainable)"

echo "== 24. a reported endpoint previews conflict-free; a forbidden point does not =="
SHIFTED=$(post /api/applications "{
  \"name\":\"F shifted to 211\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":211,\"high\":221},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}")
SHIFTED_ID=$(echo "$SHIFTED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$SHIFTED_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("shifted to 211 (gap == guard): has_conflicts =", d["has_conflicts"])'
CLASH=$(post /api/applications "{
  \"name\":\"F shifted to 200\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":200,\"high\":210},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}")
CLASH_ID=$(echo "$CLASH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$CLASH_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("shifted to 200 (inside forbidden): has_conflicts =", d["has_conflicts"])'

echo "== 25. the search is non-mutating and validates its inputs =="
post "/api/applications/$F_ID/available-ranges" \
  '{"candidate_low_min":430,"candidate_low_max":180}' | J || true
post "/api/applications/999999/available-ranges" \
  '{"candidate_low_min":180,"candidate_low_max":430}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"])'

echo "== 26. suspend C (temporary device outage): leaves the effective set, one revision (8) =="
post "/api/applications/$C_ID/suspend" \
  '{"expected_revision":7,"reason":"device downtime"}' | J
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"])'

echo "== 27. while C is suspended, a device I overlapping C's band [205,208] can be approved (9) =="
I=$(post /api/applications '{
  "name":"Device I",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":205,"high":208},
  "time_window":{"start":"2026-10-01T13:00:00Z","end":"2026-10-01T18:00:00Z"}
}')
I_ID=$(echo "$I" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$I_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("has_conflicts =", d["has_conflicts"], "(C is suspended)")'
post "/api/applications/$I_ID/approve" '{"expected_revision":8}' | J

echo "== 28. resume C re-adjudicates: clashes with I -> 409 interference, C stays suspended =="
post "/api/applications/$C_ID/resume" '{"expected_revision":9}' | J || true
post "/api/applications/$C_ID/resume" '{"expected_revision":9}' >/dev/null || true
echo "    a stale resume returns the current revision and changes nothing either:"
post "/api/applications/$C_ID/resume" '{"expected_revision":8}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"], "; C status =", [a["status"] for a in d["suspended_applications"]])'

echo "== 29. permanently revoke I (revision 10), then resume C succeeds (revision 11) =="
post "/api/applications/$I_ID/revoke" '{"reason":"obsolete"}' | J
post "/api/applications/$C_ID/resume" \
  '{"expected_revision":10,"reason":"fault cleared"}' | J

echo "== 30. history restores suspend/resume records and each effective set =="
curl -s "$BASE/api/revision/8" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 8: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"], "suspensions =", [(s["revision"], s["application_id"]) for s in d["suspensions"]])'
curl -s "$BASE/api/revision/9" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 9: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"])'
curl -s "$BASE/api/revision/11" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 11: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"]); print("        suspensions =", [(s["revision"], s["application_id"]) for s in d["suspensions"]], "resumptions =", [(s["revision"], s["application_id"], s["guard_band"]) for s in d["resumptions"]], "revocations =", [(s["revision"], s["application_id"]) for s in d["revocations"]])'

echo "== 31. unknown ids and illegal states are refused without consuming a revision =="
post /api/applications/999999/suspend '{"expected_revision":11}' | J || true
post "/api/applications/$H_ID/suspend" '{"expected_revision":11}' | J || true
post "/api/applications/$D_ID/resume" '{"expected_revision":11}' | J || true
post "/api/applications/$C_ID/suspend" '{"expected_revision":11}' >/dev/null
post "/api/applications/$C_ID/suspend" '{"expected_revision":12}' | J || true
post "/api/applications/$C_ID/resume" '{"expected_revision":12}' >/dev/null
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "(the rejections consumed none)")'

# --- waitlist walkthrough (isolated 20:00-22:00 window; guard band is 1.0) ---
mkw() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2026-10-01T20:00:00Z\",\"end\":\"2026-10-01T22:00:00Z\"}
}"; }

echo "== 32. approve holder J [700,710] (revision 14); queue K, M, L behind it =="
J=$(mkw "Holder J" 700 710); J_ID=$(echo "$J" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$J_ID/approve" '{"expected_revision":13}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print("J approved at revision", d["revision"])'
K=$(mkw "Wait K" 705 706);  K_ID=$(echo "$K" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
M=$(mkw "Wait M" 705.5 707); M_ID=$(echo "$M" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
L=$(mkw "Wait L" 720 721);  L_ID=$(echo "$L" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$K_ID/waitlist" '{"reason":"band busy"}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print("K queued at position", d["queue_position"], "current_revision", d["current_revision"])'
post "/api/applications/$M_ID/waitlist" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("M queued at position", d["queue_position"])'
post "/api/applications/$L_ID/waitlist" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("L queued at position", d["queue_position"])'
curl -s "$BASE/api/waitlist" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("queue:", [(e["application_id"], e["queue_position"]) for e in d["waitlist"]])'

echo "== 33. duplicate / non-pending / unknown registrations are refused (no revision) =="
post "/api/applications/$K_ID/waitlist" | J || true
post "/api/applications/$J_ID/waitlist" | J || true
post /api/applications/999999/waitlist | J || true

echo "== 34. register N then cancel it: application stays pending, no revision consumed =="
N=$(mkw "Wait N" 800 801); N_ID=$(echo "$N" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$N_ID/waitlist" >/dev/null
post "/api/applications/$N_ID/waitlist/cancel" '{"reason":"operator cancelled"}' | J
post "/api/applications/$N_ID/waitlist/cancel" | J || true
echo "    N status is still: $(curl -s "$BASE/api/applications/$N_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"

echo "== 35. revoke J (revision 15): drain K, M, L in one transaction =="
echo "    K [705,706] passes (auto rev 16); M [705.5,707] then clashes with K and stays; L [720,721] still gets checked and passes (auto rev 17)"
post "/api/applications/$J_ID/revoke" '{"reason":"spectrum released"}' | J

echo "== 36. history restores the automatic approvals revision by revision =="
curl -s "$BASE/api/revision/15" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 15: effective =", d["effective_application_ids"])'
curl -s "$BASE/api/revision/16" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 16: effective =", d["effective_application_ids"], "; K approval auto =", [(a["application_id"], a["auto_approved"], a["trigger_revision"]) for a in d["approvals"] if a["application_id"]=='"$K_ID"'])'
curl -s "$BASE/api/revision/17" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 17: effective =", d["effective_application_ids"], "(M is not among them)")'
curl -s "$BASE/api/waitlist" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("queue now:", [(e["application_id"], e["queue_position"]) for e in d["waitlist"]])'

echo "== 37. revoke K (revision 18): M now fits and is auto-approved (revision 19) =="
post "/api/applications/$K_ID/revoke" '{"reason":"clearing the blocker"}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print("final revision", d["revision"], "; auto approved:", [(x["application_id"], x["revision"]) for x in d["waitlist"]["auto_approvals"]], "; still waiting:", [x["application_id"] for x in d["waitlist"]["still_waiting"]])'

echo "== 38. suspension drains the queue the same way: approve P, queue Q, suspend P =="
P=$(mkw "Holder P" 900 910); P_ID=$(echo "$P" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
Q=$(mkw "Wait Q" 905 906);  Q_ID=$(echo "$Q" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$P_ID/approve" '{"expected_revision":19}' >/dev/null
post "/api/applications/$Q_ID/waitlist" >/dev/null
post "/api/applications/$P_ID/suspend" '{"expected_revision":20,"reason":"releasing for the waitlist"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("suspension revision", d["revision"], "final revision", d["final_revision"], "; auto approved:", [(x["application_id"], x["revision"], x["trigger_kind"]) for x in d["waitlist"]["auto_approvals"]])'

echo "== 39. a stale release leaves queue, licenses and revisions untouched =="
# current revision is 22; suspending effective L with expected revision 1
# loses stale_revision: L stays effective, nothing is drained, revision stays 22
post "/api/applications/$L_ID/suspend" '{"expected_revision":1}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "; L still effective:", int("'"$L_ID"'") in d["effective_application_ids"])'
