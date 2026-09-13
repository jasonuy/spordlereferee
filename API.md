# play.spordle.com — Reverse-Engineering Notes

Worked example produced by following `REVERSE-ENGINEERING-GUIDE.md`. Phases 0-2
came from **static analysis of the shipped JS bundles**. Phases 3+ were later
verified **live**, authenticated with the account owner's own Spordle Play
credentials (their own account only — no other users' data was accessed, and
no endpoint outside normal read/GET use was exercised).

## Phase 0 — What this site is

`play.spordle.com` = **Spordle Play**, an "Electronic Scoresheet" web app for
hockey leagues/federations: game scheduling, officiating/assignment, rosters,
arena-slot booking, and scoresheets. Internal naming (`hi-scoresheet` OAuth
client id, `hisports` local-storage DB name) shows this product began life as
**HISports** before being rebranded under Spordle.

Related first-party subdomains referenced in the bundle:
`myaccount.spordle.com`, `page.spordle.com`, `brackets.spordle.com`,
`pdf.play.spordle.com`, `www.spordle.com`.

## Phase 1 — Architecture

Pattern **A (client-side SPA)**, but *not* Next/Nuxt/Gatsby/CRA — a hand-rolled
Webpack build with three top-level bundles:

| File | Role |
|---|---|
| `common-…-{hash}.bundle.js` | vendor + `src/http/*` (axios client, auth service, interceptors) |
| `main_modules-…-{hash}.bundle.js` | vendor (qs, react-intl, etc.) |
| `main.{hash}.bundle.js` | app code — ~17 MB, contains every feature module's API calls |

**Lucky break:** these bundles are built with Webpack's `eval`/`eval-source-map`
devtool, so each module is wrapped in `eval("...")` with the *original*,
un-minified source escaped inside a string (real variable/function names,
`//# sourceURL=./src/...` comments showing the real file tree). Unescaping
`\n`/`\"` turns the whole app back into readable source — no bundler/AST
tooling needed, just `str.replace('\\n','\n').replace('\\"','"')` then `grep`.

No `__NEXT_DATA__`, no `__NUXT__`, no embedded `data-configs` blob — config is
compiled in as JS constants, not shipped as page data.

Backend is **LoopBack** (Node.js) — confirmed by a source comment in the
token-refresh logic (*"warning: loopback is non-standard and uses 403 instead
of 401 per spec"*) and by the model-REST error shapes (`MODEL_NOT_FOUND`,
`Unknown "Game" id "x"`) seen on every 404. CDN/edge is Cloudflare
(`cf-ray`, `cf-cache-status`, NEL/report-to headers, `/cdn-cgi/rum` beacon) —
no Akamai/PerimeterX bot-manager challenge observed anywhere in this session.

## Phase 2 — Config mined from the bundle

```js
API_URL  = "https://play.spordle.com" + "/api"      // same-origin REST API
AUTH_URL = "https://play.spordle.com" + "/oauth/token"
client_id     = "hi-scoresheet"
client_secret = "secret"                             // literal string — public SPA "secret", not a real secret
```

## Phase 3 — Auth flow (OAuth2 password grant, verified live)

```
POST https://play.spordle.com/oauth/token
Content-Type: application/json
{
  "grant_type": "password",
  "client_id": "hi-scoresheet",
  "client_secret": "secret",
  "username": "<email>",
  "password": "<password>"
}
→ 200 { "access_token", "refresh_token", "expires_in": 1209600, "token_type": "Bearer" }
```
Real app persists this in **IndexedDB via `localforage`** (`db: hisports`,
`store: auth`, keys `tokens` / `user` / `identity`), not localStorage/cookies.

Every subsequent request carries:
```
Authorization: Bearer <access_token>
X-Device: <random UUID, client-generated once, persisted in the same localforage db>
X-Client-Version: 5.0.0-development
```
- `X-Device` is a pure client-side fingerprint (`crypto`-random UUID), also
  tagged into Sentry — not derived from anything server-issued, no need to
  reverse it, just generate a UUID once per "install".
