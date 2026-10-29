# Changelog

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
