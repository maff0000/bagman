# adapters/email/

Owns integration with email/mailbox providers (future Microsoft
Graph/Exchange, Gmail/Google APIs, IMAP), isolating the rest of BAGMAN from
provider-specific APIs per the email credential doctrine in `PID.md` §7.

Empty scaffolding as of CD-1. No implementation exists yet; CD-1 must not
connect to any mailbox (see `PID.md` §7, §16).