- Token refresh: on `403` with `error_description: "Access token is expired"`,
  or `500` with a message containing `"Cannot call AccessToken.findById()"`
  (LoopBack quirk), the client re-POSTs `oauth/token` with `grant_type: refresh_token`.
- Third-party/SSO login variant (`loginWithSma`): same endpoint, `access_token: <idp token>`,
  `password: "third-party-token"`.

**Unauthenticated endpoints** (`skipAuth: true` in source, confirmed live):
- `GET /api/accounts/ssoCheckRedirect?username=<email>` → 204, no body.
- `POST /api/accounts/request-reset`
- `POST /api/accounts/validateIdentity`
- `POST /oauth/token`

Everything else requires a bearer token (401 otherwise) — this is *not* a
public storefront-style JSON API.

### Multi-identity / tenant switching

One Spordle **account** (login) can be linked to several **identities** —
each identity is a different `participantId` under a different **tenant**
(e.g. a regional "Ball Hockey" federation vs. "Hockey Canada" itself), each
with its own roles/permissions/season. `GET /api/accounts/current` returns
the account plus an `identities[]` array (each with its own `id` (a UUID),
`participant`, and `tenant` {name, seasonId, sport, registryUrl}).

Switching identity is **pure client-side, no server call**: the SPA persists
the chosen `identity.id` in `localforage`, then attaches it as a header on
every subsequent request:
```
X-Identity: <identity.id>
```
The server reads that header and re-scopes `permissions`, `seasonId`, and
every ACL check to that identity's `participantId`. Confirmed by re-calling
`/api/accounts/current` with the header set — it returned an entirely
different `participantId`, `seasonId`, and `permissions[]` block (different
officiating grades/offices) for the same account/token.

## Phase 4 — Endpoint catalog

### Discovered statically (92 distinct `apiClient(...)` call sites across all bundles)

Base resources — bare `/games/`, `/teams/`, `/practices/`, `/schedules/`,
`/participants/`, `/offices/`, `/contacts/`, `/draftGameApprovals/` all
appear as list-style calls in addition to the action sub-routes below,
consistent with a shared REST/CRUD helper wired to `apiClient`:

| Resource | Notable action sub-routes |
|---|---|
| `accounts` | `logout`, `register`, `change-password`, `request-reset`, `reset-password`, `ssoCheckRedirect`, `ssoLinkAccount`, `ssoLinkAccountRedirect`, `ssoResetPassword`, `validateIdentity`, `current` |
| `games` | `assign`, `applyAssignRules`, `availableOfficials`, `changeJerseyColors`, `changeStatus`, `forfeit`, `resetScoresheet`, `recalculateScoresheet`, `events`, `officialAssignments`, `requestAssignment`/`respondAssignment`/`removeAssignment`/`switchAssignment`/`delegateAssignment`, `validateMembers`, `bulkCertify`, `bulkDelegate`, `bulkDelete`, `bulkGameStatus`, `bulkAssignSettings`, `availabilities` |
| `draftgames` / `draftGameApprovals` | `bulkChangeDate`, `bulkChangeOrder`, `bulkChangeStatus`, `bulkDelete`, `bulkShare`, `{id}/respond` |
| `practices` | `changeStatus`, `bulkDelete`, `bulkStatus`, `availabilities` |
| `schedules` | `publish`, `generate`, `generateMatrix`, `importBrackets`, `deleteBracketGames`, `settings`, `updateManualRankings` |
| `teams` | `{id}/teamArenas`, `{id}/effectiveSettings`, `{id}/sync` |
| `arenaslots` / `surfaces` | `buildSlots`, `bulkChangeTypes`, `bulkDelete`, `bulkRestrictions`, `{arenaId}/overlaps`, `slotOverlaps` |
| `offices` | `{id}/analyze`, `{id}/applyAssignRules`, `officeClaimsSettings`, `effectiveOfficeAssignSettings` |
| `participants` | `{id}/officialSettings`, `{id}/assignAvailability`, `{id}/assignHistory`, `{id}/identities`, `{id}/status`, `{id}/invite`, `{id}/ssoInvite`, `{id}/signInAs`, `{id}/calendarFeed` |
| `officialClaims` / `officialTransactions` | `process`, `assignPayPeriod` |
| `gamepenalties` | `export` |
| `gamequestionnaireanswer` / `questionnairequestions` | `saveQuestionnaireAnswers`, `{officeId}/getQuestions` |
| `pools` | `bulkCreate` |
| `contacts` | `{id}/sendVerification` |
| `reports` | (list/create) |
| `apimetadatas` | `getVersion` |

