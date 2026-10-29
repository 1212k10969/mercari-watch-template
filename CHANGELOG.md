# Changelog

## 0.4.3

- Support verified Mercari Shops identities on Buyee's native Mercari item route.
- Use Buyee for primary Discord and LINE links while retaining original item identities and history.
- Keep region-sensitive original URLs as copyable text in Buyee mode, with plain source labels, to avoid ambiguous direct-source links.
- Reject mismatched Shops IDs and external source URLs instead of guessing a proxy destination.

## 0.4.2

- Forward an optional third independent Discord destination to source jobs, connection tests and clock alerts.

- Replace the unverified native cron with a GitHub-hosted environment wait timer and automatic workflow handoff.
- Verify timer configuration before dispatching; refuse missing timers and required-reviewer gates instead of creating a rapid loop.
- Trace automatic monitor runs to their parent clock and report clock failures to configured Discord destinations.
- Preserve separate encrypted source caches and fixed-sale filtering across the workflow registration change.
- Document cloud-clock setup, stopping, restarting and actual automatic-run evidence.

## 0.4.1

- Remove auction websites from supported monitor sources and workflow choices.
- Exclude Mercari auction items by default and disable them in the shipped example.
- Preserve encrypted listing history while limiting monitoring to fixed-sale sources.

## 0.4.0

- Add the Japanese Suruga-ya storefront using Patchright and a dedicated browser profile.
- Verify native updated-first ordering and sold-out exclusion, then retain confirmed direct-store offers.
- Separate condition branches and new/used identities; label price ranges and exclude reservation or marketplace-only goods.
- Resolve actual public CDN image redirects before notifications; preserve names and links if images are unavailable.
- Add an optional runner probe without private search rules or notification credentials.
- Run Suruga-ya on Windows/Edge with a separate encrypted state cache; preserve the existing Linux sources and history.
- Support selective source runs and one unattended attempt at a visible verification checkbox.
- Retry a Taiwan page timeout once; allow manual source selection without replaying other providers.
- Correct the comparison assessment: semantic product matching, Taiwan price extraction and landed-cost calculation are not implemented.
- Describe evidence-based multimodal matching and abstention instead of claiming unmeasured zero-token matching quality.

## 0.3.1

- Add an optional Buyee destination for Mercari notification titles and LINE buttons.
- Preserve original listing links, copyable item IDs and existing deduplication history.
- Leave Yahoo, Shops and listings without a verified original Mercari ID on their source URLs.

## 0.3.0

- Add public Yahoo! Fleamarket and Yahoo! Auctions searches with verified newest and active filters.
- Canonicalize mirrored Yahoo listings to one identity across sources and notification rounds.
- Preserve auction current prices, deadlines and bid counts; keep fixed-price listings distinct.
- Reuse existing encrypted history and quietly baseline newly enabled sources.
- Document local-stock comparison, landed-cost estimates and a rules-first design without runtime AI calls.
- Record Suruga-ya access restrictions; keep unverifiable sources outside production monitoring.

## 0.2.0

- Route private watch rules to independently configured Discord webhooks.
- Keep deduplication and delivery retries separate for each destination.
- Migrate existing single-destination history without replaying known listings.
- Send provider health alerts to the affected destination.
- Test all configured Discord destinations with clearly marked test messages.
- Refresh the fork's schedule registration while retaining a 30-minute interval.

## 0.1.0

- Add independent Japan and Taiwan searches using newest and available filters.
- Merge matching listings by their original item identity across sources and runs.
- Include item names, large product images, and direct links in Discord alerts.
- Support Discord, LINE, or both, with isolated notification test commands.
- Load private watch rules and credentials from environment variables or .env.
- Encrypt persistent deduplication state before storing it in Actions Cache.
- Document setup, privacy boundaries, failure handling, and upstream license scope.
