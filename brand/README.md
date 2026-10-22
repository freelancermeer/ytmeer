# ytmeer brand

The mark is **four bars, a dashed average line, and one bar breaking through
it** — the outlier ratio, which is the measurement the whole toolkit is built
on. It says what the tool does rather than that it touches YouTube.

Deliberately **not** a play button: no rounded square, no right-pointing
triangle, nothing borrowed from YouTube's own lockup. This is a tool that reads
YouTube, not a thing that is part of it.

## Files

| File | Use |
|---|---|
| `ytmeer-logo.svg` | horizontal lockup, mark + wordmark. The README header |
| `ytmeer-mark.svg` | mark alone, for light backgrounds |
| `ytmeer-mark-dark.svg` | mark alone, for ink backgrounds |
| `ytmeer-icon-small.svg` | 16–20px variant: the dashes go solid, or they fill in |

Never shrink the mark below 16px, never put a gradient on it, never stretch the
bars, and never recolour a non-outlier bar to the accent.

## Colour

| Token | Hex | Use |
|---|---|---|
| Ink | `#0D1117` | ground, terminal, text on paper |
| Raised | `#161B22` | cards and panels on ink |
| Signal | `#F25C2A` | the outlier. **One use per view** |
| Paper | `#FAFAF8` | docs and light surfaces |
| Muted | `#8B949E` | labels on ink |
| Quiet | `#4A5159` | body text on paper |
| Signal ink | `#C23A12` | small accent text **on light** |

Measured contrast: Signal on Ink is 5.5:1 and Muted on Ink is 6.1:1, both fine.
**The one trap:** Signal on Paper is only 3.3:1 — good for a 24px+ heading,
failing for small text. Use Signal ink there (5.3:1).

## Type

**Space Grotesk** for anything a person reads, **JetBrains Mono** for anything
the machine produced — view counts, ratios, paths, job ids. Two faces, no third.
Both are on Google Fonts; fall back to `system-ui` and `ui-monospace`.

The wordmark is `YT` in regular Muted plus `meer` in bold Ink, letter-spacing
−3%. Never all-bold, never all-caps, never a space or hyphen in the middle.

## The visual sheet

The full brand sheet — mark construction, size ramp, lockups, the type scale and
worked examples (terminal banner, README header, editor tool list, outlier row)
— is a private Claude artifact:

<https://claude.ai/artifact/2astZN436MngVuech92jQe>

Only the owner can open that link until it is shared from the page's Share menu.
