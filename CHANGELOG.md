# Changelog

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
