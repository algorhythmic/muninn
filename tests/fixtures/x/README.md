# X API v2 fixtures — synthetic, documentation-derived, not recorded

Every file in this directory was written by hand from the public X API v2
documentation (docs.x.com, as read on 2026-10-21; see
`docs/research-intake/RI-02-RI-03-work-plan.md` §A). None of it was recorded
from a live account. All IDs, handles, names, tokens and URLs are synthetic
(`example.com`, `example.org`, `example.net`, `github.com/example-org/example-repo`,
`t.co/EXAMPLE…` codes, patterned numeric IDs).

Each response is a pair:

- `<name>.json`: the response body, kept as close to the documented shape
  as possible, so it carries no label key of its own.
- `<name>.headers.json`: `{"_fixture": …, "status": …, "headers": {…}}`, the
  HTTP status and headers (including `x-rate-limit-*`). Reset epochs are
  relative to `1792584000` (2026-10-21T12:00:00Z), the fake clock's start in
  `tests/unit/test_x_connector.py`.

| Fixture | Covers |
|---|---|
| `bookmarks_page1` | plain post with a fully unwound link (tracking params); long post with `note_tweet` and a GitHub repository link; quote post with `includes.tweets` (the quoted post has a t.co-only link); post with media |
| `bookmarks_page2` | short page (2 posts) that still carries `next_token`; `article` with a body and an `x.com/i/article` link; `article` without a body; `unwound_url: null`; `status: 404`; 200 with `errors[]` (`resource-not-found`, `not-authorized-for-resource`, `resource-unavailable`) |
| `bookmarks_page3` | terminal page (no `next_token`); a post with no links; a destination carrying a credential-shaped query param |
| `users_me` | `GET /2/users/me` |
| `error_429_rate_limit` | 429 `rate-limit-exceeded` with `x-rate-limit-reset` |
| `error_429_usage_capped` | 429 `usage-capped` (hard stop; rate headers still show budget) |
| `error_401_unauthorized` | 401 for an expired access token |
| `error_403_client_forbidden` | 403 `client-forbidden` |
| `token_exchange`, `token_refresh_rotated`, `token_error_invalid_grant` | OAuth 2.0 token endpoint: code exchange, refresh returning a rotated refresh token, refresh failure |
| `folders_list`, `folder_posts` | bookmark folder list, and an ID-only folder page |

Field spellings (`tweet.fields` vs `post.fields`, `includes.tweets`), the
`article` body key (`plain_text`), and the problem-type URIs are documentation
readings that RI-02a has not verified live.
