# FC 27 redesign — design canvas sources

Broadcast-direction redesign mockups for the Pro Clubs site (`proclubs/`),
published as a Claude Design canvas:
https://claude.ai/code/artifact/f342cee6-229e-46fd-971a-9463f37bb10c

These are **mockups, not app code**. Nothing here is served by `proclubs/`.
They exist so the visual direction can be reviewed before it's built into
the real templates and `static/css/site.css`.

| File | Artboard |
|---|---|
| `Brand.dc.html` | Design system — colour, type, motifs, components |
| `Main.dc.html` | Home page |
| `PlayerCards.dc.html` | Player cards (replaces the Players tab table) |
| `Stats.dc.html` | Stats dashboard, Overview tab |
| `League.dc.html` | League table |
| `canvas.json` | Artboard layout and notes |

The published bundle is regenerated from these files, so edit these — not
the bundle, which is gitignored.

## Direction, in short

- Ground `#05070E` (broadcast midnight) replacing the current `#0f1115`.
- Two accents with distinct jobs: `#00E27A` performance, `#FFB020` standing.
  `#FF3355` is reserved for genuinely-live streams and nothing else.
- Square corners throughout; a 12° skew is the one signature motif.
- Anton (display) + Barlow Condensed (labels, carried over from the current
  site) + Archivo (body). Tabular numerals in every numeric column.

Player card values map only to fields the EA API actually returns
(`proOverall`, `ratingAve`, `winRate`, `manOfTheMatch`, `passSuccessRate`,
`shotSuccessRate`, `tackleSuccessRate`, `cleanSheetsGK`) — no invented
attributes. Player names in the mockups are samples.
