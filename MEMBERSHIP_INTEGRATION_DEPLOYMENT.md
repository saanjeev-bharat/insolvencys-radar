# Insolvency Pulse Membership Integration — Deployment Notes

## Scope of this build

This is a staging build prepared from the supplied Insolvencys Radar repository and the active WordPress connector source supplied in chat. It does not change the live Render service, Neon database, Stripe account, or Hostinger website by itself.

### Included backend changes

- Requires a trusted WordPress bridge secret for API customer registration, login, and device registration/verification.
- Requires authentication for `/api/leads` and `/api/leads/page`.
- Free members receive one search per UTC calendar day, a maximum of five results, and a redacted preview (no ACN, ABN, contact details, source URL, or asset summary).
- Paid entitlement is calculated from an `active` or `trialing` Stripe subscription whose current billing period has not expired. The cached `users.plan` value alone does not grant paid access.
- CSV export requires a paid entitlement and a registered device.
- Paid members can register one desktop/laptop browser and one mobile browser. Replacing an occupied slot requires email verification and is limited to one replacement per member in any 30-day period.
- `/api/refresh` and `/api/status` require the administrator key.
- New database tables are additive: `search_usage`, `user_devices`, `device_change_tokens`, and `device_change_history`. They are created by the existing SQLAlchemy metadata initialization; no existing table is dropped or rewritten.

### Included WordPress plugin

Use `wordpress-plugin/insolvency-pulse-api/insolvency-pulse-api.php` as the replacement for the active connector file. Do **not** activate it alongside the old connector: both define the same Company Search and health-check functions. Back up the active file first.

Shortcodes:

- Login page: `[ip_member_login]`
- Registration page: `[ip_member_register]`
- My Account page: `[ip_member_account]`
- Device verification page: `[ip_device_verification]`
- Existing Company Search page: `[insolvency_pulse_company_search]`
- Existing API status shortcode: `[insolvency_pulse_api_status]`

Recommended WordPress page slugs are `/login/`, `/register/`, `/my-account/`, and `/device-verification/`. The existing Company Search page should keep its current URL and shortcode.

## New environment configuration required before deployment

Set these in Render (Environment):

1. `IP_WP_BRIDGE_SECRET` — a newly generated random secret of at least 32 characters.
2. `RADAR_ADMIN_KEY` — a separate newly generated random secret of at least 32 characters.
3. `WORDPRESS_FRONTEND_URL` — `https://insolvencypulse.com.au`.

Keep the existing Stripe variables and `FRONTEND_URL` unchanged. The new device-verification email uses `WORDPRESS_FRONTEND_URL` so it does not change existing Stripe return URLs.

The WordPress `wp-config.php` must contain the exact same bridge secret as Render:

```php
define( 'IP_WP_BRIDGE_SECRET', 'PASTE_THE_SAME_RANDOM_SECRET_HERE' );
```

Do not put either secret in GitHub source code, this plugin file, or a public page. `RADAR_ADMIN_KEY` is only for administrator-triggered refresh/status requests and must not be placed in WordPress front-end code.

Device verification email requires the existing Resend configuration (`RESEND_API_KEY`) and a sender accepted by Resend (`RESEND_FROM_EMAIL`). The password-reset function was preserved. No Stripe settings or plan IDs are changed by this build.

## Deployment sequence (do not skip)

1. Keep the live services unchanged until you have reviewed this build.
2. Add `IP_WP_BRIDGE_SECRET`, `RADAR_ADMIN_KEY`, and `WORDPRESS_FRONTEND_URL` to Render. Generate two different secrets; never reuse the same secret for both variables.
3. Push the backend changes to the existing GitHub `main` branch only after confirming the repository backup/branch is available. Render may automatically deploy from `main`.
4. Wait for Render deployment to finish and check `GET /api/health` still reports `status: ok` and `database: connected`.
5. Back up the active WordPress connector file, then replace it with the supplied `insolvency-pulse-api.php` file. Do not activate the older standalone `insolvency-pulse-membership.php` prototype at the same time.
6. Add the matching `IP_WP_BRIDGE_SECRET` definition to `wp-config.php`.
7. Create the Login, Register, My Account, and Device Verification pages with the shortcodes above. Keep the existing Company Search page and Avada layout.
8. Test with a newly created Free account first. Confirm one search works, a second search on the same UTC day is blocked, and only five redacted records are returned.
9. Test a paid account only after a real Stripe test subscription is active. Register one desktop and one mobile browser; verify a third device in either category triggers email verification. Verify replacement is blocked again for 30 days after a successful replacement.
10. Test CSV export, logout, expired subscription behavior, and the admin refresh key before announcing the portal live.

## Important limitations and cautions

- Device identity is a persistent, HttpOnly WordPress browser cookie, not hardware attestation. Different browsers/profiles on the same physical computer can appear as different devices; clearing cookies can trigger the verification process again. User-Agent classification distinguishes mobile from desktop and is not cryptographic device proof.
- The daily free-search boundary is UTC. A search is consumed when the authenticated API search is requested, even if it returns no matches.
- The existing password reset API remains in place. This plugin build does not add a dedicated WordPress password-reset form; do not remove or disable the existing reset flow.
- The current automated tests were run with the repository's local test-only Stripe and password-hash substitutes because those packages were unavailable in this isolated test environment. The actual Stripe SDK and Argon2 behavior must be checked in the normal GitHub Actions/Render dependency environment before production sign-off.
- No live Render, Neon, Stripe, or Hostinger test has been performed by this build process.
