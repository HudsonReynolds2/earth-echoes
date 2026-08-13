# Echoes of Earth — deployment services stack

This archive contains a complete, pre-credentialed set of the services one
Echoes of Earth deployment needs: an MQTT broker for the control plane, an
InfluxDB for telemetry, a Prometheus for metrics, a Grafana to look at both,
and optionally a MinIO for raw-audio upload.

The management platform generated it, already holds every credential in it,
and can verify the running stack from the services screen.

## Treat this archive as a credential

**Every password, token and private key for these services is in these files
in a form that can be used.** Anyone who has this archive has your deployment.

- Do not commit it to version control, attach it to a ticket, or paste it into
  a chat.
- Copy it to the host over SSH or a USB key, unpack it, and delete the archive.
- If it leaks, rotate immediately from the platform: **Services → Regenerate
  stack**. Rotation issues new credentials, re-verifies them and republishes
  them to your devices, so the old archive stops working.

The platform keeps no copy of this archive. It re-renders an identical one on
demand from the credentials it stored, so losing it costs you nothing — you can
download it again.

## Running it

You need Docker with the Compose plugin, on a host your Aggregators can reach.

```
tar xzf echoes-stack.tar.gz
cd echoes-stack
docker compose up -d
```

Check everything came up:

```
docker compose ps
```

Then go back to the platform, open the deployment's **Services** screen, and
run **Test connection**. Every service should report verified. Until they all
do, the platform will not generate provisioning bundles for this deployment —
a device provisioned against a broker nobody has proved reachable is a device
that has to be visited again.

## Ports this stack needs open

These are published on the host. Open them to your Aggregators and to the
management platform, and to nothing else — none of these services should face
the public internet.

<!-- PORT TABLE -->

## What is in the box

```
docker-compose.yml                     the stack
mosquitto/mosquitto.conf               TLS listener, dynamic security plugin
mosquitto/dynamic-security.json        the platform's broker account
mosquitto/ca.crt, server.crt, server.key   the broker's TLS material
prometheus/prometheus.yml              scrape config
prometheus/web_config.yml              basic-auth users
grafana/provisioning/                  datasources and the alert contact point
.env                                   credentials the compose file reads
```

## Notes

**The broker's certificate is signed by a private CA** that this platform
generated and trusts. That is deliberate: the platform pins the CA rather than
disabling verification, so the TLS is real. Your Aggregators receive the same
CA in their provisioning bundle.

**Device broker accounts are not in this archive.** The platform creates each
Aggregator's own broker credential, scoped to its own topics, when you
provision it. That is why `dynamic-security.json` is the one file the broker
mounts read-write.

**Prometheus's remote-write receiver is enabled here** and is off in a default
Prometheus. If you replace this stack with your own, turn it on or the platform
cannot write metrics to it.

**The Grafana alert contact point points at a platform endpoint that does not
exist yet.** Inbound alert handling ships in a later release; until then
Grafana will log delivery failures for it, which is expected and harmless.

**`node_exporter` is in the scrape config** but not in this compose file. If
you run one on the host it is picked up automatically; if you do not, that
target simply reports down.