### Verified live (this session, authenticated as the account owner)

| Method & path | Auth | Behavior confirmed |
|---|---|---|
| `POST /oauth/token` | none | Password-grant login, returns bearer + refresh token |
| `GET /api/accounts/current` | Bearer | Returns `id`, `username`, `participantId`, `permissions[]`, `identities[]`, `seasonId`, `notices[]`, `flags[]` (feature flags for the tenant) |
| `GET /api/games` | Bearer | Returns the **global, unfiltered** game list (all offices, all seasons at once — no ACL scoping seen); ordinary `?key=value` query params are silently ignored |
| `GET /api/games?filter={...}` | Bearer | LoopBack `filter` JSON param works: `where`, `include`, `limit` all honored |
| `GET /api/games/{id}` | Bearer | Single game by numeric id; 404 body is `{"error":{"statusCode":404,"message":"Unknown \"Game\" id \"X\"","code":"MODEL_NOT_FOUND"}}` for a nonexistent id |
| `GET /api/teams?filter={"where":{"id":{"inq":[...]}}}` | Bearer | **ACL-scoped** — only resolves teams the calling identity is actually rostered on; other ids in the same `inq` batch are silently dropped from the result, not errored |
| `GET /api/surfaces?filter={...}` | Bearer | Not ACL-scoped — full national rink/venue directory (10,000+ rows seen), each row nests a `venue` object (name/address/city/region/timezone). `where` only matches the surface's own columns, not nested `venue.*` fields (LoopBack limitation, not a bug) — pull a big page and filter client-side instead |
| `GET /api/offices?filter={"where":{"id":{"inq":[...]}}}` | Bearer | **ACL-scoped** the same way as `teams` |
| `GET /api/offices/{id}` | Bearer | Also ACL-scoped, but returns a real **404** for an office you can't see rather than 403 — "deny by pretending it doesn't exist," even though you can still receive officiating assignments run by that office |
| `GET /api/participants/{id}/assignHistory` | Bearer | Requires `?seasonId=` (found via its own 400 error message: `"seasonId is a required argument"` — a great Phase-4.4 "echo field"). Returns confirmed officiating assignments for that participant/season, each with the full nested `game` object |
| `GET /api/participants/{id}/assignHistory?seasonId=X` across seasons | Bearer | Not ACL-scoped beyond "this is your own participant" — works for whichever identity's `participantId` is active |
| `GET /api/officialTransactions?filter={"where":{"participantId":X}}` | Bearer | Pay-ledger rows per game officiated: `date`, `amount`, `type`, `subtype` (Referee/Linesperson), `officeId` (the **paying** office, which can differ from the game's own `officeId`), `gameId` |
| `GET /api/officialClaims?filter={"where":{"participantId":X}}` | Bearer | Expense-claim rows (empty for this account) |
| `GET /api/officialAssignments` (top-level, no id) | Bearer | **404** — `"There is no method to handle GET /officialAssignments"`; this resource is only ever addressed as a sub-route of `/games/{id}/officialAssignments`, not as its own top-level list |
| `GET /api/apimetadatas/getVersion` | Bearer | 401 without a token; not tested authenticated |

### The real "Games" page call (captured live from the browser's own network traffic)

Everything above about `/api/games` being globally unscoped was from calling
it **without** a `scope` key. Watching the actual `/games` page in the app
shows the real call always sets one:

```
GET /api/games?filter={
  "scope": "Authorized",
  "include": ["gameBracket", "shootouts"],
  "where": {"and": [
    {"date": {"gte": "<today, YYYY-MM-DD>"}},
    {"seasonId": "<season>", "_withCount": true}
  ]},
  "order": ["date ASC", "startTime ASC", "number ASC"],
  "limit": 25,
  "skip": 0
}
```
`"scope":"Authorized"` is a **custom LoopBack scope** the backend defines —
it's the real ACL switch that was missing from my earlier testing (plain
`/api/games` with no `scope` key returns everything unfiltered; add
`"scope":"Authorized"` and it's cut down to only games the *current identity*
has a role on). Confirmed side-by-side on the same account:

