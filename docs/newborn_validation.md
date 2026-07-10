# Newborn assistant validation (the publish gate)

A lightweight-seed assistant (born by the Clone Agent v1 via Build's
`clone_seed.py`) is created **private** and is not announced or flipped
public until these legs pass. Heart owns this checklist; the CloneDecision's
validation plan points here. Run each leg in the newborn's checkout.

1. **Symbol audit** — the copied API gate against the newborn's own domain
   library: `python autoassistant/audit_skill_apis.py` (generic tooling,
   copied at birth). At seed stage most domain skills are PENDING, so the
   audit's job is proving the *copied* generic surfaces reference nothing
   stale.
2. **Link sweep** — no dangling cross-references outside `PENDING.md`:
   grep the wiki/skill indexes for links whose targets neither exist nor
   appear in `PENDING.md`. Seed scaffolds legitimately point at pending
   pages; anything else dangling is a substitution bug.
3. **Wiki-currency check** — the copied `.github` wiki-currency workflow runs
   green (it clones `sources/` at main — doc-pin truth, not the release
   wheel).
4. **Chat-surface smoke** — the reference's `modes/maintainer.md` procedure,
   run against the newborn once public: `llms.txt` bootstrap on each
   supported surface, confirming the assistant states its capability
   boundary instead of pretending at the pending content.

Passing 1–3 gates the repo going public; leg 4 runs post-publish (it needs
the public URL). `PENDING.md` is the newborn's growth queue — emptying it is
development, not validation, and never blocks the publish gate.
