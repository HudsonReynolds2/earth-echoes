# E4 Verification Walkthrough: Device Provisioning

Hand-verify epic E4 against a clean local stack: generating a bundle, what is (and is not) in
it, and watching the tracking board fill in by itself as a device comes online from its own
card. Every step below was run start to finish against a fresh stack before this document
shipped. That run found two pre-existing defects (DECISIONS D182, D183), both fixed in this
batch.

Allow about 30 minutes. You need Docker, `uv`, and a browser.

## 0. Start the platform

- [ ] Follow [Getting started](getting-started.md) through *Launch* (Windows: `.\qa-stack.ps1`;
      POSIX: the sequence in [the E3 walkthrough](e3-verification.md) section 0).
      **`deploy/.env` needs `EOE_FIRMWARE_KEK`** (`openssl rand -base64 32`). A `.env` written
      before E4 lacks it, and compose stops with
      `required variable EOE_FIRMWARE_KEK is missing a value`. That refusal is itself the
      first check.
- [ ] Sign in at `http://localhost:15173` as the seeded owner.
- [ ] **Provisioning** in the nav opens the new page: a *Deployment* picker, a **New bundle**
      button, and "No bundles yet".

## 1. A deployment needs verified services first (spec 16.5)

Bundles embed each Aggregator's broker login, and those logins are minted through Mosquitto's
dynamic security plugin, so a deployment must have a **verified** broker before anything can
be provisioned.

- [ ] With **Redwood Coast** selected (its services are not verified), open **New bundle** and
      go through to **Generate bundle**. It is refused with "*Redwood Coast's services are not
      verified yet (spec 16.5). Configure and verify the broker on the deployment's Services
      page…*". Nothing is generated.