| Identity | `x-identity` | `where` | `X-Total-Count` |
|---|---|---|---|
| Ball Hockey (own account) | `66fa47a8-...` | date≥2026-09-12, season 2026-27 | **2** |
| Hockey Canada (referee) | `a4cdaedc-...` | date≥2026-09-12, season 2026-27 | **124** |

The Ball Hockey identity's count (2) exactly matches the 2 confirmed
officiating assignments found earlier via `assignHistory` — but the Hockey
Canada identity's count (124) is far larger than its 2 *personally-assigned*
games. **`scope:"Authorized"` means "games you're allowed to view/act on"**
(any game run by any of your 21 eligible officiating offices, assigned to you
or not), not "games assigned to you" — an important distinction Phase 4's
static analysis alone couldn't have told us; it took watching a real session.

Response headers repeat the useful bits already known
(`X-Total-Count`, `X-Authorized-Roles: scheduling:view,assigning:official,scoresheets:view,suspensions:view`
for this call) plus real HTTP caching (`ETag` / `If-None-Match`, `304`s
observed on repeat identical requests).

### The support calls that hydrate the grid

The games list only returns foreign keys (`arenaId`, `homeTeamId`,
`awayTeamId`, `groupId`, `officeId`, game `id`) — the UI issues four more
`scope:"Tenant"` bulk lookups per page to resolve everything to display data:

```
GET /api/surfaces?filter={"where":{"id":{"inq":[<arenaIds from this page>]}},"scope":"Tenant"}
GET /api/teams?filter={"where":{"id":{"inq":[<home+away team ids>]}},"scope":"Tenant"}
GET /api/groups?filter={"where":{"id":{"inq":[<groupIds>]}},"scope":"Tenant"}
GET /api/gamestatuses?filter={"where":{"id":{"inq":[<game ids from this page>]}},"scope":"Tenant"}
```
`scope:"Tenant"` behaves like the earlier-documented reference-data lookups
(broad, not row-ACL'd the same strict way `teams`/`offices` were tested
before — though still confined to what the tenant/identity should see).

**`gamestatuses` is new and is the actual "outstanding games" signal.** Its
`id` equals the game's own `id` (one status record per game, not a separate
numeric FK), and it carries the officiating fill state directly:
```
status ("scheduled" | "approved" | "certified"),
officials: {
  officeId, referees, linespersons, scorekeepers, timekeepers, supervisors,
  pending, declined, requests, unassigned, allAssigned, allConfirmed
},
score: { home, away, forfeit },
assigning, scorekeeping, scoringMode, rescheduleRequests, requireCertification
```
A game is "outstanding" from an assigning/officiating standpoint when
`officials.allAssigned === false` or `officials.unassigned > 0` — this is a
much more precise definition than what could be inferred from `assignHistory`
alone (which only ever shows *your own* confirmed rows, not the office-wide
picture of which games still need officials).

### Filter/query syntax (LoopBack "filter" param)

