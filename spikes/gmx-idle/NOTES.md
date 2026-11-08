# Results (2026-11-07 to 2026-11-08)

## IDLE stability
- Ran 36 hours with IDLE renewed every 10 minutes (about 200 renewals).
- GMX ends the connection about every 3 hours (`BYE timeout`); every
  reconnect after 30 seconds worked.
- No mail was missed. One early "missing" mail was a race with the
  service deleting it before the spike fetched its headers.

## Latency after the mail reached GMX
| Method | Latency |
| --- | --- |
| IDLE notification | 2-10 s (mostly 5 s, 12 mails) |
| Poll every second (`SEARCH ALL` / `SEARCH UNSEEN`) | about 1 s |
| `SEARCH SUBJECT` | 10-27 s later than `SEARCH ALL` |

The search probe (separate container, read-only, polling every second for
over 2 hours) saw no errors or throttling.

## Recommendation
Polling beats IDLE on GMX: the server delays IDLE notifications by several
seconds, while a poll sees a new mail almost at once. The real bottleneck
was the service's `SEARCH SUBJECT`, which depends on GMX's lagging search
index.

Adopted: search `UNSEEN` and check the subject client-side, poll every
3 seconds (expected about 10 s from arrival to confirmation instead of
about 31 s). IDLE is not pursued.