- [ ] Verify **High Desert** with E5's Path B: follow [the E5 walkthrough](e5-verification.md)
      section 7 (generate the stack, `docker compose up -d` it, **Test** on all services), until
      its rollup reads **verified**. On this local setup, set the stack's **hostname to
      `host.docker.internal` and IP to `127.0.0.1`**: the API runs inside Docker and reaches the
      stack by the first, while a device on your machine reaches it by the second. The
      certificate covers both (D176's known limitation: one endpoint, two viewpoints).

## 2. A Pod must be able to join its network

- [ ] Select **High Desert**, **New bundle**, pick **Pod 01 · Basin Flat**, keep *One file for
      the whole Pod*, and generate. It is refused, listing exactly the network keys to set:
      `network.wifi_ssid`, `network.wifi_password`, `network.stream_key`,
      `network.aggregator_ip`, `network.stream_endpoint`.
- [ ] In **Configuration → High Desert → Pod 01 · Basin Flat**, set those five (any values; the
      password and stream key are write-only). Then **apply** one more change to the same Pod,
      e.g. `logging.verbosity = debug`. **That apply must succeed.** Before D182 it returned a
      500 on any Pod holding a WiFi password, which would have made every provisioned Pod
      un-configurable.

> The apply matters for the journey below: it gives every device a published revision for its
> card to embed. A card whose device has no revision can reach `registered` but never
> `confirmed` (D179).

## 3. Generate (E4.1–E4.8, E4.10)

- [ ] **New bundle** → *One Pod* → **Pod 01 · Basin Flat** → *One file for the whole Pod* →
      **Next** → the review lists the Pod with **7** cards and warns that the archive carries
      the Aggregator's broker login **in plaintext, by design**.
- [ ] **Generate bundle**. The wizard closes, "Bundle generated…" appears, and the **board**
      shows the bundle: tiles *Files 2*, *Cards expected 7*, *Confirmed 0 / 7*, *Mismatch 0*,
      and **7 cards in Generated** (six Listeners plus the Aggregator).
- [ ] Chips are words with glyphs (○ Generated, ↧ Flashed, ◎ Registered, ✓ Confirmed,
      ✕ Mismatch), and they are **not** the map's device-status colors-and-dots. Record status
      is its own vocabulary (D40, D180).

## 4. What is in the archive

- [ ] **Download bundle** saves `echoes-provisioning-<id>.zip`. Unzip it: `README.md`,
      `manifest.json`, `pods/<pod>/device_config.yaml` and `pods/<pod>/aggregator/settings.yaml`.
- [ ] `README.md` says which cards take which file, flags both formats as **DRAFT**, and
      explains the plaintext Aggregator login.
- [ ] `device_config.yaml`: the network block has your SSID and IP, `identity` and `location`
      are `null` (filled in later from the cloud), and it ends with a `secrets:` block of
      base64 ciphertext. **Search the whole unzipped folder for the WiFi password you set: it
      appears nowhere** (`grep -r '<your password>' .` finds nothing).
- [ ] `settings.yaml` carries `aggregator_uuid: demo-agg-hd-01`, the broker endpoint, the CA as
      a readable PEM block, and the device's own `username`/`password`, plaintext by design
      (spec 16.4).
- [ ] Every checksum in `manifest.json` matches its file. From the unzipped folder:

  ```bash
  python3 -c "import hashlib,json; m=json.load(open('manifest.json')); \
  print(all('sha256:'+hashlib.sha256(open(f['path'],'rb').read()).hexdigest()==f['sha256'] for f in m['files']))"
  ```

  → `True`.

## 5. The card journey (E4.9): the board fills in by itself

- [ ] With nothing checked, the button reads *Check cards to mark flashed* and is disabled.
      Check **two** cards: it reads *Mark 2 cards flashed*. Press it. Only those two move to
      **Flashed**; the rest stay in **Generated** (D184).
- [ ] **Reload the page.** The two cards are still in **Flashed**: each card's status is saved
      as it is marked, so 30 cards can be flashed over several sittings.
- [ ] In the **Flashed** column, check one of those two cards and press **Unmark 1 card**. It
      goes back to **Generated** and stays there (D187). Cards in Registered, Confirmed or
      Mismatch never offer a checkbox: their status is what the device reported.
- [ ] **Select all** in the Generated column, then **Mark 6 cards flashed**. All seven are now
      **Flashed**.
- [ ] Bring the Aggregator up **from its card**, exactly as a Pi would: its own login from
      `settings.yaml`, with this Pod's six Listeners attached. From the repo root, with `ZIPDIR`
      set to your unzipped folder:

  ```bash
  cd sim && ZIPDIR=<unzipped folder> uv run python - <<'EOF'
  import asyncio, glob, json, os, yaml
  from pathlib import Path
  from device import BrokerLogin, MockAggregator
  z = os.environ["ZIPDIR"]
  s = yaml.safe_load(open(glob.glob(f"{z}/pods/*/aggregator/settings.yaml")[0]))
  Path(f"{z}/ca.crt").write_text(s["mqtt"]["ca_cert"])
  macs = [c["target_id"] for f in json.load(open(f"{z}/manifest.json"))["files"]
          for c in f["cards"] if c["target_type"] == "listener"]
  # D176: the card names the broker as the platform sees it; from here it is 127.0.0.1.
  login = BrokerLogin(host="127.0.0.1", port=s["mqtt"]["port"], username=s["mqtt"]["username"],
                      password=s["mqtt"]["password"], ca_cert=Path(f"{z}/ca.crt"))
  async def main():
      device = MockAggregator(deployment_slug="high-desert", aggregator_uuid=s["aggregator_uuid"], login=login)
      listeners = [await device.add_listener(m) for m in macs]
      async with device:
          await device.wait_for_apply()
          for l in listeners: await l.wait_for_apply()
          print("applied; staying up 90s"); await asyncio.sleep(90)
  asyncio.run(main())
  EOF
  ```

- [ ] **Within about a minute, without reloading**, all seven cards move to **Confirmed**
      and the tile reads *Confirmed 7 / 7*. The worker's reconciler moved them; nothing was
      set by hand except *flashed*.
- [ ] If you generated an earlier bundle for this Pod before the `logging.verbosity` apply,
      switch to it in the *Bundle* picker: its cards are **Registered**, not Mismatch. Those
      devices run a *later* revision than those cards embed, which is an operator's change,
      not a disagreement (D179).

## 6. Filling in later (E4.11; spec 8.6)

- [ ] On a **Confirmed** Listener card that shows *No GPS yet*, press **Fill in details**. Set
      a name and both coordinates, then **Save**. "Saved …" appears and the hint goes away.
- [ ] **Inventory → High Desert → Pod 01 → that Listener** shows the new name and GPS. The board
      wrote through E1's own endpoint, so there is no second write path.
- [ ] Give only a latitude: refused before anything is sent ("both latitude and longitude").
- [ ] A **Generated** or **Flashed** card offers no *Fill in details*: the device has not spoken
      yet.

## 7. Who can do what

Create users under **Users** (owner only) and sign in as each.

- [ ] **Viewer**: sees the bundle list and the board, and **no** *New bundle*, *Download bundle*,
      *Mark … flashed* or *Fill in details*.
- [ ] **Field tech scoped to High Desert**: *New bundle*, *Download* and *Mark flashed*, but **no**
      *Fill in details*. Naming devices is inventory work (`manage_devices`), which field techs
      do not hold. Switched to Redwood Coast: no *New bundle*.

## 8. Tear down

- [ ] Stop the generated stack (`docker compose down` in its unpacked folder) and the platform
      (`docker compose -f deploy/docker-compose.yml -p eoe-qa down`). **Take the platform down
      before running the test gate** (D44).