Plain `?key=value` query params are **ignored** by list endpoints — the real
vocabulary is a single URL-encoded JSON `filter` param:
```
GET /api/<resource>?filter={"where":{"seasonId":"2026-27"},"include":["x"],"limit":50}
```
- `where` supports Mongo-ish operators: `{"id":{"inq":[1,2,3]}}`, `{"field":{"like":"pattern","options":"i"}}`.
- `where` only matches columns on the **base model itself** — it cannot filter
  on a nested/joined field like `venue.name` inside `surfaces`, even though
  the response embeds that nested object.
- `include` is accepted (LoopBack relation-include syntax) but didn't add any
  visible fields in testing (e.g. `include:["homeTeam","awayTeam"]` on
  `games` returned the game unchanged — those relations may not be wired
  server-side, or need a different name).
- `limit` works as expected.
- When a required param is missing, the API returns a `400` whose message
  **names the missing param verbatim** (e.g. `seasonId is a required
  argument`) — cheap and reliable way to discover required filters (Phase 4.4
  of the guide, "echo fields are debug gold").

## Data shapes learned (real records, IDs shown are real but not sensitive)

**Game** (`/api/games`):
```
number, date, startTime, endTime, timezone, division, gender, category,
categoryId, seasonId, arenaId, scheduleId, groupId, crossGroupId,
crossScheduleId, officeId, homeTeamId, awayTeamId, homeTeamComment,
awayTeamComment, homeTeamStats, awayTeamStats, status
  (seen: Active, Cancelled, Postponed, Rescheduled, "Rink Changed"),
comments, isApproved, isCertified, spectators, actualStartTime, actualEndTime,
homeDefaultColor, awayDefaultColor, id
```
`homeTeamId`/`awayTeamId` are `null` for ID-camp/showcase-tournament games
that aren't tied to a standing team.

**Team** (`/api/teams`):
```
HCRId, name, shortName, division, gender, categoryId, seasonId, logoId,
externalId, isPublic, isSanctioned, homeColor, awayColor, id, createdAt,
updatedAt, officeId, logoUrl (CloudFront-hosted)
```

**Surface** (`/api/surfaces`) — a single physical rink/sheet:
```
id, name (rink label within the venue, e.g. "2 Blue"), alias, path, type
  (e.g. "Ice"), size, sports[], timezone, externalId, venueId, parentId,
venue: { id, name, address, city, region, postalCode, country, alias,
         timezone, externalId }
```

**Account/identity** (`/api/accounts/current`):
```
id, username, externalId, participantId, seasonId, notices[], flags[]
  (tenant feature flags, e.g. "brackets", "pools", "single-sign-on"),
identities: [{ id, accountId, participantId, isPrimary,
               participant: {HCRId, firstName, lastName, ..., fullName},
               tenant: {name, seasonId, sport, registryUrl} }],
permissions: [{ roleName, roleType, roleContext, scopes[],
                participantId? | teamIds[]+officeIds[],
                officeIds[]?, venueIds[], level, grades{}, expiry }]
```
Roles seen: `participant` (profile self-management), `player`
(`teams:view`, `scheduling:view`, `scoresheets:view`, `suspensions:view`,
scoped to `teamIds[]`/`officeIds[]`), `official` (`assigning:availability`,
`assigning:official`, `teams:view`, scoped to `officeIds[]` with a numeric
`level` and a `grades` map like `{"Referee": 2, "Linesperson": 2}`).

**AssignHistory row** (`/api/participants/{id}/assignHistory`):
```
id, gameId, officeId, payOfficeId, feesId, participantId,
position (Referee/Linesperson), status (confirmed/...),
notes, signature, notificationDate, participant: {...}, game: {...full Game...}
```

**OfficialTransaction row** (`/api/officialTransactions`):
```
id, participantId, gameId, officeId (the paying office — can differ from the
game's own officeId), date, amount (string decimal), type (e.g. "Base"),
subtype (Referee/Linesperson), description, payPeriodId, claimId
```

## Additional resources seen only in live browser traffic (not found by static grep)

