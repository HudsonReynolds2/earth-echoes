# E2 verification walkthrough — configuration model

Hand-verify the configuration release feature by feature against a seeded local stack.
This is a living acceptance document (rule R1): E2 ships it, and any later epic that
invalidates an assertion here amends it in the same batch. Sibling:
[the E1 walkthrough](e1-verification.md) covers hierarchy and inventory.

**Setup:** `.\qa-stack.ps1` from the repo root (see the E1 walkthrough for details),
sign in as the seeded owner, and open `http://localhost:5173`. The demo fixture seeds
no config overrides — you create them below, so this guide works against any fresh
stack. Remember `.\qa-stack.ps1 down` before ever running `.\gate.ps1` (D44).

## 1. The configuration tree and tabs

- [ ] Open **/configuration**. The same hierarchy tree as /inventory renders on the
      left (organization → deployments → pods → aggregators), with a **Saved
      selections** block at the rail's foot ("None yet" on a fresh stack).
- [ ] Unlike inventory, clicking an **aggregator** opens its own page — overrides are
      writable at aggregator level.
- [ ] The context bar carries three tabs: **Settings / Tags / Revisions**. The tab
      rides the URL (`?tab=`), so a deep link lands on the right pane.
- [ ] Every level titles the page **Configuration** with the entity in the breadcrumb
      and a level badge ("pod level", etc.) under the header.

## 2. The provenance walk (the heart of the release)

At **Redwood Coast → Pod 01 · Alder Creek**, Settings tab:

- [ ] Every catalog key renders in a grouped table (audio, capture, network, …), each
      group with a small rationale caption. Rows show KEY / VALUE / RESOLVED FROM /
      STATE / revert.
- [ ] Untouched keys read **default** (dashed chip) with "default" as the source.
- [ ] Change **network.wifi_ssid** (type a name). The row turns loud (accent tint),
      the chip flips to **edited**, and the amber **Unsaved draft** banner appears:
      "N keys changed. Nothing reaches devices until you publish."
- [ ] The **Draft changes** rail card shows `old → new` (old struck through in red,
      new in green).
- [ ] **Save draft** — one request; the banner clears; the chip now reads **set here**
      with a `×` revert control.
- [ ] Open a listener under this pod (tree → aggregator → its page won't list
      listeners; use /inventory or the deep link from §5). Its
      **network.wifi_ssid** shows your value with RESOLVED FROM **Pod** and an
      **inherited** chip — no editor, no revert (a pod-lowest key is read-only below
      its level: the at-or-above rule).
- [ ] Back at the pod, click the `×` on your override. The diff says "inherited
      again"; Save. The listener now shows the default again.

## 3. The level rule and the locked keys

- [ ] At a **listener** page: `network.*` keys render read-only (inherited/default) —
      editable at pod or above only. `identity.*` and `location.*` read from
      **inventory** (chip says so) with a link to the listener's inventory page —
      never editable here.
- [ ] At any level: `telemetry.*` and `upload.s3_bucket/s3_endpoint/…` render locked
      with "Managed by services onboarding — arrives with **E5**".
      **upload.s3_prefix** (aggregator level) IS editable — the deliberate exception.
- [ ] At the **organization** page: listener-lowest keys (audio, capture) are editable
      — an org-wide override that every device inherits. Set **logging.verbosity**
      to `warn` and watch any listener resolve it with source **Organization**.

## 4. Catalog-driven editors

One of each editor type, all rendered from the catalog with no bespoke code:

- [ ] **enum** — audio.sample_rate_hz (a select of the spec's sample rates).
- [ ] **int + unit** — capture.duty_on_seconds (number input with an "s" suffix).
- [ ] **float + range** — analysis.confidence_threshold at an aggregator page
      (bounded 0–1).
- [ ] **bool** — buffering.sd_enabled renders the ink-track toggle with a visible
      on/off word.
- [ ] **object** — capture.schedule renders a mono JSON box; invalid JSON shows "not
      valid JSON yet" and stages nothing; the server validates semantics.
- [ ] The catalog-is-data acceptance is proven by the automated suite (a fixture-only
      test key grows a working editor with zero frontend code); if you want to see it
      live, insert a row into `settings_catalog` in the QA database and reload —
      a new editor appears.

## 5. Secret handling

At the Alder Creek pod:

- [ ] **network.wifi_password** carries a SECRET chip; the value cell shows constant
      bullets and "not set".
- [ ] **Replace** opens a write-only password input. Type a value; the diff says
      **replaced** — the value appears nowhere. Save.
- [ ] Reload: bullets + "set". There is no reveal, no copy, and the browser dev-tools
      network tab shows only `{"$secret_set": true}` in responses — never your value.
- [ ] Keep-round-trip: change any OTHER key on the pod and save. The secret stays
      "set" (the sentinel round-trips; your password was neither cleared nor re-sent).

## 6. Draft semantics and the E3 slots

- [ ] The **Publish revision** button is disabled everywhere, tooltip naming **E3
      (EOE_PUBLISH_ENABLED)**.
- [ ] The banner's claim is literally true: nothing you saved above reaches any
      device — publication is E3's entire subject.
- [ ] The footer under the table reads "N keys resolved · catalog schema v1".

## 7. Bulk edit: preview → commit gating

At **/inventory → Redwood Coast → Pod 01 · Alder Creek**:

- [ ] The listeners table now leads with checkboxes. Select two listeners — a
      **Bulk edit (2)** button appears in the header.
- [ ] The wide modal opens: change form left, empty preview right ("Run Preview to
      see the affected devices before anything can commit").
- [ ] Pick **logging.verbosity** → `debug`, write at **Each selected listener**.
      **Commit is disabled.** Click **Preview**: the impact grid fills (Matched /
      Will change / No-op live; **Offline now shows "—" — live status arrives with
      E3**), and the table lists each device with Current → Resulting values and a
      Status column of "—".
- [ ] **The gating acceptance:** change the value to `trace`. Commit disables again
      ("The form changed since the last preview"). Re-preview; commit re-enables.
- [ ] **Commit.** The outcome panel reports draft revisions with checksums and names
      E3 for publishing.
- [ ] Write-at-level: select two listeners again, pick a `network.*` key — the only
      write option is **Their shared pod**, with copy warning that every listener in
      the pod inherits, selected or not.

## 8. Saved selections

- [ ] In the bulk modal, name the selection ("coastal pair") and **Save**.
- [ ] Open **/configuration**: the rail's Saved selections block lists it. Click it —
      the modal opens against the SAVED selection (by reference: the server re-checks
      membership when you preview, so a fleet change changes the match).
- [ ] There is no rename or delete — the API ships create/list only (spec 13).

## 9. Revisions

- [ ] After §7's commit: open one of the affected listeners in /configuration →
      **Revisions** tab. Draft rows list newest-first with `sha256:` checksums and
      schema v1.
- [ ] At a pod or deployment, the Revisions tab explains that revisions are
      per-device (spec 6.1) — pick a device in the tree.

## 10. Roles

Create these via /users if not present (the E1 walkthrough §8 has the flow):

- [ ] **Viewer**: /configuration renders every value read-only with "Configuration is
      read-only for your role…"; no Save, no revert, no checkboxes on device tables.
- [ ] **Field tech**: same locked treatment — visible, disabled, explained (config is
      deliberately outside the field role).
- [ ] **Scoped operator** (one deployment): edits work inside the deployment; the
      other deployment's pods answer not-found on their config pages; the
      organization page is read-only with "needs … organization-wide" (org-level
      writes require an org-wide grant).

## 11. Wrap-up

- [ ] The listener inventory page (E1) now carries an **Effective config** card with
      an Edit deep-link — the E1 walkthrough's §9 was amended accordingly.
- [ ] Nothing anywhere shows device status: no dots in the tree, "—" in every bulk
      column, no [data-status] elements — that guard lifts deliberately in E3.
- [ ] `.\qa-stack.ps1 down` when finished (keeps your data; `reset` wipes it).
