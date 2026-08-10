"""The device control plane (epic E3; spec 6, 7).

Everything that talks to a deployment's MQTT broker lives here: the client
manager (E3.2), and later the revision state machine, the publisher, the
reported consumer and the reconciliation worker. The wire contract itself is
deliberately NOT here — it lives in `app.contracts.mqtt`, which is published
outside this codebase (SIM imports it, firmware is written against it).
"""