The real app also calls these on login/profile/games-page load — none of
these turned up in the static bundle scan, likely built from string
concatenation the grep patterns didn't catch:

| Endpoint | Purpose |
|---|---|
| `GET /api/members?filter={"scope":"Authorized","include":[{"relation":"team","scope":{"scope":"Tenant","include":"category"}}],"where":{"participantId":X,"seasonId":"..."}}` | Team memberships for a participant/season, with the team + category nested in one call via `include` |
| `GET /api/participants/{id}/contacts?filter={"scope":"Authorized","where":{"_withCount":true},"order":"isPrimary DESC","limit":25,"skip":0}` | Profile contact list, real pagination (`limit`/`skip`/`order`) |
| `GET /api/participants/{id}/addresses?filter=...` (same shape) | Profile addresses |
| `GET /api/participants?filter={"where":{"id":{"inq":[X]}},"scope":"Tenant"}` | Bulk participant lookup |
| `GET /api/profiles?filter={"where":{"id":{"inq":[X]}},"scope":"Tenant"}` | Bulk profile lookup |
| `GET /api/preferences?filter={"scope":"Authorized","order":"id ASC","limit":25,"skip":0}` | Account/UI preferences |

This confirms the real pagination vocabulary is `limit`/`skip`/`order`
(inside the same `filter` JSON, not separate query params), and that
`_withCount:true` inside `where` is how the client asks for `X-Total-Count`
to be populated on the response.

## Access-control model (observed, not documented anywhere)

- `games` and `surfaces` are only "wide open" when called **without** a
  `scope` key — that's an omission in ad-hoc testing, not how the real app
  calls them. The real app always passes `"scope":"Authorized"` on `games`
  (row-ACL'd to the calling identity's roles/offices) or `"scope":"Tenant"`
  on reference-data bulk lookups (`surfaces`, `groups`, `gamestatuses` —
  broad within the tenant, not row-ACL'd to the individual the way `teams`/
  `offices` are).
- `teams` and `offices`, by contrast, **are** row-level ACL'd to the calling
  identity's actual roster/admin relationships — batch `inq` lookups
  silently drop ids you're not allowed to see rather than erroring, and
  single-id GETs on `offices` return a 404 (not 403) for ids outside your
  scope, including offices that legitimately assign you games.
- Net effect: you can always find out *that* a game/rink exists and its
  schedule details, but you can't always resolve the *name* of the team or
  office on the other end of it unless you have a direct relationship there.

## What's still undocumented

- Full CRUD verbs (`POST`/`PUT`/`DELETE`) on any resource — only `GET` calls
  were made this session; the guide's own scope for "read a scoreboard" type
  automation doesn't need them, and testing writes against a real production
  system without a clear need wasn't done.
- `X-Authorized-Roles` response header's exact shape (it's declared
  CORS-exposed but its content wasn't inspected).
- The dedicated "pending/unresponded assignment offer" flow
  (`requestAssignment`/`respondAssignment` on `games`) — everything pulled
  this session came from `assignHistory`, which only ever showed
  already-`confirmed` rows.

## Anti-bot / rate-limit / ethics notes (Phase 8)

- `robots.txt`: `Content-Signal: ai-train=no, use=reference`, generic
  crawling allowed, but a list of AI bots (including **ClaudeBot**) is
  explicitly disallowed — irrelevant to a human doing interactive,
  logged-in-as-themselves reverse engineering, but would matter if this were
  ever turned into an unattended scheduled crawler.
- Cloudflare-fronted; no bot-challenge or rate-limit response seen in this
  session (roughly two dozen authenticated GETs total, with small delays,
  spread across two identities on one account).
- Every call in this session authenticated as the **account owner's own
  login**, touching only that person's own accessible data (their own games,
  their own two team memberships, their own officiating history/pay ledger).
  No other users' personal data, no write operations, no bulk scraping of
  another organization's league. Treat any client built from this doc the
  same way: personal automation against your own account, not a scraper
  against the wider league database it happens to expose.
