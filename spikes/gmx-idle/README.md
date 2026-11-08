# Spike: GMX inbox via IMAP IDLE

Question: is IMAP IDLE on GMX stable and faster than polling, so the
service could react to the household mail sooner?

- `idle_spike.py` watches the inbox read-only (IDLE, or NOOP polling with
  `MODE=poll`) and logs the latency between the topmost `Received` header
  and the moment the client noticed the mail.
- `run.sh` starts it in a container of the service image with the
  credentials from `.env`; `docker logs -f notflix-idle-spike` follows it.
- Dependency: `imapclient` 4.1.0 (BSD), installed only inside the spike
  container.

Result and recommendation: [NOTES.md](NOTES.md). The spike is finished;
the service keeps polling (every 3 seconds).
